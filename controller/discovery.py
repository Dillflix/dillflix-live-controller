"""Device-scoped page inventory and unresolved discovery coordination.

No device I/O here. All navigation is a durable executor request with the same
intent fence, cancellation barrier, and observation verification as search.
"""

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

from .database import encode
from .planner import choose, parse_time


def policy_key(device):
    return hashlib.sha256(
        encode({k: device[k] for k in ("preferences", "rules", "team_ranks")}).encode()
    ).hexdigest()


def validate_inventory(result, session=None):
    pages = result.get("pages")
    if (session and result.get("session_id") != session) or not isinstance(pages, list) or len(pages) > 100:
        raise ValueError("Invalid Prime page inventory")
    ids = set()
    for p in pages:
        if (
            not isinstance(p, dict)
            or not isinstance(p.get("id"), str)
            or not 1 <= len(p["id"]) <= 256
            or p["id"] in ids
            or type(p.get("available")) is not bool
            or any(
                p.get(k) is not None and (not isinstance(p[k], str) or len(p[k]) > 512)
                for k in ("title", "title_hint", "kind")
            )
        ):
            raise ValueError("Invalid Prime page descriptor")
        ids.add(p["id"])
    if not parse_time(result.get("observed_at")):
        raise ValueError("Page inventory needs an observation timestamp")


