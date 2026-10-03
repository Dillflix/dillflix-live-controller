"""Durable workflow lifecycle shared by playback implementations.

Owns tokens, worker scheduling and cancellation obligations, never app navigation.
"""

import asyncio
import concurrent.futures
import fcntl
import json
import logging
import time

from .models import ExecutorError
from .store import ExecutorStore, utc

log = logging.getLogger(__name__)


class PlaybackWorker:
    def __init__(self, database, settings):
        self.db, self.settings, self.config = database, settings, settings.executor
        self.store = ExecutorStore(database, settings)
        self.input_lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.loop = self.worker = self.active = self.active_token = self.guard = None
        self.stopping = False

    async def start_resources(self):
        pass

    def recover(self):
        self.store.recover()

    async def worker_stopped(self):
        pass

    async def close_resources(self):
        pass

    def invalidate_input_context(self):
        pass

    async def start(self):
        if self.worker:
            return
        self.guard = open(str(self.db.path) + ".executor.lock", "a")
        try:
            fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.guard.close()
            self.guard = None
            raise RuntimeError("Only one executor process may own this device database") from None
        self.loop = asyncio.get_running_loop()
        await self.start_resources()
        self.recover()
        self.stopping = False
        self.worker = asyncio.create_task(self.run(), name="playback-executor")

    async def close(self):
        self.stopping = True
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.active and not self.active.done():
            self.active.cancel()
            await asyncio.gather(self.active, return_exceptions=True)
        # Interrupted navigation remains a durable cleanup obligation if offline.
        pending = []
        if self.guard:
            with self.db.transaction() as db:
                pending = [
                    r[0] for r in db.execute("SELECT token FROM executor_jobs WHERE cancel_requested=1")
                ]
        for token in pending:
            try:
                async with asyncio.timeout(self.config.cancel_timeout):
                    await self.cancel_one(token)
            except (ExecutorError, TimeoutError):
                pass
        await self.close_resources()
        if self.guard:
            self.guard.close()
            self.guard = None
        self.loop = self.worker = None

    def notify(self, tokens=None, *, replace=False):
        if self.loop and self.loop.is_running():

            def wake():
                if (
                    self.active
                    and not self.active.done()
                    and (replace or self.active_token in (tokens or []))
                ):
                    self.active.cancel()
                self.wake.set()

            self.loop.call_soon_threadsafe(wake)

    def submit(self, request):
        if not self.loop or self.stopping:
            raise ExecutorError("executor_starting", "Playback worker is not running")
        report, created = self.store.submit(request)
        self.notify(replace=created)
        return report, created

    async def cancel(self, command):
        tokens = self.store.request_cancel(command)
        self.notify(tokens)
        async with asyncio.timeout(self.config.cancel_timeout):
            while True:
                with self.db.transaction() as db:
                    pending = [
                        t
                        for t in tokens
                        if self.store.get_row(db, t)["cancel_requested"] != 2
                        and self.store.get_row(db, t)["request"] is not None
                    ]
                    stopped = any(self.store.get_row(db, t)["touched_device"] for t in tokens)
                if not pending:
                    return {
                        "device_id": command.device_id,
                        "token": command.token,
                        "through_intent_version": command.through_intent_version,
                        "input_quiescent": True,
                        "active_playback": "stopped" if stopped else "already_inactive",
                        "acknowledged_at": utc(),
                    }
                if not self.loop or self.stopping:
                    raise ExecutorError(
                        "executor_unavailable", "Cancellation is retained; executor is not running"
                    )
                await asyncio.sleep(0.05)

    def cancel_sync(self, command):
        if not self.loop or not self.loop.is_running() or self.stopping:
            self.store.request_cancel(command)
            raise ExecutorError("executor_unavailable", "Cancellation is retained; executor is not running")
        future = asyncio.run_coroutine_threadsafe(self.cancel(command), self.loop)
        try:
            return future.result(timeout=self.config.cancel_timeout + 2)
        except (concurrent.futures.TimeoutError, TimeoutError) as error:
            future.cancel()
            raise ExecutorError(
                "cancel_unconfirmed", "Playback cancellation is still pending; manual input stays disabled"
            ) from error

    async def run(self):
        next_prune = 0
        try:
            while not self.stopping:
                if time.monotonic() >= next_prune:
                    self.store.prune()
                    next_prune = time.monotonic() + 3600
                with self.db.transaction() as db:
                    cancellations = db.execute(
                        "SELECT token,next_check FROM executor_jobs WHERE cancel_requested=1 ORDER BY intent"
                    ).fetchall()
                    row = (
                        db.execute(
                            "SELECT * FROM executor_jobs WHERE cancel_requested=0 AND request IS NOT NULL "
                            "AND state IN ('accepted','navigating','playing_verified') AND next_check<=? "
                            "ORDER BY CASE state WHEN 'playing_verified' THEN 1 ELSE 0 END,intent DESC LIMIT 1",
                            (time.time(),),
                        ).fetchone()
                        if not cancellations
                        else None
                    )
                if cancellations:
                    for cancel in cancellations:
                        if cancel["next_check"] <= time.time():
                            try:
                                await self.cancel_one(cancel["token"])
                            except ExecutorError as error:
                                with self.db.transaction() as db:
                                    job = self.store.get_row(db, cancel["token"])
                                    report = json.loads(job["report"])
                                    report["observation_status"].update(
                                        state="unavailable", checked_at=utc(), error=error.detail()
                                    )
                                    self.store.write(db, job["token"], report, next_check=time.time() + 5)
                elif row:
                    self.active_token = row["token"]
                    routine = self.monitor if row["state"] == "playing_verified" else self.navigate
                    self.active = asyncio.create_task(routine(dict(row)))
                    try:
                        await self.active
                    except asyncio.CancelledError:
                        if self.stopping or asyncio.current_task().cancelling():
                            raise
                    finally:
                        self.active = self.active_token = None
                    continue
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), 0.25)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            if self.active and not self.active.done():
                self.active.cancel()
                await asyncio.gather(self.active, return_exceptions=True)
            raise
        except Exception:
            # Fail visibly; no uncontrolled device loop after an internal defect.
            log.exception("Playback executor stopped unexpectedly")
            self.stopping = True
        finally:
            await self.worker_stopped()
