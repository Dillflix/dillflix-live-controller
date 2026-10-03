"""Read-only explanation of recent controller decisions and Prime observations."""

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def redact(value):
    """Exclude credentials even when an upstream error embeds structured context."""
    if isinstance(value, dict):
        return {
            k: "[redacted]"
            if any(
                s in k.lower()
                for s in ("password", "secret", "authorization", "api_key", "access_token", "cookie")
            )
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def collect(path, device_id="living-room"):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        device = db.execute("SELECT id,revision,payload FROM devices WHERE id=?", (device_id,)).fetchone()
        if not device:
            raise KeyError(device_id)
        activity = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM activity WHERE device_id=? ORDER BY sequence DESC LIMIT 1000", (device_id,)
            )
        ]
        controller_jobs = [
            {k: r[k] for k in r.keys() if k != "payload"}
            for r in db.execute(
                "SELECT * FROM jobs WHERE device_id=? ORDER BY rowid DESC LIMIT 100", (device_id,)
            )
        ]
        jobs = []
        for row in db.execute(
            "SELECT * FROM executor_jobs WHERE device_id=? ORDER BY rowid DESC LIMIT 100", (device_id,)
        ):
            report = json.loads(row["report"])
            prime = report.get("prime_player") or {}
            jobs.append(
                {
                    "token": row["token"],
                    "request_id": row["request_id"],
                    "state": row["state"],
                    "cancel_requested": row["cancel_requested"],
                    "next_check": row["next_check"],
                    "content_id": report.get("content_id"),
                    "operation": report.get("operation"),
                    "observation_status": report.get("observation_status"),
                    "observation": report.get("observation"),
                    "cancellation": report.get("cancellation"),
                    "session_id": prime.get("session_id"),
                    "attempt_id": prime.get("attempt_id"),
                    "playback_status": prime.get("playback_status"),
                    "stop_result": prime.get("stop_result"),
                    "stop_error": prime.get("stop_error"),
                    "workflow": prime,
                }
            )
        actions = [
            dict(r)
            for r in db.execute(
                "SELECT a.* FROM executor_actions a JOIN executor_jobs j ON a.token=j.token WHERE j.device_id=? ORDER BY a.id DESC LIMIT 1000",
                (device_id,),
            )
        ]
        return redact(
            {
                "schema_version": 1,
                "captured_at": datetime.now(UTC).isoformat(),
                "device": {
                    **json.loads(device["payload"]),
                    "id": device["id"],
                    "revision": device["revision"],
                },
                "activity": activity,
                "controller_jobs": controller_jobs,
                "playbacks": jobs,
                "executor_actions": actions,
                "catalog_health": {
                    r["key"]: json.loads(r["value"])
                    for r in db.execute(
                        "SELECT key,value FROM metadata WHERE key IN ('feed_health','team_directory_health','maintenance_health')"
                    )
                },
                "limits": {"activity": 1000, "jobs": 100, "executor_actions": 1000},
                "excluded": [
                    "credentials",
                    "request/source snapshots",
                    "screenshots",
                    "host journal and Docker stdout",
                ],
            }
        )


if __name__ == "__main__":
    print(json.dumps(collect(os.environ.get("CONTROLLER_DATABASE", "data/controller.sqlite3")), indent=2))
