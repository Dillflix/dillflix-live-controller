"""One acquisition budget across search, network changes and verification."""

import json
import time
from datetime import datetime

MAX_ACQUISITION_SECONDS = 300


def migrate_acquisition_timeout(db):
    """Tighten saved policies and pending coordinator work without replaying it."""
    now = time.time()
    deadlines = {}
    for job_id, device_id, intent, ready_at, deadline, report in db.execute(
        "SELECT j.id,j.device_id,j.intent,j.ready_at,j.deadline_at,e.report "
        "FROM jobs j LEFT JOIN executor_jobs e ON e.request_id=j.id WHERE j.state='pending'"
    ).fetchall():
        started = min(now, ready_at)
        if report:
            operation = json.loads(report)["operation"]
            if operation["state"] == "playing_verified":
                continue  # Acquisition already succeeded; preserve live playback.
            started = min(started, datetime.fromisoformat(operation["created_at"]).timestamp())
        bounded = min(deadline, started + MAX_ACQUISITION_SECONDS) if deadline is not None else started + MAX_ACQUISITION_SECONDS
        if bounded != deadline:
            db.execute("UPDATE jobs SET deadline_at=? WHERE id=?", (bounded, job_id))
        deadlines[device_id, intent] = bounded

    def tighten_policy(value):
        policy = value.get("preferences", {}).get("vpn_policy")
        if not isinstance(policy, dict):
            return False
        timeout = policy.get("acquisition_timeout", MAX_ACQUISITION_SECONDS)
        if type(timeout) is int and timeout > MAX_ACQUISITION_SECONDS:
            policy["acquisition_timeout"] = MAX_ACQUISITION_SECONDS
            return True
        return False

    for device_id, raw in db.execute("SELECT id,payload FROM devices").fetchall():
        value = json.loads(raw)
        changed = tighten_policy(value)
        automatic = value.get("automatic_attempt") or {}
        deadline = deadlines.get((device_id, automatic.get("intent_version")))
        if deadline is not None and automatic.get("deadline_at", deadline) > deadline:
            automatic["deadline_at"] = deadline
            changed = True
        if changed:
            db.execute("UPDATE devices SET payload=?,revision=revision+1 WHERE id=?", (json.dumps(value), device_id))
    row = db.execute("SELECT value FROM metadata WHERE key='fleet_plan'").fetchone()
    if row:
        value = json.loads(row[0])
        if tighten_policy(value):
            db.execute("UPDATE metadata SET value=? WHERE key='fleet_plan'", (json.dumps(value),))