class DiscoveryCoordinator:
    def refresh_pages_command(self, device_id, command):
        def apply(db, device):
            inventory = device["prime_pages"]
            if inventory["state"] not in {"queued", "refreshing"}:
                inventory.update(state="queued", error=None, requested_at=datetime.now(UTC).isoformat())
            self.db.log(
                db,
                self.now(db).isoformat(),
                "Page refresh queued",
                "Runs when automation owns an idle device; existing page choices are retained",
                "settings",
            )

        return self.mutate(
            device_id, command.command_id, command.expected_revision, command.model_dump(), apply
        )

    def discovery_eligible(self, d, items, now, real):
        # A failed match does not remove the known Teamarr target. Do not replace
        # it with a different sport simply because it is in retry backoff.
        unthrottled = {**d, "failures": {}}
        if choose(unthrottled, items, now, real)["content_id"]:
            return False
        return True

    def maybe_stage_discovery(self, db, d, decision, items, real):
        if decision["content_id"]:
            if d["prime_pages"]["state"] == "refreshing":
                d["prime_pages"]["state"] = "queued"
            return False
        pending = db.execute(
            "SELECT * FROM jobs WHERE device_id=? AND state='pending'", (d["id"],)
        ).fetchone()
        if pending and json.loads(pending["payload"]).get("schema_version") != 2:
            return False
        observed = d.get("observed") or {}
        inventory = d["prime_pages"]
        prefs = d["preferences"]["prime_discovery"]
        available = {p["id"] for p in inventory["pages"] if p.get("available")}
        enabled = [p for p in prefs["enabled_pages"] if p in available]
        key = policy_key(d)
        if pending:
            request = json.loads(pending["payload"])
            valid = pending["intent"] == d["intent_version"] and (
                request["purpose"] == "page_refresh"
                or (
                    request["interests"].get("policy_key") == key
                    and enabled
                    and self.discovery_eligible(d, items, self.now(db), real)
                )
            )
            if valid:
                d["reason"] = (
                    "Refreshing available Prime pages"
                    if request["purpose"] == "page_refresh"
                    else "Finding an interesting live event on enabled pages"
                )
                self.db.save_device(db, d)
                return True
            db.execute("UPDATE jobs SET state='superseded' WHERE id=?", (pending["id"],))
            d["intent_version"] += 1
            if request["purpose"] == "page_refresh":
                inventory["state"] = "queued"
        current = next((i for i in items if i["content_id"] == observed.get("content_id")), None)
        if observed.get("verified") and (
            d.get("discovered") or (current and current["lifecycle"]["state"] in {"live", "unknown"})
        ):
            # Scanning pages navigates the TV. Preserve healthy current playback.
            d["reason"] = (
                ("Discovered on Prime: " + d["discovered"].get("reason", "Live broadcast selected"))
                if d.get("discovered")
                else "Retaining verified playback"
            )
            self.db.save_device(db, d)
            return True
        grace = (d.get("recovery") or {}).get("retry_after")
        if observed and grace and real < parse_time(grace):
            self.db.save_device(db, d)
            return True
        purpose = "page_refresh" if inventory["state"] == "queued" else "discovery"
        retry = d.get("discovery_retry") or {}
        if purpose == "discovery" and (
            not enabled
            or not self.discovery_eligible(d, items, self.now(db), real)
            or (retry.get("policy_key") == key and real < parse_time(retry["after"]))
        ):
            if pending:
                d["playback_state"] = "waiting"
                self.db.save_device(db, d)
            return False
        request_id = str(uuid.uuid4())
        d["intent_version"] += 1
        team_ids = {key for ranked in d["team_ranks"].values() for key in ranked}
        team_ids.update(r.get("team_id") for r in d["rules"] if r.get("enabled", True))
        interests = dict(
            policy_key=key,
            priorities=[r for r in d["rules"] if r.get("enabled", True)],
            team_ranks=d["team_ranks"],
            teams=[t for t in self.db.teams(db) if t["key"] in team_ids],
            recent_viewing=d.get("recent_discovery", [])[-10:],
        )
        request = dict(
            schema_version=2,
            request_id=request_id,
            device_id=d["id"],
            intent_version=d["intent_version"],
            content_id=None,
            mode="live",
            purpose=purpose,
            discovery={**prefs, "enabled_pages": enabled},
            interests=interests,
        )
        # Complete collection plus model/launch/verification fit one explicit budget.
        budget = 80 if purpose == "page_refresh" else 100 * len(enabled) + 150
        db.execute(
            "INSERT INTO jobs(id,device_id,intent,content_id,state,ready_at,payload,deadline_at,progress) VALUES (?,?,?,NULL,'pending',?,?,?,?)",
            (
                request_id,
                d["id"],
                d["intent_version"],
                time.time(),
                encode(request),
                time.time() + budget,
                "refreshing_pages" if purpose == "page_refresh" else "discovering",
            ),
        )
        d.update(
            desired=None,
            playback_state="navigating",
            observed=None,
            discovered=None,
            recovery=None,
            reason="Refreshing available Prime pages"
            if purpose == "page_refresh"
            else "Finding an interesting live event on enabled pages",
        )
        if purpose == "page_refresh":
            inventory.update(state="refreshing", request_id=request_id)
        self.db.log(db, self.now(db).isoformat(), d["reason"], ", ".join(enabled), "navigation")
        self.db.save_device(db, d)
        return True

    def discovery_failed(self, db, d, job, reason, state="failed"):
        request = json.loads(job["payload"])
        db.execute("UPDATE jobs SET state=?,progress=?,error=? WHERE id=?", (state, state, reason, job["id"]))
        d["playback_state"] = "waiting"
        if request["purpose"] == "page_refresh":
            d["prime_pages"].update(state="error", error=reason)
        else:
            d["discovery_retry"] = dict(
                policy_key=request["interests"].get("policy_key"),
                after=(datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
            )
        d["reason"] = reason
        self.db.log(
            db,
            self.now(db).isoformat(),
            "Page refresh failed" if request["purpose"] == "page_refresh" else "Discovery waiting",
            reason,
            "failure",
        )

    def receive_discovery_report(self, db, d, job, report):
        request = json.loads(job["payload"])
        items = self.items(db, d["id"])
        current = choose(d, items, self.now(db), datetime.now(UTC))
        if (
            d.get("manual_control")
            or d.get("input_handoff")
            or d["automation"] == "paused"
            or job["intent"] != d["intent_version"]
            or current["content_id"]
            or (
                request["purpose"] == "discovery"
                and (
                    request["interests"].get("policy_key") != policy_key(d)
                    or not self.discovery_eligible(d, items, self.now(db), datetime.now(UTC))
                )
            )
        ):
            db.execute("UPDATE jobs SET state='superseded' WHERE id=?", (job["id"],))
            if request["purpose"] == "page_refresh":
                d["prime_pages"]["state"] = "queued"
            self.db.save_device(db, d)
            return False
        if job["deadline_at"] <= time.time():
            self.discovery_failed(db, d, job, "Discovery exceeded its deadline", "timed_out")
        elif not isinstance(report, dict) or any(
            report.get(k) != v
            for k, v in {
                "request_id": job["id"],
                "device_id": job["device_id"],
                "intent_version": job["intent"],
            }.items()
        ):
            self.discovery_failed(db, d, job, "Discovery response identity mismatch", "rejected")
        elif report["state"] in {"accepted", "navigating"}:
            db.execute(
                "UPDATE jobs SET executor_job_id=?,progress=? WHERE id=?",
                (report.get("executor_job_id"), report.get("phase") or report["state"], job["id"]),
            )
        elif request["purpose"] == "page_refresh" and report["state"] == "completed":
            result = (report.get("prime_player") or {}).get("pages_result") or {}
            try:
                validate_inventory(result)
            except (ValueError, TypeError):
                self.discovery_failed(db, d, job, "Invalid page inventory", "rejected")
            else:
                d["prime_pages"].update(
                    pages=result["pages"],
                    state="ready",
                    last_success=result["observed_at"],
                    session_id=result.get("session_id"),
                    error=None,
                )
                d["playback_state"] = "waiting"
                db.execute(
                    "UPDATE jobs SET state='completed',progress='pages_refreshed' WHERE id=?", (job["id"],)
                )
                self.db.log(
                    db,
                    self.now(db).isoformat(),
                    "Available Prime pages refreshed",
                    "Enable pages in Settings to include them in discovery",
                    "settings",
                )
        elif request["purpose"] == "discovery" and report["state"] == "playing_verified":
            selected = (report.get("prime_player") or {}).get("selected") or {}
            from .prime_player.matching import GTI

            cid = selected.get("content_id")
            valid = (
                isinstance(cid, str)
                and GTI.fullmatch(cid)
                and report.get("content_id") == "prime:" + cid
                and selected.get("title")
                and selected.get("viewing_option_id") == "prime-live:" + cid
            )
            bound = {**dict(job), "content_id": report.get("content_id"), "resolution": encode(selected)}
            error = (
                self.observation_error(bound, report.get("observation"))
                if valid
                else "Invalid discovered identity"
            )
            if error:
                self.discovery_failed(db, d, job, error, "rejected")
            else:
                db.execute(
                    "UPDATE jobs SET state='verified',content_id=?,resolution=?,progress='playing_verified',executor_job_id=? WHERE id=?",
                    (bound["content_id"], bound["resolution"], report.get("executor_job_id"), job["id"]),
                )
                d.update(
                    desired=bound["content_id"],
                    observed=report["observation"],
                    discovered=selected,
                    playback_state="verified",
                    started_at=self.now(db).isoformat(),
                    last_switch_at=datetime.now(UTC).isoformat(),
                    discovery_retry=None,
                    reason=selected.get("reason", "Selected live broadcast"),
                )
                d["recent_discovery"] = [
                    *d.get("recent_discovery", [])[-9:],
                    dict(content_id=cid, title=selected["title"], at=datetime.now(UTC).isoformat()),
                ]
                self.db.log(
                    db, self.now(db).isoformat(), "Discovered live playback verified", d["reason"], "verified"
                )
        else:
            self.discovery_failed(
                db, d, job, str(report.get("reason") or "No suitable live event selected")[:1000]
            )
        self.db.save_device(db, d)
        return db.execute("SELECT state FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] in {
            "pending",
            "verified",
            "completed",
        }
