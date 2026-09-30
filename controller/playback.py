"""Playback boundary and a durable simulator. This module performs no device I/O."""

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .database import encode


class PlaybackAdapter(Protocol):
    def submit(self, request: dict) -> dict: ...

    def inspect(self, request_id: str) -> dict | None: ...

    def cancel(self, request_id: str) -> None: ...

    def observe(self, device_id: str) -> dict | None: ...


class SimulatedPlaybackAdapter:
    """Independent durable executor state lets delivery/reconciliation survive a restart.

    Repeated submit uses the original request ID. Cancellation ends navigation,
    not an already playing or newer target. Intent watermarks reject obsolete work.
    """

    def __init__(self, database, observation_ttl=15):
        self.db = database
        self.observation_ttl = observation_ttl

    @staticmethod
    def report(row):
        payload = json.loads(row["payload"])
        return {
            "request_id": row["id"],
            "executor_job_id": row["id"],
            "device_id": payload["device_id"],
            "intent_version": payload["intent_version"],
            "content_id": payload["content_id"],
            "state": row["state"],
            "observation": json.loads(row["observation"]) if row["observation"] else None,
            "reason": row["error"],
        }

    def submit(self, request):
        if request["mode"] != "live" or request["content_id"] != request["content_snapshot"]["id"]:
            raise ValueError("Playback request identity or presentation mismatch")
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM simulated_jobs WHERE id=?", (request["request_id"],)).fetchone()
            if row:
                if json.loads(row["payload"]) != request:
                    raise ValueError("Request ID reused with a different playback payload")
                return self.report(row)
            device = db.execute(
                "SELECT * FROM simulated_devices WHERE device_id=?", (request["device_id"],)
            ).fetchone()
            highest = device["intent"] if device else -1
            state = "superseded" if request["intent_version"] < highest else "accepted"
            if db.execute(
                "SELECT 1 FROM simulated_cancellations WHERE id=?", (request["request_id"],)
            ).fetchone():
                state = "cancelled"
            if (
                state == "accepted"
                and db.execute(
                    "SELECT 1 FROM simulated_jobs WHERE device_id=? AND intent=?",
                    (request["device_id"], request["intent_version"]),
                ).fetchone()
            ):
                raise ValueError("This device intent already belongs to another request")
            if state == "accepted":
                db.execute(
                    "INSERT INTO simulated_devices(device_id,intent) VALUES (?,?) "
                    "ON CONFLICT(device_id) DO UPDATE SET intent=excluded.intent",
                    (request["device_id"], request["intent_version"]),
                )
                db.execute(
                    "UPDATE simulated_jobs SET state='superseded' WHERE device_id=? AND intent<? "
                    "AND state IN ('accepted','navigating')",
                    (request["device_id"], request["intent_version"]),
                )
            db.execute(
                "INSERT INTO simulated_jobs(id,device_id,intent,payload,state,submitted_at) VALUES (?,?,?,?,?,?)",
                (
                    request["request_id"],
                    request["device_id"],
                    request["intent_version"],
                    encode(request),
                    state,
                    time.time(),
                ),
            )
            return self.report(
                db.execute("SELECT * FROM simulated_jobs WHERE id=?", (request["request_id"],)).fetchone()
            )

    def inspect(self, request_id):
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM simulated_jobs WHERE id=?", (request_id,)).fetchone()
            if row is None:
                return None
            if row["state"] not in {"accepted", "navigating"}:
                return self.report(row)
            request = json.loads(row["payload"])
            highest = db.execute(
                "SELECT intent FROM simulated_devices WHERE device_id=?", (row["device_id"],)
            ).fetchone()[0]
            controls = request["content_snapshot"].get("_simulation", {})
            observation, reason = None, None
            if row["intent"] < highest:
                state = "superseded"
            elif controls.get("stall_navigation"):
                state = "navigating"
            elif controls.get("fail_playback"):
                state, reason = "failed", "All simulated routes failed"
            else:
                now = datetime.now(UTC)
                state = "playing_verified"
                observation = {
                    "device_id": row["device_id"],
                    "content_id": request["content_id"],
                    "request_id": request_id,
                    "intent_version": row["intent"],
                    "viewing_option_id": request["allowed_viewing_options"][0]["id"],
                    "presentation": "replay" if controls.get("replay_result") else "live",
                    "verified": True,
                    "simulated": True,
                    "observed_at": now.isoformat(),
                    "valid_until": (now + timedelta(seconds=self.observation_ttl)).isoformat(),
                    "health": "healthy",
                }
                db.execute(
                    "UPDATE simulated_devices SET observation=? WHERE device_id=?",
                    (encode(observation), row["device_id"]),
                )
            db.execute(
                "UPDATE simulated_jobs SET state=?,observation=?,error=? WHERE id=?",
                (state, encode(observation) if observation else None, reason, request_id),
            )
            return self.report(
                db.execute("SELECT * FROM simulated_jobs WHERE id=?", (request_id,)).fetchone()
            )

    def cancel(self, request_id):
        with self.db.transaction() as db:
            # Remember cancellation even if it arrives before an uncertain submit.
            db.execute("INSERT OR IGNORE INTO simulated_cancellations VALUES (?)", (request_id,))
            db.execute(
                "UPDATE simulated_jobs SET state='cancelled' WHERE id=? AND state IN ('accepted','navigating')",
                (request_id,),
            )

    def observe(self, device_id):
        with self.db.transaction() as db:
            device = db.execute("SELECT * FROM simulated_devices WHERE device_id=?", (device_id,)).fetchone()
            if not device or not device["observation"]:
                return None
            observation = json.loads(device["observation"])
            row = db.execute(
                "SELECT payload FROM simulated_jobs WHERE id=?", (observation["request_id"],)
            ).fetchone()
            controls = json.loads(row[0])["content_snapshot"].get("_simulation", {}) if row else {}
            if controls.get("observation_outage"):
                raise ConnectionError("Simulated playback observation outage")
            now = datetime.now(UTC)
            observation.update(
                observed_at=now.isoformat(),
                valid_until=(now + timedelta(seconds=self.observation_ttl)).isoformat(),
            )
            db.execute(
                "UPDATE simulated_devices SET observation=? WHERE device_id=?",
                (encode(observation), device_id),
            )
            return observation
