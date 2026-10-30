"""Small, retained-state projection for public video overlays; never polls a device."""

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from .current_playback import current_playback
from .planner import parse_time
from .prime_player.client import RECOVERING_ADAPTATION_STATES

LEASE_SECONDS = 5
HEADLINES = {
    "waiting": "Waiting for live sports",
    "switching": "Switching live coverage",
    "recovering": "Reconnecting live coverage",
    "standby": "Live coverage on standby",
}


class BroadcastOverlay(BaseModel):
    schema_version: Literal[1] = 1
    device_id: str
    device_name: str
    generated_at: str
    valid_until: str
    visible: bool
    state: Literal["hidden", "waiting", "switching", "recovering", "standby"]
    headline: str | None
    event_title: str | None


def _time(value):
    try:
        result = parse_time(value)
        return result if result and result.tzinfo else None
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def _deadline(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _pending(service, db, device, now):
    """Only this stream's current, unfinished, unexpired intent can name a target."""
    row = db.execute(
        "SELECT * FROM jobs WHERE device_id=? AND intent=? AND content_id=? "
        "AND state='pending' ORDER BY rowid DESC LIMIT 1",
        (device["id"], device["intent_version"], device.get("desired")),
    ).fetchone()
    if not row:
        return None, None, None
    deadline = _deadline(row["deadline_at"])
    if deadline is None or deadline <= now:
        return None, None, None
    content = db.execute("SELECT * FROM contents WHERE id=?", (row["content_id"],)).fetchone()
    completed = device["manual_completions"].get(row["content_id"])
    if completed:
        return None, None, None
    if content:
        check = db.execute("SELECT * FROM content_status WHERE content_id=?", (row["content_id"],)).fetchone()
        lifecycle = service.content_lifecycle(content, check, service.now(db), now, True, completed)
        if lifecycle["state"] in {"ended", "cancelled"}:
            return None, None, None
    snapshot = json.loads(content["snapshot"]) if content else {}
    return row, deadline, snapshot.get("title") or None


def _recovery_title(service, db, device, now):
    """Name interrupted physical playback during the gap before a retry job exists."""
    observed = device.get("observed") or {}
    content_id = observed.get("content_id")
    recovery_id = (device.get("recovery") or {}).get("content_id")
    if (not content_id or observed.get("simulated") is not False
            or (recovery_id and recovery_id != content_id)
            or device.get("desired") not in {None, content_id}
            or device["manual_completions"].get(content_id)):
        return None
    # Expired evidence can identify the interrupted event, but does not establish
    # current playback. Keep its original device/request association intact.
    job = db.execute("SELECT content_id FROM jobs WHERE id=? AND device_id=?",
                     (observed.get("request_id"), device["id"])).fetchone()
    if not job or job["content_id"] != content_id:
        return None
    content = db.execute("SELECT * FROM contents WHERE id=?", (content_id,)).fetchone()
    if not content:
        return None
    check = db.execute("SELECT * FROM content_status WHERE content_id=?", (content_id,)).fetchone()
    lifecycle = service.content_lifecycle(content, check, service.now(db), now, True, None)
    if lifecycle["state"] in {"ended", "cancelled"}:
        return None
    return json.loads(content["snapshot"]).get("title") or None


def _possibly_started(db, device, job, now):
    """An accepted controller request or phase name alone never proves a Play."""
    row = db.execute(
        "SELECT e.*,o.highest_intent,o.cancelled_through,o.current_token FROM executor_jobs e "
        "LEFT JOIN executor_devices o ON o.device_id=e.device_id "
        "WHERE e.request_id=? AND e.device_id=? AND e.intent=? AND e.content_id=?",
        (job["id"], device["id"], job["intent"], job["content_id"]),
    ).fetchone()
    if not row or row["cancel_requested"] or row["retired_at"] is not None:
        return False, None
    if (row["state"] not in {"accepted", "navigating", "playing_verified"}
            or row["highest_intent"] != job["intent"]
            or row["cancelled_through"] >= job["intent"]
            or row["current_token"] != row["token"]):
        return False, None
    report = json.loads(row["report"])
    if any(report.get(key) != value for key, value in {
        "device_id": device["id"], "request_id": job["id"],
        "intent_version": job["intent"], "content_id": job["content_id"],
    }.items()):
        return False, None
    operation = report.get("operation") or {}
    deadline = _time(operation.get("deadline_at"))
    if (deadline is None or deadline <= now
            or (operation.get("finished_at") and operation.get("state") != "playing_verified")
            or operation.get("state") not in {"accepted", "navigating", "playing_verified"}
            or (report.get("cancellation") or {}).get("state", "none") != "none"):
        return False, None
    workflow = report.get("prime_player") or {}
    attempt, session = workflow.get("attempt_id"), workflow.get("session_id")
    selected = workflow.get("selected") or {}
    if not attempt or not session or not selected.get("content_id"):
        return False, None
    # Health reads publish the replacement runtime/scope before rejecting an old
    # attempt. Its acknowledgement must not hide a later Prime restart while the
    # worker is still reconciling that old report. Missing scope is legitimate
    # when managed networking is disabled.
    scope = device.get("prime_network_scope")
    if isinstance(scope, dict) and scope.get("context_id") != workflow.get("network_context_id"):
        return False, None
    adaptation = device.get("prime_runtime_adaptation") or {}
    if adaptation.get("session_id") and adaptation["session_id"] != session:
        return False, None
    options = json.loads(job["payload"]).get("allowed_viewing_options") or []
    if selected.get("viewing_option_id") not in {option.get("id") for option in options}:
        return False, None
    outcome = workflow.get("launch_outcome") or {}
    if outcome:
        if any(outcome.get(key) != value for key, value in {
            "attempt_id": attempt, "session_id": session, "requested_id": selected["content_id"],
        }.items()):
            return False, None
        if workflow.get("network_context_id") and outcome.get("network_context_id") != workflow["network_context_id"]:
            return False, None
        if (selected.get("parent_content_id") and outcome.get("resolved_id")
                and outcome["resolved_id"] != selected["content_id"]):
            return False, None
        evidence = outcome.get("evidence") or {}
        disposition = (evidence.get("launch") or {}).get("disposition")
        if disposition == "not_invoked" or outcome.get("state") in {"failed", "cancelled", "stopped"}:
            return False, None
        resolution = evidence.get("resolution") or {}
        if (outcome.get("state") in {"launch_requested", "observing", "playing"}
                and outcome.get("resolved_id")
                and resolution.get("playbackClass") == "live_watch_now"):
            return True, deadline
        if outcome.get("state") == "unknown" and disposition == "unknown":
            return True, deadline
        return False, None
    # The action is journaled immediately before dispatch. A response/write can be
    # lost after real playback starts; preserve that ambiguity only to the existing
    # deadline, not forever. attempt_id is persisted earlier and is insufficient.
    action = db.execute(
        "SELECT at,state FROM executor_actions WHERE token=? AND action='PRIME_PLAY' "
        "ORDER BY id DESC LIMIT 1", (row["token"],),
    ).fetchone()
    dispatched = _time(action["at"]) if action else None
    possible = bool(action and action["state"] in {"dispatching", "uncertain", "acknowledged"}
                    and dispatched and dispatched <= now + timedelta(seconds=5) and dispatched < deadline)
    return possible, deadline if possible else None


def broadcast_overlay(service, device_id, *, now=None):
    """Read one snapshot and at most the current/target events, without catalogue work."""
    now = now or datetime.now(UTC)
    until = now + timedelta(seconds=LEASE_SECONDS)
    with service.db.snapshot() as db:
        device = service.db.device(db, device_id)
        state, title = "waiting", None
        if device.get("manual_control") or device.get("input_handoff"):
            state = "standby"
        else:
            current = current_playback(service, db, device_id, now=now)
            if current.state == "playing" and current.simulated is False:
                state = "hidden"
                until = min(until, _time(current.valid_until))
            elif device["automation"] == "paused":
                state = "standby"
            else:
                job, deadline, target_title = _pending(service, db, device, now)
                started, started_until = _possibly_started(db, device, job, now) if job else (False, None)
                if started:
                    state = "hidden"
                    until = min(until, deadline, started_until)
                else:
                    adaptation_state = (device.get("prime_runtime_adaptation") or {}).get("state")
                    network_state = (device.get("prime_network") or {}).get("state")
                    purpose = json.loads(job["payload"]).get("purpose") if job else None
                    # A provider refusal is not a connection outage. Conversely,
                    # normal runtime/VPN preparation for a new request is a switch,
                    # even while its runtime is validating or its network changes.
                    recovery = bool(device.get("recovery") or device.get("player_recovery")
                                    or (device.get("executor_health") or {}).get("state") == "offline"
                                    or network_state == "recovering" or adaptation_state == "backoff"
                                    or purpose == "recovery")
                    preparing = network_state == "transitioning" or adaptation_state in RECOVERING_ADAPTATION_STATES
                    state = "standby" if adaptation_state == "contract_incompatible" else (
                        "recovering" if recovery else "switching" if job else "recovering" if preparing else "waiting"
                    )
                    if job:
                        title = target_title
                        until = min(until, deadline)
                    elif state == "recovering":
                        title = _recovery_title(service, db, device, now)
        return BroadcastOverlay(
            device_id=device_id, device_name=device["name"], generated_at=now.isoformat(),
            valid_until=until.isoformat(), visible=state != "hidden", state=state,
            headline=HEADLINES.get(state), event_title=title,
        )
