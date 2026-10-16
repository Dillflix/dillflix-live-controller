"""Durable, low-priority entitlement enrichment of the imported Teamarr catalogue."""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from functools import partial
from uuid import uuid4

from fastapi import HTTPException

from .database import encode
from .entitlement_store import identity
from .executor.models import ExecutorError
from .model_diagnostics import capture_model_calls
from .planner import parse_time
from .prime_player.entitlement import choose_entitlement
from .prime_player.identity_cache import EventMatchCache
from .prime_player.labels import prime_option
from .prime_player.search import choose_search

log = logging.getLogger(__name__)
SEARCH_SECONDS = 15
CHECK_SECONDS = 120
DISPATCH_INTERVAL = 5
DEFER_SECONDS = 60
SERVICE_ERRORS = frozenset({
    "prime_unavailable", "prime_incompatible", "prime_api_incompatible", "prime_runtime_recovering",
    "prime_runtime_unsupported", "prime_network_recovering", "prime_ownership_unavailable",
    "prime_device_mismatch", "prime_transport_unknown",
})


def retry_delay(failures):
    return min(5 * 60 * 2 ** min(max(failures - 1, 0), 6), 5 * 60 * 60)


def queue_priority(row, item, device, prefer_retry=True):
    """Urgent events first; zero-valued initial deadlines must not starve retries."""
    committed = {entry["content_id"] for entry in device["plan"]}
    urgent = item["lifecycle"]["state"] == "live" or item["content_id"] in committed
    return (-row["manual"], not urgent, (row["failures"] > 0) != prefer_retry,
            parse_time(item["start_time"]).timestamp(), row["next_check"], row["content_id"])


class Deferred(Exception):
    """Playback has priority; keep this job without counting an access failure."""


class Settled(Exception):
    """Foreground evidence completed this event while background work was running."""


