"""One durable device worker, with cancellation serialized at physical input."""

import asyncio
import concurrent.futures
import fcntl
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from ..planner import parse_time
from .adb import PACKAGE, AdbDevice
from .models import ExecutorError
from .store import ExecutorStore, utc
from .verification import (
    activation_allowed,
    completed,
    evidence,
    playback_sample,
    prime_option,
    progression,
    search_queries,
)
from .vision import VisionClient

log = logging.getLogger(__name__)


class PlaybackExecutor:
    def __init__(self, database, settings, *, device=None, vision=None):
        self.db, self.settings, self.config = database, settings, settings.executor
        self.store = ExecutorStore(database, settings)
        self.device = device or AdbDevice(settings)
        self.vision = vision
        self.input_lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.loop = self.worker = self.active = self.active_token = self.guard = None
        self.stopping = False

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
        self.vision = self.vision or VisionClient(self.config)
        self.store.recover()
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
        if self.vision:
            await self.vision.close()
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

    async def input(self, token, action, call, *, frame=None, cancelling=False):
        async with self.input_lock:
            if not cancelling:
                with self.db.transaction() as db:
                    count = db.execute(
                        "SELECT COUNT(*) FROM executor_actions WHERE token=?", (token,)
                    ).fetchone()[0]
                if count >= self.config.max_actions:
                    raise ExecutorError(
                        "action_budget", "Playback reached its bounded input budget", retryable=False
                    )
            if frame and (datetime.now(UTC) - frame.captured_at).total_seconds() > self.config.frame_max_age:
                raise ExecutorError("stale_navigation_frame", "Navigation evidence expired before input")
            if frame:
                foreground, _ = await self.device.state()
                if foreground != PACKAGE:
                    raise ExecutorError("foreground_changed", "Application changed before input")
            action_id = self.store.journal(
                token, action, frame.sha256 if frame else None, cancelling=cancelling
            )
            if action_id is None:
                return
            task = asyncio.create_task(call())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Drain bounded device operations before releasing the shared gate.
                # Repeated cancellation (e.g. another Cancel during shutdown)
                # must not release the gate while an input subprocess is alive.
                drained = asyncio.gather(task, return_exceptions=True)
                while not drained.done():
                    try:
                        await asyncio.shield(drained)
                    except asyncio.CancelledError:
                        continue
                self.store.journal_done(action_id, "interrupted_delivery")
                raise
            except Exception:
                self.store.journal_done(action_id, "delivery_unconfirmed")
                raise
            else:
                self.store.journal_done(action_id)

    async def cancel_one(self, token):
        with self.db.transaction() as db:
            row = self.store.get_row(db, token)
            owner = db.execute(
                "SELECT current_token FROM executor_devices WHERE device_id=?", (row["device_id"],)
            ).fetchone()
            owns = bool(owner and owner[0] == token and row["touched_device"])
        if owns:
            await self.device.ready()
            await self.input(token, "STOP_PRIME", self.device.stop, cancelling=True)
        self.store.acknowledge_cancel(token, owns)

    def context(self, row):
        request = json.loads(row["request"])
        with self.db.transaction() as db:
            timezone = self.db.device(db, row["device_id"])["preferences"]["timezone"]
        return request, timezone

    async def read_scene(self, token, *, navigation=False):
        with self.db.transaction() as db:
            self.store.allowed(db, token, navigation=navigation)
        frame = await self.device.capture()
        scene = await self.vision.observe(frame)
        age = (datetime.now(UTC) - frame.captured_at).total_seconds()
        if age > self.config.frame_max_age:
            raise ExecutorError("stale_observation", "Vision reading exceeded the observation age limit")
        with self.db.transaction() as db:
            self.store.allowed(db, token, navigation=navigation)
        return frame, scene

    async def navigate(self, row):
        token = row["token"]
        request, timezone = self.context(row)
        try:
            budget = (parse_time(request["deadline_at"]) - datetime.now(UTC)).total_seconds()
            if budget <= 0:
                raise ExecutorError("navigation_expired", "Navigation deadline passed", retryable=False)
            async with asyncio.timeout(budget):
                await self.device.ready()
                options = request["allowed_viewing_options"]
                if not any(prime_option(option) for option in options):
                    raise ExecutorError(
                        "unsupported_routes",
                        "No supplied viewing option uses the supported Prime Video app",
                        retryable=False,
                    )
                await self.input(token, "WAKEUP", lambda: self.device.key("WAKEUP"))
                await self.input(token, "LAUNCH_PRIME", self.device.launch)
                attempt = 0
                for option in options:
                    if not prime_option(option):
                        self.store.phase(
                            token,
                            "unsupported_route",
                            attempt={
                                "attempt": attempt + 1,
                                "viewing_option_id": option["id"],
                                "phase": "unsupported_app",
                                "started_at": utc(),
                                "finished_at": utc(),
                                "error": {
                                    "code": "unsupported_app",
                                    "message": "App is not supported by this executor",
                                    "retryable": False,
                                },
                            },
                        )
                        attempt += 1
                        continue
                    for query in search_queries(request["content_snapshot"]):
                        attempt += 1
                        self.store.phase(
                            token,
                            "searching",
                            attempt={
                                "attempt": attempt,
                                "viewing_option_id": option["id"],
                                "phase": "searching",
                                "started_at": utc(),
                                "finished_at": None,
                                "error": None,
                            },
                        )
                        await self.input(token, "SEARCH_PRIME", lambda: self.device.search(query))
                        await asyncio.sleep(self.config.settle_seconds)
                        outcome = await self.navigate_query(token, request, option, query, timezone)
                        if outcome:
                            return
                raise ExecutorError(
                    "target_unresolved",
                    "Requested live content could not be verified within the supplied routes",
                    retryable=False,
                )
        except asyncio.CancelledError:
            if self.stopping:
                self.store.fail(
                    token, ExecutorError("shutdown_interrupted", "Navigation interrupted by shutdown")
                )
            raise
        except TimeoutError:
            self.store.fail(
                token,
                ExecutorError("navigation_timeout", "Navigation reached its fixed deadline", retryable=False),
            )
        except ExecutorError as error:
            self.store.fail(token, error)
        except Exception:
            log.exception("Navigation failed")
            self.store.fail(
                token,
                ExecutorError(
                    "executor_internal", "Navigation stopped after an internal error", retryable=False
                ),
            )

    async def navigate_query(self, token, request, option, query, timezone):
        history, feedback, repeated, prior_signature = [], None, 0, None
        proof = False
        for _ in range(self.config.max_actions):
            frame, scene = await self.read_scene(token, navigation=True)
            if scene.blocker in {"signin", "purchase", "profile"}:
                raise ExecutorError(
                    "needs_user_action",
                    "Prime Video requires sign-in, a profile choice, or subscription access",
                    retryable=False,
                )
            if scene.blocker == "error":
                return False
            if scene.surface == "search":
                if scene.current_query and scene.current_query.casefold().strip() != query.casefold().strip():
                    raise ExecutorError(
                        "search_mismatch", "Prime Video displayed a different search query", retryable=False
                    )
                if scene.search_state == "no_results":
                    return False
                if scene.search_state in {"loading", "empty", "unknown"}:
                    await asyncio.sleep(max(self.config.settle_seconds, 0.5))
                    continue
            if proof and playback_sample(scene, frame, request, timezone):
                await asyncio.sleep(max(1, self.config.settle_seconds))
                second = await self.read_scene(token, navigation=True)
                if playback_sample(second[1], second[0], request, timezone) and progression(
                    (frame, scene), second
                ):
                    if self.save_observation(token, request, option, second, verified=True):
                        return True
            action = await self.vision.decide(frame, request, option, history, feedback)
            signature = (scene.surface, scene.focus.model_dump_json(), action)
            repeated = repeated + 1 if signature == prior_signature else 1
            prior_signature = signature
            if repeated >= 4:
                return False
            feedback = None
            if action in {"WAIT", "FINISH"}:
                feedback = "Playback has not passed independent live/content/progression verification."
                await asyncio.sleep(max(1, self.config.settle_seconds))
                continue
            if action == "SELECT":
                if not activation_allowed(scene, request, option, frame, timezone):
                    feedback = "Activation rejected: focus, live status, identity or permitted route was not established."
                    continue
                # A second independent capture must agree on the focused action.
                second_frame, second_scene = await self.read_scene(token, navigation=True)
                if second_scene.focus != scene.focus or not activation_allowed(
                    second_scene, request, option, second_frame, timezone
                ):
                    feedback = "Focus changed or could not be confirmed. Observe and navigate again."
                    continue
                frame = second_frame
                proof = proof or scene.focus.role in {"event", "play_live"}
            self.store.phase(token, "verifying" if proof else "navigating")
            await self.input(token, action, lambda: self.device.key(action), frame=frame)
            history.append((frame, action))
            await asyncio.sleep(self.config.settle_seconds)
        return False

    def save_observation(self, token, request, option, sample, *, verified):
        frame, scene = sample
        now = datetime.now(UTC)
        if (now - frame.captured_at).total_seconds() >= self.settings.observation_ttl:
            return False
        observation = {
            "device_id": request["device_id"],
            "request_id": request["request_id"],
            "content_id": request["content_id"],
            "intent_version": request["intent_version"],
            "viewing_option_id": option["id"],
            "presentation": "live",
            "verified": verified,
            "simulated": False,
            "health": "healthy",
            "observed_at": frame.captured_at.isoformat(),
            "valid_until": (frame.captured_at + timedelta(seconds=self.settings.observation_ttl)).isoformat(),
            "evidence": evidence(
                frame, "Matched requested content and live edge; two sampled playback positions advanced"
            ),
        }
        with self.db.transaction() as db:
            row = self.store.allowed(db, token)
            report = json.loads(row["report"])
            report["operation"].update(
                state="playing_verified",
                phase="verified",
                updated_at=utc(),
                finished_at=report["operation"]["finished_at"] or utc(),
                error=None,
            )
            if report["operation"]["attempts"]:
                report["operation"]["attempts"][-1].update(phase="verified", finished_at=utc())
            report["observation"] = observation
            report["observation_status"] = {"state": "fresh", "checked_at": utc(), "error": None}
            self.store.write(
                db,
                token,
                report,
                state="playing_verified",
                next_check=time.time() + self.config.monitor_interval,
            )
        return True

    async def monitor(self, row):
        token = row["token"]
        request, timezone = self.context(row)
        try:
            first = await self.read_scene(token)
            if completed(first[1], first[0], request, timezone):
                self.record_completion(token, request, first)
                self.unverified(token, None)
                return
            self.clear_completion_candidate(token)
            report = self.store.report(token)
            old = report["observation"] or {}
            option = next(
                (o for o in request["allowed_viewing_options"] if o["id"] == old.get("viewing_option_id")),
                None,
            )
            if option and playback_sample(first[1], first[0], request, timezone):
                await asyncio.sleep(max(1, self.config.settle_seconds))
                second = await self.read_scene(token)
                if playback_sample(second[1], second[0], request, timezone) and progression(first, second):
                    if self.save_observation(token, request, option, second, verified=True):
                        return
            self.unverified(token, None)
        except ExecutorError as error:
            self.clear_completion_candidate(token)
            self.unverified(token, error)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Playback observation failed")
            self.clear_completion_candidate(token)
            self.unverified(
                token, ExecutorError("observation_failed", "Playback observation could not be refreshed")
            )

    def unverified(self, token, error):
        with self.db.transaction() as db:
            row = self.store.get_row(db, token)
            if row["cancel_requested"]:
                return
            report = json.loads(row["report"])
            if report["observation"]:
                report["observation"]["verified"] = False
            report["observation_status"] = {
                "state": "unavailable",
                "checked_at": utc(),
                "error": error.detail() if error else None,
            }
            self.store.write(db, token, report, next_check=time.time() + self.config.monitor_interval)

    def clear_completion_candidate(self, token):
        with self.db.transaction() as db:
            db.execute(
                "UPDATE executor_jobs SET completion_candidate=NULL WHERE token=? AND cancel_requested=0",
                (token,),
            )

    def record_completion(self, token, request, sample):
        frame, scene = sample
        if (datetime.now(UTC) - frame.captured_at).total_seconds() >= self.settings.status_ttl:
            return
        with self.db.transaction() as db:
            row = self.store.allowed(db, token)
            previous = json.loads(row["completion_candidate"]) if row["completion_candidate"] else None
            candidate = {
                "at": frame.captured_at.isoformat(),
                "identity": scene.completion.identity.model_dump(),
                "text": scene.completion.final_text,
                "hash": frame.sha256,
            }
            if not previous or previous["identity"] != candidate["identity"]:
                db.execute(
                    "UPDATE executor_jobs SET completion_candidate=? WHERE token=?",
                    (json.dumps(candidate), token),
                )
                return
            elapsed = (frame.captured_at - parse_time(previous["at"])).total_seconds()
            if not self.config.completion_interval <= elapsed <= self.settings.status_ttl:
                if elapsed > self.settings.status_ttl:
                    db.execute(
                        "UPDATE executor_jobs SET completion_candidate=? WHERE token=?",
                        (json.dumps(candidate), token),
                    )
                return
            report = json.loads(row["report"])
            observation = {
                "content_id": request["content_id"],
                "state": "ended",
                "source": "prime_video_visual",
                "simulated": False,
                "timestamp_basis": "device_observed",
                "observed_at": frame.captured_at.isoformat(),
                "received_at": utc(),
                "valid_until": (frame.captured_at + timedelta(seconds=self.settings.status_ttl)).isoformat(),
                "evidence": evidence(
                    frame,
                    "Two independent readings confirm: " + scene.completion.final_text,
                    decision="confirmed",
                ),
            }
            report["content_status"] = {
                "content_id": request["content_id"],
                "lookup_state": "ok",
                "effective_state": "ended",
                "stale": False,
                "observation": observation,
                "checked_at": utc(),
                "error": None,
            }
            self.store.write(db, token, report, next_check=time.time() + self.config.monitor_interval)
