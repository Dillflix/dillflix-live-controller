"""Shared planning and independent, server-configured playback streams."""

import copy
import json
from datetime import timedelta

from .planner import choose, entitlement_eligible, ordered_plan, parse_time, preview_plan
from .entitlement_store import identity


SHARED_FIELDS = ("plan", "rules", "team_ranks", "manual_completions", "public_access")


def catalogue_reservation(device, items, real):
    """Keep an issued, bounded read assigned while temporary status ages out.

    This reserves selection work only. The coordinator still requires current
    eligibility and the completed Prime decision before issuing playback.
    """
    assignment = device.get("stream_assignment") or {}
    pending = assignment.get("catalogue") or {}
    item = next((item for item in items if item["content_id"] == assignment.get("content_id")), None)
    if (
        pending and item
        and assignment.get("revision") == device["revision"]
        and pending.get("intent") == device.get("intent_version")
        and real.timestamp() < pending.get("expires", 0)
        and device["automation"] == "active"
        and not device.get("manual_control") and not device.get("input_handoff")
        and (item["active"] or item["content_id"] in {p["content_id"] for p in device["plan"]})
        and (item["playable"] or item["lifecycle"]["state"] == "unknown")
        and entitlement_eligible(item)
        and pending.get("identity") == identity(item)
    ):
        return item["content_id"]
    return None


def planning_fields(device):
    return {**{key: copy.deepcopy(device.get(key, {} if key != "plan" else []))
               for key in SHARED_FIELDS},
            "preferences": {key: copy.deepcopy(value) for key, value in device["preferences"].items()
                            if key != "vpn_policy"}}


def apply_planning(device, fields):
    for key in SHARED_FIELDS:
        device[key] = copy.deepcopy(fields[key])
    device["preferences"] = {**copy.deepcopy(fields["preferences"]),
                             **({"vpn_policy": device["preferences"]["vpn_policy"]}
                                if "vpn_policy" in device["preferences"] else {})}


def validate_saved_plan(service, db):
    """Reject ambiguous migration before any startup cleanup can change intent."""
    devices = {row[0]: service.db.device(db, row[0]) for row in db.execute("SELECT id FROM devices")}
    configured = {device.id for device in service.settings.configured_devices}
    if devices.keys() - configured:
        raise ValueError("Device configuration omits saved streams; retain their IDs and pause them instead")
    if not devices:
        return
    canonical = service.db.meta(db, "fleet_plan", planning_fields(devices[service.device_id]))
    conflicting = [key for key, device in devices.items() if planning_fields(device) != canonical]
    if conflicting:
        raise ValueError(
            "Saved stream plans differ from the shared plan: " + ", ".join(conflicting)
            + "; reconcile the saved plans before enabling shared playback. No plan was overwritten."
        )


def synchronize_plan(service, db):
    """Commit shared edits and all projected device revisions atomically.

    Device payloads retain the legacy contract. The canonical metadata record
    detects edits, including confirmed completion and undo, before commit.
    Playback, ownership, network evidence and VPN preferences are never copied.
    """
    if len(service.runtimes) < 2:
        return
    original = service.db.meta(db, "fleet_plan")
    devices = [service.db.device(db, device_id) for device_id in service.runtimes]
    changed = [planning_fields(device) for device in devices if planning_fields(device) != original]
    if not changed:
        return
    fields = changed[0]
    if any(value != fields for value in changed[1:]):
        raise ValueError("Conflicting shared-plan edits in one transaction")
    for device in devices:
        if planning_fields(device) != fields:
            apply_planning(device, fields)
            device["revision"] += 1
            device["force_switch"] = True
            service.db.save_device(db, device)
    service.db.set_meta(db, "fleet_plan", fields)


