"""Durable workflow lifecycle shared by playback implementations.

Owns tokens, worker scheduling and cancellation obligations, never app navigation.
"""

import asyncio
import concurrent.futures
import fcntl
import json
import logging
import sqlite3
import time

from ..database import is_database_busy
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
        self.database_status = {"state": "ready", "consecutive_failures": 0}

    def recover(self):
        raise NotImplementedError

    async def close_resources(self):
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
        await self.database_call(self.recover)
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
            def read_cancellations():
                with self.db.snapshot() as db:
                    return [r[0] for r in db.execute("SELECT token FROM executor_jobs WHERE cancel_requested=1")]
            pending = await self.database_call(read_cancellations)
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
        tokens = (await self.database_call(self.store.request_cancel, command))
        self.notify(tokens)
        async with asyncio.timeout(self.config.cancel_timeout):
            while True:
                def read_cancellation():
                    with self.db.snapshot() as db:
                        pending = [
                            t
                            for t in tokens
                            if self.store.get_row(db, t)["cancel_requested"] != 2
                            and self.store.get_row(db, t)["request"] is not None
                        ]
                        outcomes = {
                            json.loads(self.store.get_row(db, t)["report"])
                            .get("cancellation", {})
                            .get("active_playback", "unknown")
                            for t in tokens
                            if self.store.get_row(db, t)["request"] is not None
                        }
                        active_playback = next(
                            (s for s in ("unknown", "stopped", "not_current") if s in outcomes),
                            "already_inactive",
                        )
                    return pending, active_playback
                pending, active_playback = await self.database_call(read_cancellation)
                if not pending:
                    return {
                        "device_id": command.device_id,
                        "token": command.token,
                        "through_intent_version": command.through_intent_version,
                        "input_quiescent": True,
                        "active_playback": active_playback,
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

    def pending_work(self):
        with self.db.snapshot() as db:
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
        return cancellations, row

    async def database_call(self, function, *args, **kwargs):
        # SQLite's busy timeout must not freeze service RPCs or cancellation.
        # Drain the thread on shutdown so maintenance cannot outlive our guard.
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def run(self):
        next_prune = 0
        try:
            while not self.stopping:
                try:
                    if time.monotonic() >= next_prune:
                        await self.database_call(self.store.prune)
                        next_prune = time.monotonic() + 3600
                    cancellations, row = await self.database_call(self.pending_work)
                    await self.dispatch_work(cancellations, row)
                except sqlite3.OperationalError as error:
                    if not is_database_busy(error):
                        raise
                    failures = self.database_status["consecutive_failures"] + 1
                    delay = min(5, 2 ** min(failures - 1, 3))
                    self.database_status = {
                        "state": "waiting", "consecutive_failures": failures,
                        "last_error": "database_busy", "last_failure_at": utc(),
                        "retry_at": time.time() + delay,
                    }
                    if failures == 1:
                        log.warning("Playback scheduling waiting for database lock; durable requests retained")
                    # Reload durable intent and attempt receipts, never retry
                    # the failed call directly. Workflows reconcile an existing
                    # attempt or cancellation before considering a new action.
                    await asyncio.sleep(delay)
                    continue
                if self.database_status["state"] != "ready":
                    log.info("Playback scheduling resumed after database contention")
                    self.database_status = {"state": "ready", "consecutive_failures": 0,
                                            "recovered_at": utc()}
                if row:
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

    async def dispatch_work(self, cancellations, row):
        if cancellations:
            for cancel in cancellations:
                if cancel["next_check"] <= time.time():
                    try:
                        await self.cancel_one(cancel["token"])
                    except ExecutorError as error:
                        def record_cancel_error(error=error):
                            with self.db.transaction() as db:
                                job = self.store.get_row(db, cancel["token"])
                                report = json.loads(job["report"])
                                report["observation_status"].update(
                                    state="unavailable", checked_at=utc(), error=error.detail()
                                )
                                self.store.write(db, job["token"], report, next_check=time.time() + 5)
                        await self.database_call(record_cancel_error)
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
