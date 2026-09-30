"""Deterministic policy. No device I/O, database writes, or inferred game completion."""

from datetime import UTC, datetime, timedelta


def parse_time(value):
    if not value:
        return None
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Timestamps must include a UTC offset")
    return value.astimezone(UTC)


def team_key(team, league):
    return f"{team.get('provider', '')}:{league}:{team['id']}" if team and team.get("id") else None


def phase_of(snapshot):
    event = snapshot.get("event") or {}
    phase = str(event.get("season_type") or "").lower()
    # Only explicit provider values; do not guess from event names or the calendar.
    if phase in {"postseason", "post-season", "playoffs"}:
        return "playoffs"
    if phase in {"regular", "regular season", "regular-season"}:
        return "regular"
    return phase or "unknown"


def allowed_options(snapshot):
    accepted = []
    for option in snapshot.get("viewing_options", []):
        if option.get("decision") == "excluded" or option.get("presentation") in {"replay", "highlights"}:
            continue
        reasons = set(option.get("reasons", []))
        if snapshot["kind"] != "broadcast" and (
            "multi_event_coverage_not_full_game" in reasons
            or option.get("coverage_type") in {"partial", "multi_event", "redzone", "highlights"}
        ):
            continue
        accepted.append(option)  # Preserve every field and original order, including review reasons.
    return accepted


def content_view(snapshot, active, lifecycle):
    event = snapshot.get("event") or {}
    teams = [t for t in [event.get("away_team_details"), event.get("home_team_details")] if t]
    league = event.get("league") or snapshot.get("competition") or "unknown"
    teams = [{**t, "key": team_key(t, league), "league": league} for t in teams]
    options = allowed_options(snapshot)
    return {
        "content_id": snapshot["id"],
        "kind": snapshot["kind"],
        "title": snapshot["title"],
        "league": event.get("league") or snapshot.get("competition") or "unknown",
        "sports": snapshot.get("sports", []),
        "source": snapshot.get("source"),
        "phase": phase_of(snapshot),
        "teams": teams,
        "artwork": snapshot.get("artwork") or {},
        "start_time": snapshot["start_time"],
        "expected_end_time": snapshot.get("expected_end_time"),
        "end_time_estimated": snapshot.get("end_time_estimated"),
        "active": bool(active),
        "lifecycle": lifecycle,
        "viewing_options": options,
        "playable": lifecycle["state"] == "live" and bool(options),
        "availability_reason": "No valid viewing options"
        if not options
        else ("Awaiting fresh live status" if lifecycle["state"] == "unknown" else None),
        "scores": [event.get("away_score"), event.get("home_score")],
        "status_detail": event.get("status_detail"),
        "snapshot": snapshot,
    }


def matches(rule, item):
    return (
        rule.get("enabled", True)
        and (rule.get("league", "all") == "all" or rule["league"] == item["league"])
        and (rule.get("phase", "any") == "any" or rule["phase"] == item["phase"])
        and (not rule.get("team_id") or rule["team_id"] in [t["key"] for t in item["teams"]])
        and (not rule.get("source") or rule["source"] == item["source"])
        and (not rule.get("kind") or rule["kind"] == item["kind"])
    )


def priority(device, item):
    return next((i for i, rule in enumerate(device["rules"]) if matches(rule, item)), 1_000_000)


def team_priority(device, item):
    ordered = device["team_ranks"].get(item["league"], [])
    return min((ordered.index(t["key"]) for t in item["teams"] if t["key"] in ordered), default=1_000_000)