def select_for_stream(service, db, device, items, now, real, *, reserve=True):
    """Reserve each event before catalogue work so simultaneous ticks cannot duplicate it.

    Claims include pending launches and retained playback. Loss of monitoring
    alone cannot safely transfer a physical stream: the former owner must retire
    its attempt first. A paused stream keeps its current playback claim.
    """
    from .planner import priority, team_priority

    runtimes = service.root.runtimes
    if len(runtimes) < 2:
        return choose(device, items, now, real)

    def available_stream(key, value):
        return (value["automation"] == "active" and not value.get("manual_control")
                and not value.get("input_handoff") and not value.get("player_recovery")
                and value.get("executor_health", {}).get("state") != "offline"
                and not getattr(getattr(runtimes[key], "executor", None), "stopping", False))

    devices, claims = {}, {}
    for device_id in runtimes:
        other = devices[device_id] = device if device_id == device["id"] else service.db.device(db, device_id)
        reserved = claims[device_id] = set()
        observation = other.get("observed") or {}
        # Persisted attempts remain exclusive even when observation expires.
        for row in db.execute(
            "SELECT content_id FROM executor_jobs WHERE device_id=? AND touched_device=1 "
            "AND retired_at IS NULL AND cancel_requested!=2 AND state!='completed'", (device_id,)
        ):
            reserved.add(row[0])
        if observation.get("content_id") and other.get("playback_state") in {"verified", "unverified"}:
            reserved.add(observation["content_id"])
        pending = db.execute("SELECT content_id FROM jobs WHERE device_id=? AND state='pending'",
                             (device_id,)).fetchall()
        reserved.update(row[0] for row in pending)
        assignment = other.get("stream_assignment") or {}
        if (assignment.get("content_id") and assignment.get("revision") == other["revision"]
                and available_stream(device_id, other)):
            reserved.add(assignment["content_id"])
    reserved = set().union(*(value for key, value in claims.items() if key != device["id"]))
    available = [item for item in items if item["content_id"] not in reserved]
    idle = {key: value for key, value in devices.items() if not claims[key] and available_stream(key, value)}
    assignments = {}
    held = catalogue_reservation(device, items, real)
    if held and held not in reserved:
        # Do not let one scheduler tick replace a still-running catalogue read
        # with the previously playing event when feed status briefly expires.
        return {"content_id": held, "manual": held in {p["content_id"] for p in device["plan"]},
                "rule_id": None, "reason": "Checking Prime catalogue before switching playback"}
    if device["id"] in idle:
        occupied = set().union(*claims.values())
        candidates, indexed = {}, {}
        # N choices per stream suffice: at most N-1 other idle streams can
        # occupy earlier choices. Keep choose's local access/retry/policy rules.
        for key, other in idle.items():
            local = items if key == device["id"] else runtimes[key].items(db, key)
            remaining = [item for item in local if item["content_id"] not in occupied]
            options = candidates[key] = {}
            for _ in range(len(idle)):
                selected = choose(other, remaining, now, real)
                content_id = selected["content_id"]
                if not content_id:
                    break
                options[content_id] = selected
                indexed[content_id] = next(item for item in remaining if item["content_id"] == content_id)
                remaining = [item for item in remaining if item["content_id"] != content_id]
        plan_order = {entry["content_id"]: index for index, entry in reversed(list(enumerate(ordered_plan(device))))}

        def event_order(content_id):
            item = indexed[content_id]
            if content_id in plan_order:
                return (0, plan_order[content_id], 0, "", content_id)
            return (1, priority(device, item), team_priority(device, item), item["start_time"], content_id)

        # Augmenting paths can move an unstarted assignment to another eligible
        # stream, preserving higher-priority events while filling spare capacity.
        # Physical/pending/previously reserved work never enters this matching.
        order = {key: index for index, key in enumerate(runtimes)}

        def place(content_id, visited):
            eligible = sorted((key for key in idle if content_id in candidates[key]),
                              key=lambda key: (key in assignments, len(candidates[key]), order[key]))
            for key in eligible:
                if key in visited:
                    continue
                visited.add(key)
                prior = assignments.get(key)
                if prior is None or place(prior, visited):
                    assignments[key] = content_id
                    return True
            return False

        for content_id in sorted(indexed, key=event_order):
            place(content_id, set())
            if len(assignments) == len(idle):
                break
        assigned = assignments.get(device["id"])
        decision = candidates[device["id"]][assigned] if assigned else choose(device, [], now, real)
        if reserve:
            # Reserve the entire idle allocation in this transaction. Otherwise
            # the next tick could recompute a different symmetric assignment.
            for key, other in idle.items():
                other["stream_assignment"] = {
                    "content_id": assignments.get(key), "revision": other["revision"],
                    "assigned_at": real.isoformat(),
                }
                service.db.save_device(db, other)
    else:
        decision = choose(device, available, now, real)
    if reserve and device["id"] not in idle:
        device["stream_assignment"] = {
            "content_id": decision["content_id"], "revision": device["revision"],
            "assigned_at": real.isoformat(),
        }
        service.db.save_device(db, device)
    if not decision["content_id"]:
        if any(item["active"] and item["playable"] and item["content_id"] not in reserved
               and (item.get("entitlement") or {}).get("status") == "unknown"
               and not entitlement_eligible(item) for item in items):
            decision["reason"] = "Checking Prime access for other live events"
        elif reserved or assignments:
            decision["reason"] = "Waiting for another eligible event; other streams have the current assignments"
    return decision


