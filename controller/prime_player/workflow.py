"""Durable event-to-Prime orchestration; no ADB, vision, or app execution engine.

Search/selection/launch run once after a scheduling decision. Monitoring is read
only and always scoped to the saved service session and playback attempt.
"""

import asyncio
import json
import math
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ..executor.models import ExecutorError
from ..executor.store import utc
from ..executor.worker import PlaybackWorker
from ..model_diagnostics import capture_model_calls
from ..planner import parse_time
from .client import PrimePlayerClient, correlated
from .labels import search_queries
from .matching import EventMatcher


class PrimePlaybackWorkflow(PlaybackWorker):
    def __init__(self, database, settings, *, player=None, matcher=None):
        super().__init__(database, settings)
        self.player = player or PrimePlayerClient(self.config.prime_socket)
        self.matcher = matcher or EventMatcher(self.config)

    async def close_resources(self):
        # Closing the client never restarts, detaches, or terminates Prime Player.
        await self.matcher.close()
        await self.player.close()

    def recover(self):
        with self.db.transaction() as db:
            for row in db.execute(
                "SELECT * FROM executor_jobs WHERE request IS NOT NULL AND cancel_requested=0"
            ).fetchall():
                report = json.loads(row["report"])
                workflow = report.get("prime_player") or {}
                if report["observation"]:
                    report["observation"]["verified"] = False
                report["observation_status"]["state"] = "unavailable"
                if row["touched_device"] and not workflow.get("attempt_id"):
                    # An interrupted search has no playback attempt to reconcile.
                    # Quiesce it before any new search; do not repeat on startup.
                    report["operation"].update(
                        state="failed",
                        phase="restart_interrupted",
                        finished_at=utc(),
                        error=ExecutorError(
                            "restart_interrupted", "Search interrupted by controller restart"
                        ).detail(),
                    )
                    self.store.write(
                        db, row["token"], report, state="failed", cancel_requested=1, next_check=0
                    )
                else:
                    self.store.write(db, row["token"], report, next_check=0)
            db.execute(
                "UPDATE executor_actions SET state='uncertain',error='process_restart' WHERE state='dispatching'"
            )

    def save_workflow(self, token, **fields):
        with self.db.transaction() as db:
            row = self.store.allowed(db, token)
            report = json.loads(row["report"])
            report.setdefault("prime_player", {}).update(fields)
            self.store.write(db, token, report)

    def save_model_call(self, token, evidence):
        # Diagnostic writes must survive cancellation without granting navigation
        # authority or changing the job's state/cancellation fields.
        with self.db.transaction() as db:
            row = self.store.get_row(db, token)
            if row["request"] is None:
                return
            report = json.loads(row["report"])
            prime = report.setdefault("prime_player", {})
            calls = [c for c in prime.get("model_calls", []) if c["call_id"] != evidence["call_id"]]
            prime["model_calls"] = [*calls, evidence][-3:]
            self.store.write(db, token, report)

    async def mutation(self, token, action, function):
        async with self.input_lock:
            action_id = self.store.journal(token, action)
            task = asyncio.create_task(function())
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Stop remote work out of band: search holds input_lock locally,
                # but the service cancellation lane can interrupt its runtime.
                cleanup = asyncio.create_task(self.interrupt(token))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                # A failed barrier remains a durable cancellation obligation.
                await asyncio.gather(cleanup, return_exceptions=True)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.store.journal_done(action_id, "interrupted_delivery")
                raise
            except Exception:
                self.store.journal_done(action_id, "delivery_unconfirmed")
                raise
            else:
                self.store.journal_done(action_id)
                with self.db.transaction() as db:
                    self.store.allowed(db, token, navigation=True)
                return result

    async def check_session(self, expected=None, *, capabilities=(), control=False):
        health = await (self.player.control_health() if control else self.player.health())
        if expected and health["session_id"] != expected:
            raise ExecutorError(
                "prime_session_changed",
                "Prime Player restarted; the old attempt must not be replayed",
                retryable=False,
            )
        self.player.require(health, *capabilities)
        if health.get("serial") != self.settings.screen_adb_serial:
            raise ExecutorError("prime_device_mismatch", "Prime Player serial differs from SCREEN_ADB_SERIAL")
        return health

    @correlated
    async def navigate(self, row):
        token, request = row["token"], json.loads(row["request"])
        try:
            budget = (parse_time(request["deadline_at"]) - datetime.now(UTC)).total_seconds()
            if budget <= 0:
                raise ExecutorError("navigation_expired", "Playback workflow deadline passed")
            async with asyncio.timeout(budget):
                workflow = self.store.report(token).get("prime_player") or {}
                if not workflow.get("attempt_id"):
                    await self.select_and_launch(token, request)
                # Even after a lost acknowledgement/restart, only inspect the
                # original attempt. Never resend Play, switch to GTI, or re-search.
                await self.verify_launch(token)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.store.fail(token, ExecutorError("navigation_timeout", "Prime playback workflow timed out"))
        except ExecutorError as exc:
            self.store.fail(token, exc)
        except Exception:
            self.store.fail(token, ExecutorError("prime_workflow_failed", "Prime playback workflow failed"))

    async def select_and_launch(self, token, request):
        health = await self.check_session(
            capabilities=("search", "play", "playback_status", "cancel", "stop")
        )
        ownership = self.player.ownership(health, automatic=True)
        session = health["session_id"]
        with self.db.transaction() as db:
            self.store.allowed(db, token, navigation=True)
            timezone = self.db.device(db, request["device_id"])["preferences"]["timezone"]
        query = search_queries(request["content_snapshot"])[0]
        self.save_workflow(
            token, session_id=session, query=query, source="prime_player", selection=None, ownership=ownership
        )
        self.store.phase(token, "searching")
        results = await self.mutation(
            token,
            "PRIME_SEARCH",
            lambda: self.player.search(query, self.config.prime_search_timeout, ownership),
        )
        if results.get("session_id") != session or results.get("query") != query:
            raise ExecutorError("prime_stale_result", "Search result belongs to another session or query")
        # Local monotonic age is independent of host clock skew and cannot extend
        # the service's own expiring handle. The service remains the final check.
        received = time.monotonic()
        self.store.phase(token, "matching")
        with capture_model_calls(lambda evidence: self.save_model_call(token, evidence)):
            selected, audit = await self.matcher.choose(request, results, timezone)
        self.save_workflow(
            token,
            selection=audit,
            search_id=results.get("search_id"),
            search_generation=results.get("generation"),
            search_observed_at=results.get("observed_at"),
        )
        if not selected:
            raise ExecutorError("prime_no_match", audit["reason"], retryable=False)
        if time.monotonic() - received > 90:
            raise ExecutorError("prime_stale_result", "Result selection outlived its usable handle")
        health = await self.check_session(session, capabilities=("play",))
        ownership = self.player.ownership(health, automatic=True)
        attempt = uuid4().hex
        self.save_workflow(
            token, attempt_id=attempt, selected=selected, launch_outcome=None, ownership=ownership
        )
        self.store.phase(token, "launching")
        try:
            outcome = await self.mutation(
                token, "PRIME_PLAY", lambda: self.player.play(selected["handle"], attempt, ownership)
            )
            self.validate_outcome(outcome, self.store.report(token)["prime_player"])
            self.save_workflow(token, launch_outcome=outcome)
        except ExecutorError as exc:
            # Delivery uncertainty is resolved by read-only inspection, not a
            # fresh request or a repeated handle which Play may have invalidated.
            if exc.code not in {"prime_transport_unknown", "prime_operation_unknown"}:
                raise
            self.save_workflow(token, launch_error=exc.detail())

    @staticmethod
    def validate_outcome(outcome, workflow):
        if (
            outcome.get("session_id") != workflow["session_id"]
            or outcome.get("attempt_id") != workflow["attempt_id"]
            or outcome.get("requested_id") != workflow["selected"]["content_id"]
        ):
            raise ExecutorError(
                "prime_attempt_mismatch", "Prime attempt identity does not match the selection"
            )
        resolution = outcome.get("evidence", {}).get("resolution", {})
        if resolution and resolution.get("playbackClass") != "live_watch_now":
            raise ExecutorError(
                "prime_non_live_resolution", "Prime did not resolve the live Watch Now action"
            )

    async def verify_launch(self, token):
        self.store.phase(token, "verifying")
        while True:
            with self.db.transaction() as db:
                self.store.allowed(db, token, navigation=True)
            workflow = self.store.report(token)["prime_player"]
            outcome = workflow.get("launch_outcome") or {}
            if outcome.get("state") != "playing":
                await self.check_session(workflow["session_id"])
                outcome = await self.player.attempt(workflow["attempt_id"])
            self.validate_outcome(outcome, workflow)
            self.save_workflow(token, launch_outcome=outcome)
            if outcome["state"] in {"failed", "unknown", "cancelled", "stopped"}:
                reason = outcome.get("reason")
                message = "Prime could not verify the requested playback"
                if isinstance(reason, str) and reason.strip():
                    message += ": " + reason.strip()[:800]
                raise ExecutorError(
                    "prime_launch_unverified", message
                )
            if outcome["state"] == "playing":
                resolution = outcome.get("evidence", {}).get("resolution", {})
                if resolution.get("playbackClass") != "live_watch_now" or not outcome.get("resolved_id"):
                    raise ExecutorError(
                        "prime_non_live_resolution", "Verified launch lacks live resolution evidence"
                    )
                self.record_launch(token, outcome)
                return
            await asyncio.sleep(2)

    def record_launch(self, token, outcome):
        """Accept Prime Player's startup proof; current status belongs to monitoring."""
        now = datetime.now(UTC)
        with self.db.transaction() as db:
            row = self.store.allowed(db, token, navigation=True)
            report = json.loads(row["report"])
            workflow = report["prime_player"]
            self.validate_outcome(outcome, workflow)
            report["observation"] = {
                "device_id": row["device_id"],
                "request_id": row["request_id"],
                "intent_version": row["intent"],
                "content_id": row["content_id"],
                "viewing_option_id": workflow["selected"]["viewing_option_id"],
                "presentation": "live",
                "verified": True,
                "simulated": False,
                "health": "healthy",
                "observed_at": now.isoformat(),
                "valid_until": (now + timedelta(seconds=self.settings.playback_evidence_ttl)).isoformat(),
                "evidence": {
                    "method": "device_observation",
                    "evidence_id": f"prime:{workflow['session_id']}:{workflow['attempt_id']}",
                    "summary": "Prime Player verified live startup; timestamp is result receipt time",
                    "confidence": None,
                    "captured_at": now.isoformat(),
                },
            }
            report["operation"].update(
                state="playing_verified",
                phase="verified",
                updated_at=utc(),
                finished_at=utc(),
                error=None,
            )
            report["observation_status"] = {"state": "fresh", "checked_at": utc(), "error": None}
            self.db.log(
                db,
                utc(),
                "Prime playback verified",
                "Prime Player accepted live startup",
                "verified",
                row["device_id"],
            )
            self.store.write(
                db,
                token,
                report,
                state="playing_verified",
                next_check=time.time() + self.config.monitor_interval,
            )

    def record_status(self, token, status):
        now = datetime.now(UTC)
        with self.db.transaction() as db:
            row = self.store.allowed(db, token)
            report = json.loads(row["report"])
            previously_verified = bool((report.get("observation") or {}).get("verified"))
            workflow = report["prime_player"]
            launch = workflow.get("launch_outcome") or {}
            if (
                status.get("session_id") != workflow["session_id"]
                or status.get("attempt_id") != workflow["attempt_id"]
                or status.get("requested_id") != workflow["selected"]["content_id"]
                or status.get("resolved_id") != launch.get("resolved_id")
            ):
                raise ExecutorError(
                    "prime_status_mismatch", "Current status belongs to another session or attempt"
                )
            workflow["playback_status"] = status
            observed, fresh = None, False
            try:
                age = status.get("observation_age_seconds")
                if type(age) in {float, int} and math.isfinite(age) and age >= 0:
                    observed = parse_time(status["observed_at"]) - timedelta(seconds=age)
                    fresh = 0 <= (now - observed).total_seconds() < self.settings.observation_ttl
            except (ValueError, TypeError, KeyError, OverflowError):
                pass
            verified = bool(
                fresh
                and launch.get("state") == "playing"
                and launch.get("evidence", {}).get("resolution", {}).get("playbackClass") == "live_watch_now"
                and status.get("state") == "playing"
                and status.get("is_playing") is True
                and status.get("matches_attempt") is True
                and status.get("current_content_id") == launch.get("resolved_id")
            )
            if verified:
                report["observation"] = {
                    "device_id": row["device_id"],
                    "request_id": row["request_id"],
                    "intent_version": row["intent"],
                    "content_id": row["content_id"],
                    "viewing_option_id": workflow["selected"]["viewing_option_id"],
                    "presentation": "live",
                    "verified": True,
                    "simulated": False,
                    "health": "healthy",
                    "observed_at": observed.isoformat(),
                    "valid_until": (observed + timedelta(seconds=self.settings.playback_evidence_ttl)).isoformat(),
                    "evidence": {
                        "method": "device_observation",
                        "evidence_id": f"prime:{workflow['session_id']}:{workflow['attempt_id']}",
                        "summary": "Selected live event; Prime verified live resolution and current bound playback progression",
                        "confidence": None,
                        "captured_at": observed.isoformat(),
                    },
                }
                report["operation"].update(
                    state="playing_verified",
                    phase="verified",
                    updated_at=utc(),
                    finished_at=report["operation"]["finished_at"] or utc(),
                    error=None,
                )
            elif report["observation"]:
                report["observation"]["verified"] = False
            reason = (
                None
                if verified
                else (
                    str(status.get("error"))[:500]
                    if status.get("error")
                    else "Prime playback evidence is stale"
                    if not fresh
                    else "Prime playback is not verified: " + str(status.get("state", "unknown"))
                )
            )
            report["observation_status"] = {
                "state": "fresh" if verified else "unavailable",
                "checked_at": utc(),
                "error": None if verified else ExecutorError("prime_playback_unverified", reason).detail(),
            }
            if previously_verified and not verified:
                self.db.log(
                    db, utc(), "Playback monitoring lost verification", reason, "recovery", row["device_id"]
                )
            elif verified and not previously_verified and report["operation"]["finished_at"]:
                self.db.log(
                    db,
                    utc(),
                    "Prime playback verified",
                    "Current attempt-bound playback is playing",
                    "verified",
                    row["device_id"],
                )
            # Even explicit player Ended is not a sports-event completion fact.
            # Leave content_status to Teamarr/manual lifecycle evidence.
            self.store.write(
                db,
                token,
                report,
                state="playing_verified" if verified else row["state"],
                next_check=time.time() + self.config.monitor_interval,
            )
            return verified

    @correlated
    async def monitor(self, row):
        token = row["token"]
        try:
            workflow = self.store.report(token)["prime_player"]
            await self.check_session(workflow["session_id"], capabilities=("playback_status",))
            status = await self.player.status(workflow["attempt_id"], self.config.prime_status_timeout)
            self.record_status(token, status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, ExecutorError)
                else ExecutorError("prime_status_unknown", "Status unavailable")
            )
            with self.db.transaction() as db:
                row = self.store.get_row(db, token)
                report = json.loads(row["report"])
                if report["observation"] and report["observation"].get("verified"):
                    self.db.log(
                        db,
                        utc(),
                        "Playback monitoring unavailable",
                        error.message,
                        "recovery",
                        row["device_id"],
                    )
                if report["observation"]:
                    report["observation"]["verified"] = False
                report["observation_status"] = {
                    "state": "unavailable",
                    "checked_at": utc(),
                    "error": error.detail(),
                }
                self.store.write(db, token, report, next_check=time.time() + self.config.monitor_interval)

    def save_cancellation(self, token, **fields):
        with self.db.transaction() as db:
            row = self.store.get_row(db, token)
            report = json.loads(row["report"])
            report.setdefault("prime_player", {}).update(fields)
            self.store.write(db, token, report)

    def prepare_recovery(self, db, request_id, after):
        """Require a completed read-only check before replacing the old attempt.

        Called in the coordinator's staging transaction. Queue the check on the
        existing worker; never cancel an in-flight monitor or perform RPC here.
        """
        row = db.execute("SELECT * FROM executor_jobs WHERE request_id=?", (request_id,)).fetchone()
        if not row or row["cancel_requested"] or row["state"] != "playing_verified":
            return True
        report = json.loads(row["report"])
        checked = parse_time(report["observation_status"].get("checked_at"))
        in_flight = self.active_token == row["token"] and self.active and not self.active.done()
        recent = checked and 0 <= (datetime.now(UTC) - checked).total_seconds() < self.settings.observation_ttl
        if recent and checked >= after and not in_flight:
            # A resumed result may arrive between the coordinator's observation
            # read and this staging transaction. Let the next tick adopt it.
            return not bool((report.get("observation") or {}).get("verified"))
        if not in_flight:
            db.execute("UPDATE executor_jobs SET next_check=0 WHERE token=?", (row["token"],))
            self.notify()
        return False

    @correlated
    async def interrupt(self, token, *, force=False):
        """Fence remote work without waiting on the local mutation lock."""
        report = self.store.report(token)
        workflow = report.get("prime_player") or {}
        health = await self.check_session(control=True, capabilities=("cancel",))
        receipt = self.player.control_ownership(health)
        if workflow.get("session_id") != health["session_id"]:
            # An old service token cannot address the new service lifetime.
            return self.player.ownership(health)
        previous = workflow.get("ownership") or {}
        if receipt.get("acknowledged") is True and (
            receipt["mode"] == "manual"
            or (not force and any(receipt.get(k) != previous.get(k) for k in ("epoch", "handoff_id")))
        ):
            # A newer acknowledged transition has already fenced this work.
            return receipt
        result = await self.player.cancel_work(workflow["session_id"], workflow.get("attempt_id"), receipt)
        acknowledged = self.player.ownership({"session_id": health["session_id"], "ownership": result})
        self.save_cancellation(token, cancellation_receipt=acknowledged)
        return acknowledged

    @correlated
    async def cancel_one(self, token):
        async with self.input_lock:
            with self.db.transaction() as db:
                row = self.store.get_row(db, token)
                if row["cancel_requested"] == 2:
                    return
                report = json.loads(row["report"])
                owner = db.execute(
                    "SELECT current_token FROM executor_devices WHERE device_id=?", (row["device_id"],)
                ).fetchone()
                owns = bool(owner and owner[0] == token and row["touched_device"])
            active = "not_current" if row["touched_device"] else "already_inactive"
            if owns:
                workflow = report.get("prime_player") or {}
                receipt = await self.interrupt(token)
                attempt = workflow.get("attempt_id")
                if workflow.get("session_id") != receipt["session_id"] or receipt["mode"] != "automatic":
                    active = "not_current"
                elif attempt:
                    active = "unknown"
                    # Persist before dispatch. A lost response must never replay
                    # a physical Back/stop action after restart or retry.
                    if not workflow.get("stop_dispatched"):
                        self.save_cancellation(token, stop_dispatched=True)
                        try:
                            result = await self.player.stop_attempt(workflow["session_id"], attempt, receipt)
                        except ExecutorError as exc:
                            self.save_cancellation(token, stop_error=exc.detail())
                            # The preceding barrier is insufficient after a lost
                            # stop response: fence/drain that operation as well.
                            await self.interrupt(token, force=True)
                        else:
                            self.save_cancellation(token, stop_result=result)
                            if result.get("stopped") is True and (
                                result.get("session_id") == workflow["session_id"]
                                and result.get("attempt_id") == attempt
                            ):
                                active = "stopped"
                            elif result.get("reason") == "attempt_not_current":
                                active = "not_current"
                    else:
                        result = workflow.get("stop_result") or {}
                        if result.get("stopped") is True and (
                            result.get("session_id") == workflow["session_id"]
                            and result.get("attempt_id") == attempt
                        ):
                            active = "stopped"
                        elif result.get("reason") == "attempt_not_current":
                            active = "not_current"
                        else:
                            # Recover uncertain stop by fencing input, not replaying
                            # stop. A native stop confirmation remains unknown.
                            await self.interrupt(token, force=True)
                else:
                    # Search can replace existing content; no bound attempt exists
                    # with which to prove what is playing or stopped.
                    active = "unknown"
            self.store.acknowledge_cancel(token, active)
