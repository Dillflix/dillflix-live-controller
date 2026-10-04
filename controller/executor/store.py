"""Durable request, token, intent, cancellation and side-effect journal."""

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime, timedelta

from ..content_status import source_observation
from ..database import encode
from ..planner import parse_time
from .models import ExecutorError, PlaybackRequest


def utc():
    return datetime.now(UTC).isoformat()


class ExecutorStore:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings

    def get_row(self, db, token):
        row = db.execute("SELECT * FROM executor_jobs WHERE token=?", (token,)).fetchone()
        if row is None:
            raise ExecutorError("token_not_found", "Unknown playback token", status=404, retryable=False)
        return row

    def submit(self, request):
        body = PlaybackRequest.model_validate(request).model_dump(mode="json")
        digest = hashlib.sha256(encode(body).encode()).hexdigest()
        with self.db.transaction() as db:
            prior = db.execute(
                "SELECT * FROM executor_jobs WHERE request_id=?", (body["request_id"],)
            ).fetchone()
            if prior:
                if prior["request_hash"] != digest:
                    raise ExecutorError(
                        "request_conflict",
                        "Request ID already has different data",
                        status=409,
                        retryable=False,
                    )
                return self.project(prior, db), False
            device = self.db.device(db, body["device_id"])
            if (
                device.get("manual_control")
                or device.get("input_handoff")
                or device["automation"] == "paused"
            ):
                raise ExecutorError(
                    "manual_or_paused",
                    "Device automation is paused or in manual handoff",
                    status=409,
                    retryable=False,
                )
            db.execute("INSERT OR IGNORE INTO executor_devices(device_id) VALUES (?)", (body["device_id"],))
            owner = db.execute(
                "SELECT * FROM executor_devices WHERE device_id=?", (body["device_id"],)
            ).fetchone()
            intent = body["intent_version"]
            if (
                intent <= owner["cancelled_through"]
                or intent <= owner["highest_intent"]
                or intent < device["intent_version"]
            ):
                raise ExecutorError(
                    "stale_intent",
                    "Device intent is obsolete or already assigned",
                    status=409,
                    retryable=False,
                )
            if parse_time(body["deadline_at"]) <= datetime.now(UTC):
                raise ExecutorError(
                    "deadline_expired", "Navigation deadline has passed", status=409, retryable=False
                )
            if parse_time(body["deadline_at"]) > datetime.now(UTC) + timedelta(hours=1):
                raise ExecutorError(
                    "deadline_too_far",
                    "Navigation deadline must be within one hour",
                    status=422,
                    retryable=False,
                )
            # Raw authenticated API calls also advance the controller's intent so
            # its next manual fence always covers every accepted executor request.
            device["intent_version"] = max(device["intent_version"], intent)
            self.db.save_device(db, device)
            db.execute(
                "UPDATE executor_devices SET highest_intent=? WHERE device_id=?", (intent, body["device_id"])
            )
            for old in db.execute(
                "SELECT * FROM executor_jobs WHERE device_id=? AND cancel_requested=0 AND retired_at IS NULL "
                "AND state IN ('accepted','navigating','playing_verified','waiting_for_feed','access_unknown','feeds_locked','no_matching_feed','completed')",
                (body["device_id"],),
            ).fetchall():
                report = json.loads(old["report"])
                report["cancellation"] = {"state": "requested", "input_quiescent": False}
                if self.settings.executor.mode == "prime-player" and old["state"] == "playing_verified":
                    report.setdefault("prime_player", {})["replacement_request_id"] = body["request_id"]
                if old["state"] not in {"playing_verified", "completed"}:
                    report["operation"].update(state="superseded", phase="superseded", finished_at=utc())
                self.write(db, old["token"], report, cancel_requested=1, next_check=0)
            token, now = "pb_" + secrets.token_urlsafe(24), utc()
            report = {
                "schema_version": 1,
                "token": token,
                "request_id": body["request_id"],
                "device_id": body["device_id"],
                "content_id": body["content_id"],
                "intent_version": intent,
                "revision": 1,
                "operation": {
                    "state": "accepted",
                    "phase": "queued",
                    "created_at": now,
                    "updated_at": now,
                    "finished_at": None,
                    "deadline_at": body["deadline_at"],
                    "error": None,
                    "attempts": [],
                },
                "observation": None,
                "observation_status": {"state": "unavailable", "checked_at": now, "error": None},
                "content_status": {
                    "content_id": body["content_id"],
                    "lookup_state": "unavailable",
                    "effective_state": "unknown",
                    "stale": True,
                    "observation": None,
                    "checked_at": now,
                    "error": None,
                },
                "cancellation": {"state": "none", "input_quiescent": False},
                "retained_until": None,
            }
            db.execute(
                "INSERT INTO executor_jobs(token,request_id,device_id,intent,content_id,request,request_hash,report,state) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    token,
                    body["request_id"],
                    body["device_id"],
                    intent,
                    body["content_id"],
                    encode(body),
                    digest,
                    encode(report),
                    "accepted",
                ),
            )
            return report, True

    def project(self, row, db=None):
        if row["request"] is None:
            raise ExecutorError("token_retired", "Playback token has retired", status=410, retryable=False)
        report = json.loads(row["report"])
        now = datetime.now(UTC)
        observation = report["observation"]
        if observation and parse_time(observation["valid_until"]) <= now:
            observation["verified"] = False
            report["observation_status"]["state"] = "stale"
        # Old report blobs may remain in backups; native probe fields have no
        # current authority in the service-backed workflow.
        report.pop("runtime", None)
        lifecycle = report["content_status"]
        if db is not None and lifecycle["effective_state"] not in {"ended", "cancelled"}:
            catalog = db.execute(
                "SELECT snapshot,seen_at FROM contents WHERE id=?", (row["content_id"],)
            ).fetchone()
            if catalog:
                feed = source_observation(
                    json.loads(catalog["snapshot"]),
                    catalog["seen_at"],
                    now,
                    now,
                    "teamarr",
                    self.settings.status_ttl,
                )
                feed.update(source="teamarr_feed", simulated=False)
                lifecycle.update(
                    observation=feed,
                    effective_state=feed["state"],
                    lookup_state="ok",
                    checked_at=now.isoformat(),
                    stale=False,
                    error=None,
                )
        evidence = lifecycle["observation"]
        if evidence and parse_time(evidence["valid_until"]) <= now:
            lifecycle["stale"] = True
            if evidence["state"] not in {"ended", "cancelled"}:
                lifecycle["effective_state"] = "unknown"
        return report

    def report(self, token):
        with self.db.transaction() as db:
            return self.project(self.get_row(db, token), db)

    def by_request(self, request_id):
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM executor_jobs WHERE request_id=?", (request_id,)).fetchone()
            return self.project(row, db) if row else None

    def write(self, db, token, report, **fields):
        report["revision"] += 1
        fields["report"] = encode(report)
        allowed = {
            "report",
            "state",
            "cancel_requested",
            "touched_device",
            "retired_at",
            "next_check",
            "completion_candidate",
        }
        if set(fields) - allowed:
            raise ValueError("Unknown executor record field")
        db.execute(
            "UPDATE executor_jobs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE token=?",
            (*fields.values(), token),
        )

    def allowed(self, db, token, *, navigation=False):
        row = self.get_row(db, token)
        device = self.db.device(db, row["device_id"])
        owner = db.execute("SELECT * FROM executor_devices WHERE device_id=?", (row["device_id"],)).fetchone()
        if (
            row["cancel_requested"]
            or not owner
            or row["intent"] != owner["highest_intent"]
            or row["intent"] <= owner["cancelled_through"]
            or (navigation and row["intent"] < device["intent_version"])
        ):
            raise ExecutorError(
                "superseded", "Playback intent no longer owns the device", status=409, retryable=False
            )
        if device.get("manual_control") or device.get("input_handoff"):
            raise ExecutorError(
                "manual_control", "Manual control owns the device", status=409, retryable=False
            )
        if navigation and (
            device["automation"] == "paused"
            or parse_time(json.loads(row["request"])["deadline_at"]) <= datetime.now(UTC)
        ):
            raise ExecutorError(
                "navigation_expired",
                "Navigation is paused or its deadline expired",
                status=409,
                retryable=False,
            )
        return row

    def phase(self, token, phase, *, attempt=None):
        with self.db.transaction() as db:
            row = self.allowed(db, token, navigation=True)
            report = json.loads(row["report"])
            report["operation"].update(state="navigating", phase=phase, updated_at=utc())
            if attempt:
                report["operation"]["attempts"].append(attempt)
            self.write(db, token, report, state="navigating")

    def fail(self, token, error):
        with self.db.transaction() as db:
            row = self.get_row(db, token)
            if row["cancel_requested"]:
                return
            report = json.loads(row["report"])
            state = "timed_out" if error.code in {"navigation_expired", "navigation_timeout"} else "failed"
            report["operation"].update(
                state=state, phase=error.code, error=error.detail(), updated_at=utc(), finished_at=utc()
            )
            report["cancellation"] = {"state": "requested", "input_quiescent": False}
            self.write(db, token, report, state=state, cancel_requested=1)

    def request_cancel(self, command):
        with self.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO executor_devices(device_id) VALUES (?)", (command.device_id,))
            if command.token:
                row = self.get_row(db, command.token)
                if row["device_id"] != command.device_id:
                    raise ExecutorError(
                        "device_mismatch", "Token belongs to another device", status=409, retryable=False
                    )
                rows = [row]
            else:
                db.execute(
                    "UPDATE executor_devices SET cancelled_through=MAX(cancelled_through,?) WHERE device_id=?",
                    (command.through_intent_version, command.device_id),
                )
                device = self.db.device(db, command.device_id)
                device["intent_version"] = max(device["intent_version"], command.through_intent_version)
                self.db.save_device(db, device)
                rows = db.execute(
                    "SELECT * FROM executor_jobs WHERE device_id=? AND intent<=?",
                    (command.device_id, command.through_intent_version),
                ).fetchall()
            tokens = []
            for row in rows:
                tokens.append(row["token"])
                if row["cancel_requested"] == 2 or row["request"] is None:
                    continue
                report = json.loads(row["report"])
                report["cancellation"] = {"state": "requested", "input_quiescent": False}
                if report["observation"]:
                    report["observation"]["verified"] = False
                    report["observation_status"]["state"] = "unavailable"
                self.write(db, row["token"], report, cancel_requested=1, next_check=0)
            return tokens

    def acknowledge_cancel(self, token, active_playback):
        with self.db.transaction() as db:
            row = self.get_row(db, token)
            report = json.loads(row["report"])
            if report["operation"]["state"] in {"accepted", "navigating"}:
                report["operation"].update(
                    state="cancelled", phase="cancelled", finished_at=utc(), updated_at=utc()
                )
            report["cancellation"] = {
                "state": "acknowledged",
                "input_quiescent": True,
                "active_playback": active_playback,
            }
            report["observation"] = None
            report["observation_status"] = {"state": "unavailable", "checked_at": utc(), "error": None}
            until = datetime.now(UTC) + timedelta(days=self.settings.executor.retention_days)
            report["retained_until"] = until.isoformat()
            self.write(db, token, report, state="cancelled", cancel_requested=2, retired_at=time.time())
            db.execute("UPDATE executor_devices SET current_token=NULL WHERE current_token=?", (token,))
            return active_playback

    def journal(self, token, action, evidence_id=None, *, cancelling=False):
        with self.db.transaction() as db:
            row = self.get_row(db, token) if cancelling else self.allowed(db, token, navigation=True)
            owner = db.execute(
                "SELECT * FROM executor_devices WHERE device_id=?", (row["device_id"],)
            ).fetchone()
            if cancelling:
                if owner["current_token"] != token or row["cancel_requested"] != 1:
                    return None
            else:
                db.execute(
                    "UPDATE executor_devices SET current_token=? WHERE device_id=?", (token, row["device_id"])
                )
                db.execute("UPDATE executor_jobs SET touched_device=1 WHERE token=?", (token,))
            cursor = db.execute(
                "INSERT INTO executor_actions(token,at,action,state,evidence_id) VALUES (?,?,?,'dispatching',?)",
                (token, utc(), action, evidence_id),
            )
            return cursor.lastrowid

    def journal_done(self, action_id, error=None):
        with self.db.transaction() as db:
            db.execute(
                "UPDATE executor_actions SET state=?,error=? WHERE id=?",
                ("uncertain" if error else "acknowledged", error, action_id),
            )

    def prune(self):
        with self.db.transaction() as db:
            cutoff = time.time() - self.settings.executor.retention_days * 86400
            rows = db.execute(
                "SELECT token FROM executor_jobs WHERE retired_at<? AND cancel_requested=2 AND request IS NOT NULL",
                (cutoff,),
            ).fetchall()
            for row in rows:
                # Keep the id/hash/token/intent tombstone so expiry cannot cause a relaunch.
                db.execute(
                    "UPDATE executor_jobs SET request=NULL,report='{}',completion_candidate=NULL WHERE token=?",
                    (row[0],),
                )
                db.execute("DELETE FROM executor_actions WHERE token=?", (row[0],))