def stream_summaries(service, db):
    from .current_playback import current_playback

    result = []
    for device_id, runtime in service.root.runtimes.items():
        device = service.db.device(db, device_id)
        accepted = current_playback(runtime, db, device_id)
        event = accepted.model_dump().get("event")
        target = device.get("desired") or (device.get("stream_assignment") or {}).get("content_id")
        row = db.execute("SELECT snapshot FROM contents WHERE id=?", (target,)).fetchone() if target else None
        snapshot = json.loads(row[0]) if row else {}
        result.append({
            "id": device_id, "name": device["name"], "playback_state": device["playback_state"],
            "automation": device["automation"], "verified": accepted.state == "playing",
            "status": "manual" if device.get("manual_control") else
                      "paused" if device["automation"] == "paused" else
                      "starting" if device["playback_state"] == "navigating" else
                      "playing" if accepted.state == "playing" else
                      "unavailable" if device.get("executor_health", {}).get("state") == "offline" else
                      "unverified" if accepted.state == "unverified" else "waiting",
            "current_event": {"content_id": event["content_id"], "title": event["title"]} if event else None,
            "target_event": {"content_id": target, "title": snapshot.get("title", target)} if target else None,
            "reason": "Manual control" if device.get("manual_control") else
                      "Automation paused" if device["automation"] == "paused" else
                      "Verified live playback" if accepted.state == "playing" else
                      "Preparing playback" if device["playback_state"] == "navigating" else
                      "Waiting for playback",
        })
    return result


def planning_preview(service, db, device, items, now):
    if len(service.root.runtimes) < 2:
        return preview_plan(device, items, now)
    devices = [service.db.device(db, key) for key in service.root.runtimes]
    devices = [d for d in devices if d["automation"] == "active" and not d.get("manual_control")]
    indexed = {item["content_id"]: item for item in items}
    entries = [indexed[p["content_id"]] for p in ordered_plan(device)
               if p["content_id"] in indexed
               and indexed[p["content_id"]]["lifecycle"]["state"] not in {"ended", "cancelled"}
               and entitlement_eligible(indexed[p["content_id"]])]
    bounds = {now, now + timedelta(hours=24)}
    unknown = []
    for item in entries:
        start, end = parse_time(item["start_time"]), parse_time(item["expected_end_time"])
        if not start or not end:
            unknown.append(item["content_id"])
            continue
        bounds.update(t for t in (start, end) if now <= t <= now + timedelta(hours=24))
    segments, conflicts, assigned, last = [], [], {}, {}
    times = sorted(bounds)
    for start, end in zip(times, times[1:]):
        active = [item["content_id"] for item in entries if parse_time(item["start_time"])
                  and parse_time(item["expected_end_time"])
                  and parse_time(item["start_time"]) <= start < parse_time(item["expected_end_time"])]
        winners = active[:len(devices)]
        for blocked in active[len(devices):]:
            for winner in winners:
                pair = [winner, blocked]
                if pair not in conflicts:
                    conflicts.append(pair)
        assigned = {key: value for key, value in assigned.items() if value in winners}
        remaining = [value for value in winners if value not in assigned.values()]
        for stream in devices:
            key = stream["id"]
            if key not in assigned and remaining:
                assigned[key] = remaining.pop(0)
            target = assigned.get(key)
            if key in last and last[key]["content_id"] == target:
                last[key]["end"] = end.isoformat()
            else:
                segment = {"device_id": key, "device_name": stream["name"], "start": start.isoformat(),
                           "end": end.isoformat(), "content_id": target, "estimated": True}
                segments.append(segment)
                last[key] = segment
    return {"segments": sorted(segments, key=lambda item: (item["start"], item["device_id"])),
            "conflicts": conflicts, "unknown_timing": unknown,
            "note": f"Estimated shared plan across {len(devices)} active streams. Actual live status, "
                    "device availability and access determine playback; gaps use automatic selection."}