def choose(device, items, now, real_now):
    indexed = {e["content_id"]: e for e in items}
    current_id = (device.get("observed") or {}).get("content_id")
    committed = {p["content_id"] for p in device["plan"]}
    candidates = []
    for item in items:
        if not item["active"] and item["content_id"] not in committed | {current_id}:
            continue
        failure = device.get("failures", {}).get(item["content_id"], {})
        cooldown = failure.get("retry_after")
        if item["playable"] and (not cooldown or real_now >= parse_time(cooldown)):
            candidates.append(item)
    ids = {e["content_id"] for e in candidates}
    manual = next((p for p in device["plan"] if p["content_id"] in ids), None)
    if manual:
        result = {
            "content_id": manual["content_id"],
            "manual": True,
            "rule_id": None,
            "reason": "Protected by your watch plan",
        }
    else:
        candidates = [e for e in candidates if priority(device, e) < 1_000_000]
        candidates.sort(
            key=lambda e: (
                priority(device, e),
                team_priority(device, e),
                e["content_id"] != current_id,
                e["start_time"],
                e["content_id"],
            )
        )
        target = candidates[0] if candidates else None
        rule = device["rules"][priority(device, target)] if target else None
        result = {
            "content_id": target["content_id"] if target else None,
            "manual": False,
            "rule_id": rule["id"] if rule else None,
            "reason": f"Priority {priority(device, target) + 1} · {rule['name']}"
            if rule
            else "Waiting for an eligible live event",
        }
    current = indexed.get(current_id)
    observed = device.get("observed") or {}
    recovery = device.get("recovery") or {}
    grace = recovery.get("retry_after")
    holding = bool(grace and recovery.get("content_id") == current_id and real_now < parse_time(grace))
    route_present = current and observed.get("viewing_option_id") in {
        o["id"] for o in current["viewing_options"]
    }
    leaving_current = device.get("playback_state") == "navigating" and device.get("desired") != current_id
    if (
        route_present
        and not leaving_current
        and (
            (current["lifecycle"]["state"] == "unknown" and observed.get("verified"))
            or (holding and current["lifecycle"]["state"] in {"live", "unknown"})
        )
    ):
        order = [p["content_id"] for p in device["plan"]]
        wins_manual = result["manual"] and (
            current_id not in order or order.index(result["content_id"]) < order.index(current_id)
        )
        if not wins_manual:
            return {
                "content_id": current_id,
                "manual": current_id in order,
                "rule_id": None,
                "reason": "Playback evidence is missing; allowing time for recovery"
                if holding
                else "Status is stale; retaining existing verified playback",
            }
    # Dwell/cooldown decides whether to start a switch. Once navigation is approved,
    # the previous event's timer must not reverse it while we await verification.
    if device.get("force_switch") or device.get("playback_state") == "navigating":
        return result
    if (
        current
        and observed.get("verified")
        and current_id in ids
        and not result["manual"]
        and result["content_id"] != current_id
    ):
        prefs = device["preferences"]
        started = parse_time(device.get("started_at"))
        switched = parse_time(device.get("last_switch_at"))
        target = indexed.get(result["content_id"])
        same_tier = target and priority(device, target) == priority(device, current)
        dwell = started and (now - started).total_seconds() < prefs["minimum_viewing_seconds"]
        cooldown = switched and (real_now - switched).total_seconds() < prefs["switch_cooldown_seconds"]
        if (same_tier and not prefs["same_tier_switching"]) or dwell or cooldown:
            return {
                "content_id": current_id,
                "manual": False,
                "rule_id": None,
                "reason": "Current event retained by your automatic switching policy",
                "next_candidate": result,
            }
    return result


def preview_plan(device, items, now):
    """Schedule estimates for UX only; never used as lifecycle or execution commands."""
    indexed = {i["content_id"]: i for i in items}
    entries = [
        indexed[p["content_id"]]
        for p in device["plan"]
        if p["content_id"] in indexed
        and indexed[p["content_id"]]["lifecycle"]["state"] not in {"ended", "cancelled"}
    ]
    conflicts, unknown = [], []
    bounds = {now, now + timedelta(hours=24)}
    for i, e in enumerate(entries):
        start, end = parse_time(e["start_time"]), parse_time(e["expected_end_time"])
        if not end:
            unknown.append(e["content_id"])
            continue
        bounds.update(t for t in (start, end) if now <= t <= now + timedelta(hours=24))
        for other in entries[i + 1 :]:
            other_end = parse_time(other["expected_end_time"])
            if other_end and start < other_end and parse_time(other["start_time"]) < end:
                conflicts.append([e["content_id"], other["content_id"]])
    segments = []
    times = sorted(bounds)
    for start, end in zip(times, times[1:]):
        winner = next(
            (
                e
                for e in entries
                if parse_time(e["start_time"]) <= start
                and parse_time(e["expected_end_time"])
                and start < parse_time(e["expected_end_time"])
            ),
            None,
        )
        content_id = winner["content_id"] if winner else None
        if segments and segments[-1]["content_id"] == content_id:
            segments[-1]["end"] = end.isoformat()
        else:
            segments.append(
                {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "content_id": content_id,
                    "estimated": True,
                }
            )
    return {
        "conflicts": conflicts,
        "segments": segments,
        "unknown_timing": unknown,
        "note": "Estimated manual windows. Actual live status controls playback; gaps use automatic selection.",
    }
