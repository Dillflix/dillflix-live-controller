"""Persist subscription evidence shared by background and foreground work."""

import hashlib
import json
from datetime import UTC, datetime

from .database import encode
from .planner import allowed_options
from .prime_player.labels import prime_option


def identity(item):
    snapshot = item["snapshot"]
    event = snapshot.get("event") or {}
    return hashlib.sha256(encode([
        snapshot.get("id"), snapshot.get("title"), snapshot.get("start_time"),
        snapshot.get("competition"), snapshot.get("kind"),
        event.get("away_team_details"), event.get("home_team_details"), item["viewing_options"],
    ]).encode()).hexdigest()


def record_entitled(db, device_id, request, selected, *, source, provenance):
    """Accept an already matched, fenced positive observation, never readiness alone.

    Callers validate current ownership/session or bound playback first. A route or
    event identity change rejects the old result. Invalidate the background check
    token so a late timeout/negative cannot replace this stronger observation.
    """
    if not selected or selected.get("entitlement_status") != "ENTITLED":
        return
    snapshot = request["content_snapshot"]
    options = request["allowed_viewing_options"]
    if not any(o["id"] == selected.get("viewing_option_id") and prime_option(o) for o in options):
        return
    current = db.execute("SELECT snapshot FROM contents WHERE id=? AND source='teamarr'",
                         (snapshot["id"],)).fetchone()
    if not current:
        return
    current = json.loads(current[0])
    fingerprint = identity({"snapshot": snapshot, "viewing_options": options})
    if fingerprint != identity({"snapshot": current, "viewing_options": allowed_options(current)}):
        return
    row = db.execute("SELECT e.status,v.evidence FROM event_entitlements e JOIN entitlement_read_models v "
                     "ON v.device_id=e.device_id AND v.content_id=e.content_id "
                     "WHERE e.device_id=? AND e.content_id=?",
                     (device_id, snapshot["id"])).fetchone()
    if row and row["status"] == "entitled" and json.loads(row["evidence"]).get("identity") == fingerprint:
        return  # Routine monitoring must not refresh a conclusive check or cancel an admin recheck.
    evidence = {"identity": fingerprint, "source": source,
                "reason": "Included with your subscription", "selected": selected,
                **provenance}
    db.execute("INSERT OR IGNORE INTO event_entitlements(device_id,content_id) VALUES (?,?)",
               (device_id, snapshot["id"]))
    db.execute("UPDATE event_entitlements SET state='complete',status='entitled',failures=0,"
               "next_check=NULL,check_id=NULL,manual=0,checked_at=?,evidence=? "
               "WHERE device_id=? AND content_id=?",
               (datetime.now(UTC).isoformat(), encode(evidence), device_id, snapshot["id"]))
