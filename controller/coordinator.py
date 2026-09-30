"""Durable playback coordination, independent of executor implementation."""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

from .database import encode
from .planner import choose, parse_time
from .recovery import PlaybackRecovery, compatible_options


class PlaybackCoordinator(PlaybackRecovery):
    def owns_device(self, db, device_id):
        row = db.execute(
            "SELECT owner,expires FROM leases WHERE resource=?", (f"device:{device_id}",)
        ).fetchone()
        return row is not None and row["owner"] == self.owner and row["expires"] > time.time()

    def stage_playback(self):
        real = datetime.now(UTC)
        with self.db.transaction() as db:
            if not self.db.lease(db, "device:living-room", self.owner, time.time()):
                return False
            d = self.db.device(db)
            if d.get("manual_control") or d.get("input_handoff") or d["automation"] == "paused":
                return True
            if d.get("executor_health", {}).get("state") == "offline":
                d["reason"] = "Playback service unavailable; waiting for recovery. Watch plan retained."
                self.db.save_device(db, d)
                return True
            items = self.items(db)
            indexed = {i["content_id"]: i for i in items}
            now = self.now(db)
            decision = choose(d, items, now, real)
            d["force_switch"] = False
            target = decision["content_id"]
            d["reason"] = decision["reason"]
            d["next_candidate"] = decision.get("next_candidate")
            pending = db.execute(
                "SELECT * FROM jobs WHERE device_id=? AND intent=? AND state='pending'",
                (d["id"], d["intent_version"]),
            ).fetchone()
            observed = d.get("observed") or {}
            if observed.get("verified") and observed.get("content_id") == d.get("retry_playback"):
                d["retry_playback"] = None
            previous_job = db.execute(
                "SELECT * FROM jobs WHERE id=?", (observed.get("request_id"),)
            ).fetchone()
            options = indexed[target]["viewing_options"] if target else []
            changed_pending = bool(
                pending
                and target == pending["content_id"]
                and not compatible_options(json.loads(pending["payload"])["allowed_viewing_options"], options)
            )
            handoff = bool(
                target
                and observed.get("content_id") == target
                and previous_job
                and not compatible_options(
                    json.loads(previous_job["payload"])["allowed_viewing_options"],
                    options,
                    observed.get("viewing_option_id"),
                )
            )
            recovery = d.get("recovery") or {}
            retry_due = recovery.get("retry_after") and real >= parse_time(recovery["retry_after"])
            retry = bool(
                target
                and observed.get("content_id") == target
                and not observed.get("verified")
                and (retry_due or d.get("retry_playback") == target)
                and indexed[target]["playable"]
            )
            missing_job = bool(target and not pending and d["playback_state"] == "navigating")
            needs_request = (
                target != d.get("desired")
                or changed_pending
                or missing_job
                or (
                    not pending
                    and (handoff or retry or (target and d["playback_state"] in {"waiting", "failed"}))
                )
            )
            # Retaining unknown current playback is not permission to reopen it.
            if target and not indexed[target]["playable"]:
                needs_request = False
            if needs_request:
                d["intent_version"] += 1
                d["desired"] = target
                db.execute(
                    "UPDATE jobs SET state='superseded' WHERE device_id=? AND state='pending'", (d["id"],)
                )
                if target:
                    item = indexed[target]
                    request_id = str(uuid.uuid4())
                    payload = {
                        "schema_version": 1,
                        "request_id": request_id,
                        "device_id": d["id"],
                        "intent_version": d["intent_version"],
                        "content_id": target,
                        "mode": "live",
                        "content_snapshot_schema_version": 1,
                        "content_snapshot": item["snapshot"],
                        "allowed_viewing_options": item["viewing_options"],
                        "purpose": "route_handoff"
                        if handoff or changed_pending
                        else "recovery"
                        if retry
                        else "selection",
                        "previous_request_id": observed.get("request_id"),
                    }
                    if retry and not handoff:
                        recovery["attempts"] = recovery.get("attempts", 0) + 1
                        d["recovery"] = recovery
                    d["retry_playback"] = None
                    budget = self.settings.navigation_timeout
                    if self.settings.mode == "demo" and item["snapshot"].get("_simulation", {}).get(
                        "stall_navigation"
                    ):
                        budget = 3  # Short, visible deadline for the explicit timeout scenario.
                    db.execute(
                        "INSERT INTO jobs(id,device_id,intent,content_id,state,ready_at,payload,deadline_at,progress) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            request_id,
                            d["id"],
                            d["intent_version"],
                            target,
                            "pending",
                            time.time() + self.settings.simulation_delay,
                            encode(payload),
                            time.time() + budget,
                            "queued",
                        ),
                    )
                    d["playback_state"] = "navigating"
                    self.db.log(
                        db,
                        now.isoformat(),
                        f"Opening {item['title']}",
                        "Updating coverage for the same event"
                        if handoff or changed_pending
                        else "Recovering live playback"
                        if retry
                        else decision["reason"],
                        "navigation",
                    )
                else:
                    d["playback_state"], d["observed"] = "waiting", None
                    self.db.log(db, now.isoformat(), "Waiting for live sports", decision["reason"])
            self.db.save_device(db, d)
            return True

    def fail_attempt(self, db, d, job, reason, state="failed"):
        prior = d["failures"].get(job["content_id"], {})
        attempts = prior.get("attempts", 0) + 1
        wait = 5 if attempts == 1 else 15 if attempts == 2 else 300
        d["failures"][job["content_id"]] = {
            "attempts": attempts,
            "retry_after": (datetime.now(UTC) + timedelta(seconds=wait)).isoformat(),
            "reason": reason,
        }
        db.execute("UPDATE jobs SET state=?,progress=?,error=? WHERE id=?", (state, state, reason, job["id"]))
        d["playback_state"] = "failed"
        self.db.log(
            db,
            self.now(db).isoformat(),
            f"Playback {state.replace('_', ' ')}",
            f"{reason}. Watch plan retained; retry after {wait} seconds.",
            "failure",
            d["id"],
        )

    def observation_error(self, job, observation):
        if not isinstance(observation, dict):
            return "Playback verification is missing"
        identity = {
            "device_id": job["device_id"],
            "request_id": job["id"],
            "intent_version": job["intent"],
            "content_id": job["content_id"],
        }
        if any(observation.get(k) != v for k, v in identity.items()):
            return "Playback observation identity does not match the request"
        if (
            observation.get("presentation") != "live"
            or observation.get("verified") is not True
            or observation.get("health") != "healthy"
        ):
            return "Requested live playback was not verified"
        options = json.loads(job["payload"])["allowed_viewing_options"]
        if observation.get("viewing_option_id") not in {o["id"] for o in options}:
            return "Playback used an option outside the permitted set"
        try:
            observed = parse_time(observation.get("observed_at"))
            until = parse_time(observation.get("valid_until"))
            now = datetime.now(UTC)
            if (
                not observed
                or not until
                or not (
                    observed <= now + timedelta(seconds=5)
                    and until > now
                    and until > observed
                    and now - observed < timedelta(seconds=self.settings.observation_ttl)
                )
            ):
                return "Playback observation is stale or has invalid timing"
        except (TypeError, ValueError, AttributeError):
            return "Playback observation has invalid timing"
        return None

    def receive_playback_report(self, request_id, report):
        """One guarded path for inspection responses and future callbacks."""
        with self.db.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (request_id,)).fetchone()
            if not job or job["state"] != "pending" or not self.owns_device(db, job["device_id"]):
                return False
            d = self.db.device(db, job["device_id"])
            items = self.items(db)
            decision = choose(d, items, self.now(db), datetime.now(UTC))
            item = next((i for i in items if i["content_id"] == job["content_id"]), None)
            if (
                d.get("manual_control")
                or d.get("input_handoff")
                or d["automation"] == "paused"
                or job["intent"] != d["intent_version"]
                or job["content_id"] != d["desired"]
                or decision["content_id"] != job["content_id"]
                or not item
                or not item["playable"]
                or not compatible_options(
                    json.loads(job["payload"])["allowed_viewing_options"], item["viewing_options"]
                )
            ):
                db.execute("UPDATE jobs SET state='superseded' WHERE id=?", (request_id,))
                return False
            if job["deadline_at"] is not None and job["deadline_at"] <= time.time():
                self.fail_attempt(db, d, job, "Navigation exceeded its deadline", "timed_out")
            elif not isinstance(report, dict) or any(
                report.get(k) != v
                for k, v in {
                    "request_id": job["id"],
                    "device_id": job["device_id"],
                    "intent_version": job["intent"],
                    "content_id": job["content_id"],
                }.items()
            ):
                self.fail_attempt(
                    db, d, job, "Executor response identity does not match the request", "rejected"
                )
            elif report.get("state") in {"accepted", "navigating"}:
                if job["progress"] != report["state"]:
                    self.db.log(
                        db,
                        self.now(db).isoformat(),
                        "Playback request " + report["state"],
                        "Awaiting verified live playback",
                        "navigation",
                        d["id"],
                    )
                db.execute(
                    "UPDATE jobs SET executor_job_id=?,progress=?,error=NULL WHERE id=?",
                    (report.get("executor_job_id"), report["state"], request_id),
                )
            elif report.get("state") == "playing_verified":
                error = self.observation_error(job, report.get("observation"))
                if error:
                    self.fail_attempt(db, d, job, error, "rejected")
                else:
                    same_event = (d.get("observed") or {}).get("content_id") == job["content_id"]
                    d["observed"] = report["observation"]
                    d["playback_state"] = "verified"
                    if not same_event:
                        d["started_at"], d["last_switch_at"] = (
                            self.now(db).isoformat(),
                            datetime.now(UTC).isoformat(),
                        )
                    self.note_verified_playback(d, datetime.now(UTC))
                    d["failures"].pop(job["content_id"], None)
                    db.execute(
                        "UPDATE jobs SET state='verified',progress='playing_verified',executor_job_id=?,error=NULL WHERE id=?",
                        (report.get("executor_job_id"), request_id),
                    )
                    self.db.log(
                        db,
                        self.now(db).isoformat(),
                        "Simulated live playback verified",
                        "Requested content and live presentation matched the observation",
                        "verified",
                        d["id"],
                    )
            else:
                self.fail_attempt(
                    db, d, job, str(report.get("reason") or "Executor did not verify live playback")[:1000]
                )
            self.db.save_device(db, d)
            return db.execute("SELECT state FROM jobs WHERE id=?", (request_id,)).fetchone()[0] in {
                "pending",
                "verified",
            }

    def deliver_pending(self):
        with self.db.transaction() as db:
            jobs = [
                dict(r)
                for r in db.execute("SELECT * FROM jobs WHERE state='pending' AND device_id='living-room'")
            ]
        for job in jobs:
            with self.db.transaction() as db:
                if not self.owns_device(db, job["device_id"]):
                    return
                if not self.executor_available(db):
                    return
                d = self.db.device(db, job["device_id"])
                if (
                    d.get("manual_control")
                    or d.get("input_handoff")
                    or d["automation"] == "paused"
                    or job["intent"] != d["intent_version"]
                ):
                    continue
                if job["deadline_at"] is None:
                    job["deadline_at"] = time.time() + self.settings.navigation_timeout
                    db.execute("UPDATE jobs SET deadline_at=? WHERE id=?", (job["deadline_at"], job["id"]))
                if time.time() >= job["deadline_at"]:
                    self.fail_attempt(db, d, job, "Navigation exceeded its deadline", "timed_out")
                    self.db.save_device(db, d)
                    continue
                if job["ready_at"] > time.time():
                    continue
            # Calls happen after committing coordinator state; never hold a DB lock across executor I/O.
            try:
                report = self.inspect_playback(job["id"])
                if report is None:
                    with self.db.transaction() as db:
                        db.execute(
                            "UPDATE jobs SET delivery_attempts=delivery_attempts+1 WHERE id=?", (job["id"],)
                        )
                    report = self.playback.submit(json.loads(job["payload"]))
                if not self.receive_playback_report(job["id"], report):
                    continue
                if report.get("state") in {"accepted", "navigating"}:
                    report = self.inspect_playback(job["id"])
                    if report is not None:
                        self.receive_playback_report(job["id"], report)
            except Exception as exc:
                with self.db.transaction() as db:
                    if self.owns_device(db, job["device_id"]):
                        db.execute(
                            "UPDATE jobs SET ready_at=?,progress='retrying',error=? WHERE id=? AND state='pending'",
                            (
                                time.time() + min(2 ** min(job["delivery_attempts"] + 1, 4), 15),
                                f"{type(exc).__name__}: executor delivery or inspection failed",
                                job["id"],
                            ),
                        )

    def inspect_playback(self, request_id):
        report = self.playback.inspect(request_id)
        # A historical successful job alone is not a current device observation.
        if isinstance(report, dict) and report.get("state") == "playing_verified":
            observation = self.playback.observe(report["device_id"])
            if isinstance(observation, dict) and observation.get("request_id") == request_id:
                report = {**report, "observation": observation}
        return report

    def cancel_obsolete(self):
        with self.db.transaction() as db:
            if not self.owns_device(db, "living-room") or not self.executor_available(db):
                return
            jobs = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM jobs WHERE device_id='living-room' AND state IN ('cancelled','superseded','failed','timed_out','rejected') AND cancel_sent=0"
                )
            ]
        for job in jobs:
            try:
                self.playback.cancel(job["id"])
            except Exception:
                continue  # Durable cancellation remains queued for a later tick/restart.
            with self.db.transaction() as db:
                db.execute("UPDATE jobs SET cancel_sent=1 WHERE id=?", (job["id"],))

    def tick(self):
        with self.playback_lock:
            self._tick()

    def _tick(self):
        with self.db.transaction() as db:
            if not self.db.lease(db, "device:living-room", self.owner, time.time()):
                return
        self.reconcile_input_handoff()
        self.refresh_playback_observation()
        if not self.stage_playback():
            return
        self.cancel_obsolete()
        self.deliver_pending()
        self.cancel_obsolete()
