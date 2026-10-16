"""Durable, low-priority entitlement enrichment of the imported Teamarr catalogue."""

import asyncio
import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from functools import partial
from uuid import uuid4

from fastapi import HTTPException

from .database import encode
from .executor.models import ExecutorError
from .model_diagnostics import capture_model_calls
from .prime_player.entitlement import choose_entitlement
from .prime_player.labels import prime_option
from .prime_player.search import choose_search

log = logging.getLogger(__name__)
SEARCH_SECONDS = 5
CHECK_SECONDS = 120
DISPATCH_INTERVAL = 60


def retry_delay(failures):
    return min(5 * 60 * 2 ** min(max(failures - 1, 0), 6), 5 * 60 * 60)


def identity(item):
    snapshot = item["snapshot"]
    event = snapshot.get("event") or {}
    return hashlib.sha256(encode([
        snapshot.get("id"), snapshot.get("title"), snapshot.get("start_time"),
        snapshot.get("competition"), snapshot.get("kind"),
        event.get("away_team_details"), event.get("home_team_details"), item["viewing_options"],
    ]).encode()).hexdigest()


class Deferred(Exception):
    """Playback has priority; keep this job without counting an access failure."""


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

    def entitlement_idle(self, db, device):
        workflow = self.executor
        if (not workflow or workflow.stopping or workflow.input_lock.locked()
                or device.get("manual_control") or device.get("input_handoff")
                or device.get("recovery") or device.get("player_recovery")
                or device.get("playback_state") in {"navigating", "unverified"}
                or device.get("prime_network", {}).get("state") == "transitioning"):
            return False
        for name in ("active", "catalogue_task", "player_recovery_task"):
            task = getattr(workflow, name, None)
            if task and not task.done():
                return False
        if db.execute("SELECT 1 FROM jobs WHERE device_id=? AND state='pending' LIMIT 1", (device["id"],)).fetchone():
            return False
        return not db.execute(
            "SELECT 1 FROM executor_jobs WHERE device_id=? AND (cancel_requested=1 OR "
            "(cancel_requested=0 AND (state IN ('accepted','navigating') OR "
            "(state='playing_verified' AND next_check<=?)))) LIMIT 1",
            (device["id"], time.time()),
        ).fetchone()

    async def check_entitlement_once(self):
        workflow = self.executor
        if not workflow:
            return
        now = time.time()
        with self.db.transaction() as db:
            device = self.db.device(db)
            if not self.db.lease(db, "entitlements:" + device["id"], self.owner, now, CHECK_SECONDS + 60):
                return
            # A lost response after restart is unknown. Never immediately replay it.
            for row in db.execute(
                "SELECT * FROM event_entitlements WHERE device_id=? AND state='checking' AND started_at<?",
                (device["id"], now - CHECK_SECONDS - 30),
            ).fetchall():
                self.finish_entitlement(db, row, "unknown", {"reason": "Check interrupted; retry scheduled"})
            due = db.execute(
                "SELECT e.* FROM event_entitlements e JOIN contents c ON c.id=e.content_id AND c.active=1 "
                "WHERE e.device_id=? AND e.state IN ('queued','retry') "
                "AND e.next_check<=? ORDER BY e.manual DESC,e.next_check,e.content_id", (device["id"], now),
            ).fetchall()
            if not due or not self.entitlement_idle(db, device):
                return
            if not due[0]["manual"] and now < self.db.meta(db, "entitlement_dispatch_after", 0):
                return
            items = {i["content_id"]: i for i in self.items(db, device["id"])}
            row = None
            for candidate in due:
                item = items.get(candidate["content_id"])
                if item and item["lifecycle"]["state"] in {"ended", "cancelled"}:
                    db.execute("UPDATE event_entitlements SET state='stopped',next_check=NULL WHERE device_id=? AND content_id=?",
                               (device["id"], candidate["content_id"]))
                    continue
                if item and item["active"] and item.get("entitlement"):
                    row = dict(candidate)
                    break
            if not row or not self.entitlement_idle(db, device):
                return
            if not row["manual"] and now < self.db.meta(db, "entitlement_dispatch_after", 0):
                return
            item = items[row["content_id"]]
            row.update(check_id=uuid4().hex, started_at=now)
            db.execute(
                "UPDATE event_entitlements SET state='checking',check_id=?,started_at=?,next_check=NULL,manual=0 "
                "WHERE device_id=? AND content_id=?", (row["check_id"], now, device["id"], row["content_id"]),
            )
            self.db.set_meta(db, "entitlement_dispatch_after", now + DISPATCH_INTERVAL)
        evidence = {"identity": identity(item)}
        request = {"content_snapshot": item["snapshot"], "allowed_viewing_options": item["viewing_options"]}
        context = {"source": "schedule_entitlement", "device_id": device["id"],
                   "content_id": item["content_id"], "check_id": row["check_id"]}

        async def search(query):
            with self.db.transaction() as db:
                current = self.db.device(db, device["id"])
                if not self.entitlement_idle(db, current):
                    raise Deferred()
            # Never queue behind foreground operations. Each request releases this
            # lane before matching/fallback. The service search itself is bounded.
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
                workflow.require_catalogue_generation(after, result)
                evidence.update(session_id=result.get("session_id"), generation=result.get("generation"),
                                network_context_id=result.get("network_context_id"), search_id=result.get("search_id"))
                return result

        status = "unknown"
        deferred = False
        try:
            async with asyncio.timeout(CHECK_SECONDS):
                with capture_model_calls(lambda _: None, recorder=workflow.model_captures.record, context=context):
                    _, selected, audit = await choose_search(
                        request, device["preferences"]["timezone"], workflow.matcher, search, None,
                        lambda a: evidence.update(audit=a),
                        select=partial(choose_entitlement, workflow.matcher),
                    )
                if selected:
                    status = {"ENTITLED": "entitled", "UNENTITLED": "unentitled"}.get(
                        selected.get("entitlement_status"), "unknown")
                evidence.update(reason={"entitled": "Included with your subscription",
                                        "unentitled": "Subscription required for the matching feeds",
                                        "unknown": "Entitlement unconfirmed"}[status], audit=audit)
        except Deferred:
            deferred = True
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
                    db.execute("UPDATE event_entitlements SET state='queued',next_check=?,manual=? WHERE check_id=?",
                               (time.time() + DISPATCH_INTERVAL, row["manual"], row["check_id"]))
                else:
                    self.finish_entitlement(db, row, status, evidence)

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
            except Exception:
                log.exception("Schedule entitlement worker failed")
            await asyncio.sleep(2)
