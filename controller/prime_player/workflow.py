"""Durable event-to-Prime orchestration; no ADB, vision, or app execution engine.

Catalogue checks precede playback intent changes. Launches use fresh resolution;
monitoring stays scoped to the saved service session and playback attempt.
"""

import asyncio
import json
import math
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ..database import encode
from ..executor.models import ExecutorError
from ..executor.store import utc
from ..executor.worker import PlaybackWorker
from ..model_diagnostics import capture_model_calls
from ..planner import parse_time
from .broadcasts import choose_broadcast
from .catalogue import CatalogueChecks
from .client import PrimePlayerClient, correlated
from .labels import search_queries
from .matching import EventMatcher, selection_state


class PrimePlaybackWorkflow(CatalogueChecks, PlaybackWorker):
    def __init__(self, database, settings, *, player=None, matcher=None):
        super().__init__(database, settings)
        self.player = player or PrimePlayerClient(self.config.prime_socket)
        self.matcher = matcher or EventMatcher(self.config)

    async def close_resources(self):
        # Closing the client never restarts, detaches, or terminates Prime Player.
        recovery_task = getattr(self, "player_recovery_task", None)
        if recovery_task and not recovery_task.done():
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)
        task = getattr(self, "catalogue_task", None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.matcher.close()
        await self.player.close()

    def recover(self):
        with self.db.transaction() as db:
            db.execute("DELETE FROM metadata WHERE key LIKE 'prime_catalogue_probe:%'")
            if self.db.meta(db, "prime_catalogue_contract") != 13:
                for row in db.execute("SELECT id FROM devices").fetchall():
                    device = self.db.device(db, row[0])
                    device["prime_access"] = {}
                    self.db.save_device(db, device)
                self.db.set_meta(db, "prime_catalogue_contract", 13)
            for row in db.execute(
                "SELECT * FROM executor_jobs WHERE request IS NOT NULL AND cancel_requested=0"
            ).fetchall():
                report = json.loads(row["report"])
                workflow = report.get("prime_player") or {}
                if report["observation"]:
                    report["observation"]["verified"] = False
                report["observation_status"]["state"] = "unavailable"
                if report["operation"]["state"] in {
                    "waiting_for_feed",
                    "access_unknown",
                    "feeds_locked",
                    "no_matching_feed",
                }:
                    continue
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

    def record_player_health(self, health):
        inspection = health.get("playback_inspection") or {}
        degraded = inspection.get("state") == "degraded"
        with self.db.transaction() as db:
            device = self.db.device(db)
            previous = device.get("player_recovery")
            if not degraded and not previous:
                return False
            if degraded:
                reason = (
                    "Player inspection cooling down; checking recovery every 60 seconds. Watch plan retained."
                    if inspection.get("recovery") == "retry_after_cooldown" else
                    "Player inspection unavailable; checking recovery every 60 seconds. Watch plan retained."
                )
                device["player_recovery"] = {
                    "session_id": health["session_id"], "inspection": inspection,
                    "reason": reason, "checked_at": utc(),
                    "next_probe_at": time.time() + 60,
                }
                device["reason"] = reason
                if device.get("observed"):
                    device["observed"]["verified"] = False
                if not previous:
                    self.db.log(db, utc(), "Player recovery pending", reason, "recovery", device["id"])
            elif previous:
                device["player_recovery"] = None
                self.db.log(db, utc(), "Player inspection available",
                            "Reevaluating current intent and live status before any new launch",
                            "recovery", device["id"])
            self.db.save_device(db, device)
        return degraded

    def player_recovery_ready(self, db, device):
        recovery = device.get("player_recovery")
        adaptation = device.get("prime_runtime_adaptation") or {}
        task = getattr(self, "player_recovery_task", None)
        due = [
            value for value in (recovery, adaptation)
            if value and value.get("next_probe_at") is not None and value["next_probe_at"] <= time.time()
        ]
        if due and (not task or task.done()):
            for value in due:
                value["next_probe_at"] = time.time() + 60
            self.db.save_device(db, device)
            if self.loop and self.loop.is_running() and not self.stopping:
                def schedule():
                    task = getattr(self, "player_recovery_task", None)
                    if not task or task.done():
                        self.player_recovery_task = asyncio.create_task(self.probe_player_recovery())
                self.loop.call_soon_threadsafe(schedule)
        if not recovery:
            return True
        device["reason"] = recovery["reason"]
        self.db.save_device(db, device)
        return False

    async def probe_player_recovery(self):
        try:
            async with asyncio.timeout(15):
                async with self.input_lock:
                    await self.check_session()
        except (ExecutorError, TimeoutError):
            pass  # Retain recovery and its next probe; never turn this into event failure.

    async def check_session(self, expected=None, *, capabilities=(), control=False):
        health = await (self.player.control_health() if control else self.player.health())
        if expected and health["session_id"] != expected:
            raise ExecutorError(
                "prime_session_changed",
                "Prime Player restarted; the old attempt must not be replayed",
                retryable=False,
            )
        if health.get("serial") != self.settings.screen_adb_serial:
            raise ExecutorError("prime_device_mismatch", "Prime Player serial differs from SCREEN_ADB_SERIAL")
        self.record_runtime_adaptation(health)
        self.player.require(health, *capabilities)
        if not control and self.record_player_health(health):
            raise ExecutorError("prime_device_recovery", "Player inspection unavailable; waiting for device recovery")
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
                    if not await self.select_and_launch(token, request):
                        return
                # Even after a lost acknowledgement/restart, only inspect the
                # original attempt. Never resend Play, switch to GTI, or re-search.
                await self.verify_launch(token)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.store.fail(token, ExecutorError("navigation_timeout", "Prime playback workflow timed out"))
        except ExecutorError as exc:
            if exc.code in {"prime_device_recovery", "prime_runtime_recovering"}:
                with self.db.transaction() as db:
                    current = self.store.get_row(db, token)
                    report = json.loads(current["report"])
                    report.setdefault("prime_player", {})["device_recovery"] = True
                    report["observation_status"] = {"state": "unavailable", "checked_at": utc(), "error": exc.detail()}
                    adaptation = self.db.device(db).get("prime_runtime_adaptation") or {}
                    delay = adaptation.get("retry_after_seconds", 15) if exc.code == "prime_runtime_recovering" else 60
                    self.store.write(db, token, report, next_check=time.time() + delay)
            else:
                self.store.fail(token, exc)
        except Exception:
            self.store.fail(token, ExecutorError("prime_workflow_failed", "Prime playback workflow failed"))

    async def select_and_launch(self, token, request):
        health = await self.check_session(
            capabilities=("search", "play", "playback_status", "cancel", "stop")
        )
        if health.get("api_version", 0) < 11:
            raise ExecutorError(
                "prime_api_incompatible",
                "Prime Player API 11 or newer is required for catalogue search and launch refusal evidence",
                retryable=False,
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
        prepared = self.prepared_catalogue(
            request, session, generation=health.get("compatibility", {}).get("generation")
        )
        if prepared:
            results, selected, audit = prepared["results"], prepared["selected"], prepared["audit"]
            received = time.monotonic() - (time.time() - prepared["received_at"])
            self.save_workflow(token, catalogue_probe_id=prepared["id"], catalogue=results)
        else:
            results = await self.mutation(
                token,
                "PRIME_SEARCH",
                lambda: self.player.search(query, self.config.prime_search_timeout, ownership),
            )
            if results.get("session_id") != session or results.get("query") != query:
                raise ExecutorError("prime_stale_result", "Search result belongs to another session or query")
            self.require_catalogue_generation(health, results)
            received = time.monotonic()
            self.store.phase(token, "matching")
            self.save_workflow(token, catalogue=results)

            async def fetch_broadcasts(content_id):
                current = await self.check_session(session, capabilities=("broadcasts",))
                self.require_catalogue_generation(current, results)
                receipt = self.player.ownership(current, automatic=True)
                return await self.mutation(
                    token, "PRIME_BROADCASTS", lambda: self.player.broadcasts(content_id, receipt)
                )

            with capture_model_calls(lambda evidence: self.save_model_call(token, evidence)):
                selected, audit = await choose_broadcast(
                    request,
                    results,
                    timezone,
                    self.matcher,
                    fetch_broadcasts,
                    lambda audit: self.save_workflow(token, selection=audit),
                )
        self.save_workflow(
            token,
            selection=audit,
            search_id=results.get("search_id"),
            search_generation=results.get("generation"),
            search_observed_at=results.get("observed_at"),
        )
        if not selected:
            self.complete_search(token, selection_state(selected, audit))
            return False
        if selected["readiness"] != "ready":
            self.complete_search(token, selected["readiness"], selected)
            return False
        if time.monotonic() - received > 90:
            raise ExecutorError("prime_stale_result", "Catalogue selection is stale; refresh before launch")
        health = await self.check_session(session, capabilities=("play",))
        self.require_catalogue_generation(health, results)
        ownership = self.player.ownership(health, automatic=True)
        attempt = uuid4().hex
        self.save_workflow(
            token, attempt_id=attempt, selected=selected, launch_outcome=None, ownership=ownership
        )
        self.store.phase(token, "launching")
        try:
            outcome = await self.mutation(
                token, "PRIME_PLAY", lambda: self.player.play(selected["content_id"], attempt, ownership)
            )
            self.validate_outcome(outcome, self.store.report(token)["prime_player"])
            self.save_workflow(token, launch_outcome=outcome)
        except ExecutorError as exc:
            # Delivery uncertainty is resolved by read-only inspection, not a
            # fresh request that could replace playback a second time.
            if exc.code not in {"prime_transport_unknown", "prime_operation_unknown"}:
                raise
            self.save_workflow(token, launch_error=exc.detail())
        return True

    def complete_search(self, token, state, selected=None):
        with self.db.transaction() as db:
            row = self.store.allowed(db, token, navigation=True)
            report = json.loads(row["report"])
            report["prime_player"].update(selected=selected, readiness=state)
            report["operation"].update(
                state=state, phase=state, finished_at=utc(), updated_at=utc(), error=None
            )
            self.store.write(db, token, report, state=state)

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
        if outcome.get("evidence", {}).get("launch", {}).get("disposition") == "not_invoked":
            return
        if (
            workflow["selected"].get("parent_content_id")
            and outcome.get("resolved_id")
            and outcome["resolved_id"] != workflow["selected"]["content_id"]
        ):
            raise ExecutorError(
                "prime_broadcast_mismatch", "Prime resolved a different broadcast than selected"
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
            if (
                outcome["state"] == "failed"
                and outcome.get("evidence", {}).get("launch", {}).get("disposition") == "not_invoked"
            ):
                self.complete_search(token, "waiting_for_feed", workflow.get("selected"))
                return
            if outcome["state"] in {"failed", "unknown", "cancelled", "stopped"}:
                reason = outcome.get("reason")
                message = "Prime could not verify the requested playback"
                if isinstance(reason, str) and reason.strip():
                    message += ": " + reason.strip()[:800]
                raise ExecutorError("prime_device_recovery_expired" if workflow.get("device_recovery") else "prime_launch_unverified", message)
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
            if (
                report["content_status"].get("effective_state") == "ended"
                and (report["content_status"].get("observation") or {}).get("source") == "prime_player"
            ):
                return False
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
            device = self.db.device(db, row["device_id"])
            owner = db.execute(
                "SELECT current_token FROM executor_devices WHERE device_id=?", (row["device_id"],)
            ).fetchone()
            ended = bool(
                fresh
                and status.get("state") == "ended"
                and status.get("is_playing") is False
                and status.get("matches_attempt") is True
                and not status.get("error")
                and status.get("current_content_id") == launch.get("resolved_id")
                and launch.get("state") == "playing"
                and launch.get("evidence", {}).get("resolution", {}).get("playbackClass") == "live_watch_now"
                and row["state"] == "playing_verified"
                and device["intent_version"] == row["intent"]
                and device["automation"] == "active"
                and owner
                and owner[0] == token
            )
            if ended:
                self.record_completion(db, row, report, observed, now)
                return False
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
                    "valid_until": (
                        observed + timedelta(seconds=self.settings.playback_evidence_ttl)
                    ).isoformat(),
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
            # Pause/stop/error/unknown and unconfirmed Ended remain recovery conditions.
            self.store.write(
                db,
                token,
                report,
                state="playing_verified" if verified else row["state"],
                next_check=time.time() + self.config.monitor_interval,
            )
            return verified

    def record_completion(self, db, row, report, observed, now):
        """Publish one bound native Ended observation atomically to both read models."""
        stamp = now.isoformat()
        workflow = report["prime_player"]
        evidence = {
            "content_id": row["content_id"],
            "state": "ended",
            "source": "prime_player",
            "simulated": False,
            "timestamp_basis": "device_observed",
            "observed_at": observed.isoformat(),
            "received_at": stamp,
            "valid_until": (observed + timedelta(seconds=self.settings.status_ttl)).isoformat(),
            "evidence": {
                "method": "device_observation",
                "decision": "confirmed",
                "evidence_id": f"prime-ended:{workflow['session_id']}:{workflow['attempt_id']}",
                "summary": "Prime reported Ended for the currently bound live playback attempt",
                "confidence": None,
                "captured_at": observed.isoformat(),
            },
        }
        report["content_status"].update(
            lookup_state="ok",
            effective_state="ended",
            stale=False,
            observation=evidence,
            checked_at=stamp,
            error=None,
        )
        if report["observation"]:
            report["observation"]["verified"] = False
        report["observation_status"] = {"state": "fresh", "checked_at": stamp, "error": None}
        report["operation"].update(
            state="completed", phase="event_ended", updated_at=stamp, finished_at=stamp, error=None
        )
        self.store.write(db, row["token"], report, state="completed", next_check=0)
        # Replace the lookup request ID too: a feed lookup issued before this
        # observation must not overwrite it when its response arrives later.
        db.execute(
            "INSERT INTO content_status(content_id,request_id,observation,last_success,next_check) VALUES (?,?,?,?,0) "
            "ON CONFLICT(content_id) DO UPDATE SET request_id=excluded.request_id,observation=excluded.observation,"
            "last_success=excluded.last_success,error=NULL,failures=0,next_check=0",
            (row["content_id"], uuid4().hex, encode(evidence), stamp),
        )
        self.db.log(
            db,
            stamp,
            "Event completed by Prime",
            "The selected live feed reported Ended. Watch plan retained.",
            "completion",
            row["device_id"],
        )

    @correlated
    async def monitor(self, row):
        token = row["token"]
        try:
            workflow = self.store.report(token)["prime_player"]
            await self.check_session(workflow["session_id"], capabilities=("playback_status",))
            async with self.input_lock:
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
        recent = (
            checked and 0 <= (datetime.now(UTC) - checked).total_seconds() < self.settings.observation_ttl
        )
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
                elif workflow.get("replacement_request_id"):
                    # The acknowledged barrier quiesces the old request. The new
                    # launch will replace playback; do not stop it in advance.
                    active = "unknown"
                    self.save_cancellation(token, playback_preserved_for_replacement=True)
                elif (workflow.get("launch_outcome") or {}).get("evidence", {}).get("launch", {}).get(
                    "disposition"
                ) == "not_invoked":
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
                    # No launch is bound to this token. A catalogue read alone
                    # cannot prove what unrelated content is playing or stopped.
                    active = "unknown"
            self.store.acknowledge_cancel(token, active)