class EntitlementChecks:
    def seed_entitlements(self, db):
        if not self.executor:
            return
        # INSERT OR IGNORE is the once-per-event boundary, including reimports.
        # Rows intentionally outlive catalogue retention as compact deduplication records.
        for device in db.execute("SELECT id FROM devices").fetchall():
            for row in db.execute("SELECT id,snapshot FROM contents WHERE active=1 AND source='teamarr'"):
                snapshot = json.loads(row["snapshot"])
                if any(prime_option(o) for o in snapshot.get("viewing_options", [])):
                    db.execute(
                        "INSERT OR IGNORE INTO event_entitlements(device_id,content_id,next_check) VALUES (?,?,0)",
                        (device["id"], row["id"]),
                    )

    def entitlement_views(self, db, device_id, items):
        records = {r["content_id"]: r for r in db.execute(
            "SELECT * FROM event_entitlements WHERE device_id=? AND content_id IN (SELECT id FROM contents)", (device_id,)
        )}
        for item in items:
            row = records.get(item["content_id"])
            relevant = any(prime_option(o) for o in item["viewing_options"])
            if not self.executor or not relevant:
                item["entitlement"] = None
                continue
            evidence = json.loads(row["evidence"]) if row else {}
            stale = bool(evidence.get("identity") and evidence["identity"] != identity(item))
            status = row["status"] if row and not stale else "unknown"
            # A Prime-only denial cannot disable another permitted application.
            blocked = status == "unentitled" and all(prime_option(o) for o in item["viewing_options"])
            item["entitlement"] = {
                "status": status, "state": row["state"] if row else "queued",
                "checked_at": row["checked_at"] if row else None,
                "next_check_at": datetime.fromtimestamp(row["next_check"], UTC).isoformat()
                if row and row["next_check"] else None,
                "blocked": blocked,
                "reason": "Event details changed; recheck entitlement" if stale else evidence.get("reason"),
                "can_check": item["lifecycle"]["state"] not in {"ended", "cancelled"},
            }
        return items

    def entitlement_command(self, device_id, command):
        def apply(db, device):
            if not self.executor:
                raise HTTPException(409, "Entitlement checks require Prime Player")
            item = next((i for i in self.items(db, device_id) if i["content_id"] == command.content_id), None)
            if not item:
                raise HTTPException(404, "Content is not in the catalogue")
            if not item.get("entitlement") or not item["entitlement"]["can_check"]:
                raise HTTPException(422, "This event cannot be checked for Prime entitlement")
            db.execute(
                "INSERT OR IGNORE INTO event_entitlements(device_id,content_id,next_check) VALUES (?,?,0)",
                (device_id, command.content_id),
            )
            # Keep both in-flight work and its last observation; another click coalesces.
            db.execute(
                "UPDATE event_entitlements SET state='queued',next_check=0,manual=1 "
                "WHERE device_id=? AND content_id=? AND state!='checking'",
                (device_id, command.content_id),
            )
        return self.mutate(device_id, command.command_id, command.expected_revision,
                           command.model_dump(), apply)

    def entitlement_blocker(self, db, device, *, device_read=True):
        """Routine monitoring owns device I/O, not matching already fetched data."""
        workflow = self.executor
        if not workflow or workflow.stopping:
            return "Playback service is not running"
        for field, reason in (
            ("manual_control", "Manual control is active"),
            ("input_handoff", "Device control is changing"),
            ("recovery", "Playback recovery is in progress"),
            ("player_recovery", "Prime runtime recovery is in progress"),
        ):
            if device.get(field):
                return reason
        if device.get("playback_state") in {"navigating", "unverified"}:
            return "Waiting for playback to become stable"
        if device.get("prime_network", {}).get("state") == "transitioning":
            return "Network location is changing"
        for name in ("catalogue_task", "player_recovery_task"):
            task = getattr(workflow, name, None)
            if task and not task.done():
                return "Foreground catalogue or recovery work has priority"
        if db.execute("SELECT 1 FROM jobs WHERE device_id=? AND state='pending' LIMIT 1", (device["id"],)).fetchone():
            return "A playback request has priority"
        if db.execute(
            "SELECT 1 FROM executor_jobs WHERE device_id=? AND (cancel_requested=1 OR "
            "(cancel_requested=0 AND state IN ('accepted','navigating'))) LIMIT 1", (device["id"],),
        ).fetchone():
            return "Playback launch or cancellation has priority"
        if not device_read:
            return None
        if workflow.input_lock.locked():
            return "Waiting for the current device operation"
        if workflow.active and not workflow.active.done():
            return "Playback monitoring has priority"
        if db.execute(
            "SELECT 1 FROM executor_jobs WHERE device_id=? AND (cancel_requested=1 OR "
            "(cancel_requested=0 AND (state IN ('accepted','navigating') OR "
            "(state='playing_verified' AND next_check<=?)))) LIMIT 1",
            (device["id"], time.time()),
        ).fetchone():
            return "Playback monitoring has priority"
        return None

    def entitlement_idle(self, db, device):
        return self.entitlement_blocker(db, device) is None

    def entitlement_progress(self, db, phase, *, content_id=None, reason=None):
        self.db.set_meta(db, "entitlement_worker", {
            "phase": phase, "content_id": content_id, "reason": reason,
            "updated_at": time.time(),
        })

    def entitlement_service_failure(self, db, device_id, error):
        key = "entitlement_service:" + device_id
        previous = self.db.meta(db, key, {})
        failures = previous.get("failures", 0) + 1
        value = {"error": error.detail(), "failures": failures, "at": time.time(),
                 "next_probe": time.time() + min(300, 30 * 2 ** min(failures - 1, 4))}
        self.db.set_meta(db, key, value)
        self.entitlement_progress(db, "waiting", reason="Prime search unavailable: " + str(error))

    async def entitlement_service_ready(self, workflow, device_id):
        """Probe one shared dependency before claiming any event job."""
        with self.db.transaction() as db:
            blocked = self.db.meta(db, "entitlement_service:" + device_id, {})
            if blocked.get("next_probe", 0) > time.time():
                return False
        try:
            async with workflow.input_lock:
                health = await workflow.player.health()
                if health.get("serial") != self.settings.screen_adb_serial:
                    raise ExecutorError("prime_device_mismatch", "Prime Player device differs")
                workflow.player.require(health, "search")
                workflow.player.ownership(health, automatic=True)
        except ExecutorError as exc:
            with self.db.transaction() as db:
                self.entitlement_service_failure(db, device_id, exc)
            return False
        with self.db.transaction() as db:
            self.db.set_meta(db, "entitlement_service:" + device_id, {})
            # Older deployments charged shared outages to individual events.
            # Preserve that history, but don't leave them behind hours of backoff
            # after the dependency has recovered.
            repair = blocked or not self.db.meta(db, "entitlement_service_repaired:" + device_id, False)
            for row in (db.execute("SELECT content_id,evidence FROM event_entitlements "
                                   "WHERE device_id=? AND state='retry'", (device_id,)).fetchall() if repair else []):
                if (json.loads(row["evidence"]).get("error") or {}).get("code") in SERVICE_ERRORS:
                    db.execute("UPDATE event_entitlements SET state='queued',next_check=0 "
                               "WHERE device_id=? AND content_id=?", (device_id, row["content_id"]))
            self.db.set_meta(db, "entitlement_service_repaired:" + device_id, True)
        return True

    def entitlement_queue(self, db, device, items):
        if not self.executor:
            return None
        now = time.time()
        projected = {i["content_id"]: i for i in items}
        rows, counts = [], {s: 0 for s in ("queued", "checking", "retry", "complete", "stopped")}
        failures = 0
        for r in db.execute("SELECT e.* FROM event_entitlements e JOIN contents c ON c.id=e.content_id "
                            "WHERE e.device_id=? ORDER BY "
                            "CASE e.state WHEN 'checking' THEN 0 WHEN 'retry' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END,"
                            "e.manual DESC,e.next_check,e.content_id", (device["id"],)):
            item = projected.get(r["content_id"])
            if not item or not item.get("entitlement"):
                continue
            evidence = json.loads(r["evidence"])
            counts[r["state"]] += 1
            failures += int(r["state"] == "retry" and bool(evidence.get("error")))
            rows.append({"content_id": r["content_id"], "title": item["title"],
                         "entitlement": item["entitlement"], "failures": r["failures"],
                         "error": evidence.get("error"), "last_deferral": evidence.get("last_deferral"),
                         "started_at": datetime.fromtimestamp(r["started_at"], UTC).isoformat()
                         if r["started_at"] else None,
                         "eligible": bool(item["active"] or r["manual"]),
                         "due": r["next_check"] is not None and r["next_check"] <= now,
                         "manual": bool(r["manual"])})
        worker = self.db.meta(db, "entitlement_worker", {})
        service = self.db.meta(db, "entitlement_service:" + device["id"], {})
        reason = worker.get("reason")
        phase = worker.get("phase", "starting")
        due = [r for r in rows if r["eligible"] and r["due"] and r["entitlement"]["state"] in {"queued", "retry"}]
        dispatch = self.db.meta(db, "entitlement_dispatch_after", 0)
        if not worker or now - worker["updated_at"] > CHECK_SECONDS + 30:
            phase, reason = "unavailable", "Entitlement worker has not reported progress recently"
        elif counts["checking"] and phase == "idle":
            phase, reason = "waiting", "Waiting to recover an interrupted check"
        elif not counts["checking"]:
            blocker = self.entitlement_blocker(db, device)
            if due and blocker:
                phase, reason = "waiting", blocker
            elif due and not any(r["manual"] for r in due) and now < dispatch:
                phase, reason = "waiting", "Brief pause between automatic checks"
            elif due:
                phase, reason = "ready", "Queued work is ready to run"
            elif any(r["eligible"] and r["entitlement"]["state"] == "retry" for r in rows):
                phase, reason = "waiting", "Waiting for scheduled retries"
            elif any(r["eligible"] and r["entitlement"]["state"] == "queued" for r in rows):
                phase, reason = "waiting", "Waiting to resume deferred checks"
            else:
                phase, reason = "idle", "No checks are due"
            if worker.get("phase") == "error":
                phase, reason = "error", worker.get("reason")
        if service:
            phase, reason = "waiting", "Prime search unavailable: " + service["error"]["message"]
        return {"phase": phase, "reason": reason, "counts": counts, "failed_jobs": failures,
                "service_error": service.get("error"),
                "service_probe_at": datetime.fromtimestamp(service["next_probe"], UTC).isoformat() if service else None,
                "updated_at": datetime.fromtimestamp(worker["updated_at"], UTC).isoformat() if worker else None,
                "dispatch_after": datetime.fromtimestamp(service["next_probe"] if service else dispatch, UTC).isoformat()
                if service or dispatch > now else None,
                "rows": rows}

    async def check_entitlement_once(self):
        workflow = self.executor
        if not workflow:
            return
        now = time.time()
        with self.db.transaction() as db:
            device = self.db.device(db)
            if not self.db.lease(db, "entitlements:" + device["id"], self.owner, now, CHECK_SECONDS + 60):
                return
            self.entitlement_progress(db, "idle")
            # A lost response after restart is unknown. Never immediately replay it.
            for row in db.execute(
                "SELECT * FROM event_entitlements WHERE device_id=? AND state='checking' AND started_at<?",
                (device["id"], now - CHECK_SECONDS - 30),
            ).fetchall():
                self.finish_entitlement(db, row, "unknown", {"reason": "Check interrupted; retry scheduled"})
            due = db.execute(
                "SELECT e.* FROM event_entitlements e JOIN contents c ON c.id=e.content_id "
                "WHERE (c.active=1 OR e.manual=1) AND e.device_id=? AND e.state IN ('queued','retry') "
                "AND e.next_check<=? ORDER BY e.manual DESC,e.next_check,e.content_id", (device["id"], now),
            ).fetchall()
            if not due or not self.entitlement_idle(db, device):
                return
            items = {i["content_id"]: i for i in self.items(db, device["id"])}
            prefer_retry = not self.db.meta(db, "entitlement_last_retry:" + device["id"], False)
            due = sorted((r for r in due if r["content_id"] in items),
                         key=lambda r: queue_priority(r, items[r["content_id"]], device, prefer_retry))
            row = None
            for candidate in due:
                item = items.get(candidate["content_id"])
                if item and item["lifecycle"]["state"] in {"ended", "cancelled"}:
                    db.execute("UPDATE event_entitlements SET state='stopped',next_check=NULL WHERE device_id=? AND content_id=?",
                               (device["id"], candidate["content_id"]))
                    continue
                if item and (item["active"] or candidate["manual"]) and item.get("entitlement"):
                    row = dict(candidate)
                    break
            if not row or not self.entitlement_idle(db, device):
                return
            if not row["manual"] and now < self.db.meta(db, "entitlement_dispatch_after", 0):
                return
            item = items[row["content_id"]]
        if not await self.entitlement_service_ready(workflow, device["id"]):
            return
        with self.db.transaction() as db:
            current = db.execute("SELECT * FROM event_entitlements WHERE device_id=? AND content_id=?",
                                 (device["id"], row["content_id"])).fetchone()
            if (not current or current["state"] not in {"queued", "retry"}
                    or current["check_id"] != row["check_id"]
                    or not self.entitlement_idle(db, self.db.device(db, device["id"]))):
                return
            row = dict(current)
            now = time.time()
            row.update(check_id=uuid4().hex, started_at=now)
            db.execute(
                "UPDATE event_entitlements SET state='checking',check_id=?,started_at=?,next_check=NULL,manual=0 "
                "WHERE device_id=? AND content_id=?", (row["check_id"], now, device["id"], row["content_id"]),
            )
            self.db.set_meta(db, "entitlement_dispatch_after", now + DISPATCH_INTERVAL)
            self.db.set_meta(db, "entitlement_last_retry:" + device["id"], row["failures"] > 0)
            self.entitlement_progress(db, "searching", content_id=row["content_id"])
        evidence = {"identity": identity(item)}
        request = {"content_snapshot": item["snapshot"], "allowed_viewing_options": item["viewing_options"]}
        context = {"source": "schedule_entitlement", "device_id": device["id"],
                   "content_id": item["content_id"], "check_id": row["check_id"]}

        def check_priority():
            with self.db.transaction() as db:
                current_check = db.execute(
                    "SELECT check_id FROM event_entitlements WHERE device_id=? AND content_id=?",
                    (device["id"], row["content_id"]),
                ).fetchone()
                if not current_check or current_check[0] != row["check_id"]:
                    raise Settled()
                current = self.db.device(db, device["id"])
                blocker = self.entitlement_blocker(db, current, device_read=False)
                if blocker:
                    raise Deferred(blocker)

        def progress(phase, reason=None):
            with self.db.transaction() as db:
                self.entitlement_progress(db, phase, content_id=row["content_id"], reason=reason)

        def before_match():
            check_priority()
            progress("matching", "Matching fetched catalogue results; playback monitoring can continue")

        async def search(query):
            # Keep earlier search/matching progress while a routine monitor runs.
            # Launch, manual control and recovery still defer the entire job.
            waiting = False
            while True:
                check_priority()
                with self.db.transaction() as db:
                    blocker = self.entitlement_blocker(db, self.db.device(db, device["id"]))
                if not blocker:
                    break
                if not waiting:
                    progress("waiting", blocker)
                    waiting = True
                # The whole job's deadline bounds this wait. A slow ordinary
                # monitor must not reset otherwise valid matching progress.
                await asyncio.sleep(0.25)
            progress("searching", "Reading Prime catalogue data")
            async with workflow.input_lock:
                health = await workflow.player.health()
                if health.get("serial") != self.settings.screen_adb_serial:
                    raise ExecutorError("prime_device_mismatch", "Prime Player device differs")
                workflow.player.require(health, "search")
                ownership = workflow.player.ownership(health, automatic=True)
                result = await workflow.player.search(query, SEARCH_SECONDS, ownership)
                after = await workflow.player.health()
                if after["session_id"] != health["session_id"] or result.get("session_id") != after["session_id"]:
                    raise ExecutorError("prime_stale_result", "Prime session changed during entitlement check")
                if workflow.player.ownership(after, automatic=True) != ownership:
                    raise ExecutorError("prime_stale_result", "Prime ownership changed during entitlement check")
                workflow.require_catalogue_generation(after, result)
                evidence.update(session_id=result.get("session_id"), generation=result.get("generation"),
                                network_context_id=result.get("network_context_id"), search_id=result.get("search_id"))
                return result

        status = "unknown"
        deferred = None
        try:
            async with asyncio.timeout(CHECK_SECONDS):
                with capture_model_calls(lambda _: None, recorder=workflow.model_captures.record, context=context):
                    _, selected, audit = await choose_search(
                        request, device["preferences"]["timezone"], workflow.matcher, search, None,
                        lambda a: evidence.update(audit=a),
                        select=partial(choose_entitlement, EventMatchCache(workflow.matcher),
                                       before_match=before_match),
                        exhaust_denials=True,
                    )
                check_priority()
                if selected:
                    status = {"ENTITLED": "entitled", "UNENTITLED": "unentitled"}.get(
                        selected.get("entitlement_status"), "unknown")
                evidence.update(reason={"entitled": "Included with your subscription",
                                        "unentitled": "Subscription required for the matching feeds",
                                        "unknown": "Entitlement unconfirmed"}[status], audit=audit)
        except Settled:
            pass
        except Deferred as exc:
            deferred = str(exc)
        except ExecutorError as exc:
            if exc.code in SERVICE_ERRORS:
                deferred = "Prime search unavailable: " + str(exc)
                with self.db.transaction() as db:
                    self.entitlement_service_failure(db, device["id"], exc)
            else:
                evidence.update(reason="Entitlement check failed; retry scheduled", error=exc.detail())
        except asyncio.CancelledError:
            evidence["reason"] = "Check interrupted; retry scheduled"
            raise
        except Exception as exc:
            evidence.update(reason="Entitlement check failed; retry scheduled",
                            error=exc.detail() if isinstance(exc, ExecutorError) else {"code": type(exc).__name__})
        finally:
            with self.db.transaction() as db:
                current_item = next((i for i in self.items(db, device["id"]) if i["content_id"] == item["content_id"]), None)
                if current_item and identity(current_item) != evidence["identity"]:
                    status = "unknown"
                    evidence["reason"] = "Event details changed during the check"
                if current_item and current_item["lifecycle"]["state"] in {"ended", "cancelled"}:
                    db.execute("UPDATE event_entitlements SET state='stopped',next_check=NULL WHERE check_id=?", (row["check_id"],))
                elif deferred:
                    retained = json.loads(row["evidence"])
                    retained["last_deferral"] = {"reason": deferred, "at": datetime.now(UTC).isoformat()}
                    db.execute("UPDATE event_entitlements SET state='queued',next_check=?,manual=?,evidence=? WHERE check_id=?",
                               (time.time() + DEFER_SECONDS, row["manual"], encode(retained), row["check_id"]))
                else:
                    self.finish_entitlement(db, row, status, evidence)
                self.entitlement_progress(db, "waiting" if deferred else "idle", reason=deferred)

    def finish_entitlement(self, db, row, status, evidence):
        failures = row["failures"] + 1 if status == "unknown" else 0
        db.execute(
            "UPDATE event_entitlements SET state=?,status=?,failures=?,next_check=?,checked_at=?,evidence=? "
            "WHERE device_id=? AND content_id=? AND check_id=?",
            ("retry" if status == "unknown" else "complete", status, failures,
             time.time() + retry_delay(failures) if status == "unknown" else None,
             datetime.now(UTC).isoformat(), encode(evidence), row["device_id"], row["content_id"], row["check_id"]),
        )

    async def run_entitlements(self):
        while True:
            try:
                await self.check_entitlement_once()
            except Exception as exc:
                log.exception("Schedule entitlement worker failed")
                with self.db.transaction() as db:
                    self.entitlement_progress(db, "error", reason=f"Entitlement worker failed: {type(exc).__name__}")
            await asyncio.sleep(2)
