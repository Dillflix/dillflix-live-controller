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

    def cancel_device(self, device_id: str, through_intent_version: int) -> None: ...

    def observe(self, device_id: str) -> dict | None: ...


def simulated_selection(request):
    gti = "amzn1.dv.gti.simulated-live-sports"
    return dict(
        content_id=gti,
        title="Simulated live sports",
        handle="simulated-handle",
        viewing_option_id="prime-live:" + gti,
        reason="Simulated discovery selection",
        availability="live",
        identity_status="structural_slot_correlation",
        labels=["LIVE"],
        occurrences=[dict(page_id=request["discovery"]["enabled_pages"][0])],
    )


class SimulatedPlaybackAdapter:
    """Independent durable executor state lets delivery/reconciliation survive a restart.

    Repeated submit uses the original request ID. Cancellation ends navigation
    and matching active playback, never a newer target. Device cancellation is
    an acknowledged input barrier, including for requests not received yet.
    """

    def __init__(self, database, observation_ttl=15):
        self.db = database
        self.observation_ttl = observation_ttl

    def check_online(self, db):
        disconnect_at = self.db.meta(db, "simulated_executor_disconnect_at")
        if self.db.meta(db, "simulated_executor_outage", False) or (
            disconnect_at and time.time() >= disconnect_at
        ):
            raise ConnectionError("Simulated playback service is disconnected")

    @staticmethod
    def report(row):
        payload = json.loads(row["payload"])
        workflow = None
        if payload.get("purpose") == "page_refresh" and row["state"] == "completed":
            workflow = {
                "pages_result": {
                    "session_id": "simulator",
                    "observed_at": datetime.fromtimestamp(row["submitted_at"], UTC).isoformat(),
                    "pages": [
                        dict(id=id, title=title, available=True, kind=kind, position=i)
                        for i, (id, title, kind) in enumerate(
                            [("sports", "Sports", "primary"), ("dazn", "DAZN", "channel")]
                        )
                    ],
                }
            }
        elif payload.get("purpose") == "discovery" and row["state"] == "playing_verified":
            workflow = {"selected": simulated_selection(payload)}
        observation = json.loads(row["observation"]) if row["observation"] else None
        return {
            "prime_player": workflow,
            "request_id": row["id"],
            "executor_job_id": row["id"],
            "device_id": payload["device_id"],
            "intent_version": payload["intent_version"],
            "content_id": observation["content_id"] if observation else payload["content_id"],
            "state": row["state"],
            "observation": observation,
            "reason": row["error"],
        }

    def submit(self, request):
        discovery = request.get("schema_version") == 2
        if request["mode"] != "live" or (
            not discovery and request["content_id"] != request["content_snapshot"]["id"]
        ):
            raise ValueError("Playback request identity or presentation mismatch")
        with self.db.transaction() as db:
            self.check_online(db)
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
            if request["intent_version"] <= self.db.meta(
                db, f"simulated_cancelled_through:{request['device_id']}", -1
            ):
                state = "cancelled"
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
            self.check_online(db)
            row = db.execute("SELECT * FROM simulated_jobs WHERE id=?", (request_id,)).fetchone()
            if row is None:
                return None
            if row["state"] not in {"accepted", "navigating"}:
                return self.report(row)
            request = json.loads(row["payload"])
            highest = db.execute(
                "SELECT intent FROM simulated_devices WHERE device_id=?", (row["device_id"],)
            ).fetchone()[0]
            controls = request.get("content_snapshot", {}).get("_simulation", {})
            observation, reason = None, None
            if row["intent"] < highest:
                state = "superseded"
            elif request.get("purpose") == "page_refresh":
                state = "completed"
            elif controls.get("stall_navigation"):
                state = "navigating"
            elif controls.get("fail_playback"):
                state, reason = "failed", "All simulated routes failed"
            else:
                now = datetime.now(UTC)
                state = "playing_verified"
                selected = simulated_selection(request) if request.get("purpose") == "discovery" else None
                observation = {
                    "device_id": row["device_id"],
                    "content_id": "prime:" + selected["content_id"] if selected else request["content_id"],
                    "request_id": request_id,
                    "intent_version": row["intent"],
                    "viewing_option_id": selected["viewing_option_id"]
                    if selected
                    else request["allowed_viewing_options"][0]["id"],
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
            self.check_online(db)
            # Remember cancellation even if it arrives before an uncertain submit.
            db.execute("INSERT OR IGNORE INTO simulated_cancellations VALUES (?)", (request_id,))
            db.execute(
                "UPDATE simulated_jobs SET state='cancelled',observation=NULL WHERE id=? "
                "AND state IN ('accepted','navigating','playing_verified')",
                (request_id,),
            )
            for device in db.execute("SELECT * FROM simulated_devices WHERE observation IS NOT NULL"):
                if json.loads(device["observation"])["request_id"] == request_id:
                    db.execute(
                        "UPDATE simulated_devices SET observation=NULL WHERE device_id=?",
                        (device["device_id"],),
                    )

    def cancel_device(self, device_id, through_intent_version):
        with self.db.transaction() as db:
            self.check_online(db)
            key = f"simulated_cancelled_through:{device_id}"
            fence = max(through_intent_version, self.db.meta(db, key, -1))
            self.db.set_meta(db, key, fence)
            db.execute(
                "UPDATE simulated_jobs SET state='cancelled',observation=NULL WHERE device_id=? AND intent<=? "
                "AND state IN ('accepted','navigating','playing_verified')",
                (device_id, fence),
            )
            row = db.execute(
                "SELECT observation FROM simulated_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if row and row[0] and json.loads(row[0])["intent_version"] <= fence:
                db.execute("UPDATE simulated_devices SET observation=NULL WHERE device_id=?", (device_id,))

    def observe(self, device_id):
        with self.db.transaction() as db:
            self.check_online(db)
            device = db.execute("SELECT * FROM simulated_devices WHERE device_id=?", (device_id,)).fetchone()
            if not device or not device["observation"]:
                return None
            observation = json.loads(device["observation"])
            row = db.execute(
                "SELECT payload FROM simulated_jobs WHERE id=?", (observation["request_id"],)
            ).fetchone()
            controls = json.loads(row[0]).get("content_snapshot", {}).get("_simulation", {}) if row else {}
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
