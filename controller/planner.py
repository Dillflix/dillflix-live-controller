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


def content_view(snapshot, active, lifecycle, *, prime_search_at=None):
    event = snapshot.get("event") or {}
    teams = [t for t in [event.get("away_team_details"), event.get("home_team_details")] if t]
    league = event.get("league") or snapshot.get("competition") or "unknown"
    teams = [{**t, "key": team_key(t, league), "league": league} for t in teams]
    options = allowed_options(snapshot)
    # Teamarr's rule-based broadcasts deliberately report unknown live status.
    # A fresh listing may reach Prime's catalogue check, which still has to
    # confirm an entitled LIVE match before staging any playback request.
    unknown_broadcast = bool(
        active
        and snapshot["kind"] == "broadcast"
        and snapshot.get("status") == "unknown"
        and lifecycle["state"] == "unknown"
        and lifecycle.get("stale") is False
        and lifecycle.get("source") == "teamarr_feed"
    )
    prime_search = bool(
        prime_search_at is not None
        and (lifecycle["state"] == "scheduled" or unknown_broadcast)
        and parse_time(snapshot["start_time"]) <= prime_search_at
        and any(o.get("app") == "prime_video" for o in options)
    )
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
        "playable": bool(options) and (lifecycle["state"] == "live" or prime_search),
        "launch_eligibility": "broadcast_start_reached"
        if prime_search and unknown_broadcast
        else "scheduled_start_reached"
        if prime_search
        else "confirmed_live"
        if lifecycle["state"] == "live" and options
        else "ineligible",
        "availability_reason": "No valid viewing options"
        if not options
        else "Prime live availability check required"
        if prime_search and unknown_broadcast
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


def ordered_plan(device):
    """Stable order within each source; legacy commitments belong to admins."""
    return sorted(device["plan"], key=lambda p: p.get("actor", {}).get("type", "admin") == "user")


def choose(device, items, now, real_now):
    indexed = {e["content_id"]: e for e in items}
    current_id = (device.get("observed") or {}).get("content_id")
    access = device.get("prime_access", {})
    waiting = access.get(device.get("desired"), {})
    waiting_for_search = waiting.get("state") in {"waiting_for_feed", "access_unknown"}
    preferred_id = device.get("desired") if waiting_for_search else current_id
    committed = {p["content_id"] for p in device["plan"]}
    eligible = []
    candidates = []
    for item in items:
        if not item["active"] and item["content_id"] not in committed | {current_id}:
            continue
        evidence = access.get(item["content_id"], {})
        if evidence.get("options") == item["viewing_options"] and evidence.get("state") in {
            "feeds_unavailable",
            "feeds_locked",
            "no_matching_feed",
        }:
            continue
        failure = device.get("failures", {}).get(item["content_id"], {})
        cooldown = failure.get("retry_after")
        if item["playable"]:
            eligible.append(item)
            if not cooldown or real_now >= parse_time(cooldown):
                candidates.append(item)
    ids = {e["content_id"] for e in candidates}
    eligible_ids = {e["content_id"] for e in eligible}
    manual = next((p for p in ordered_plan(device) if p["content_id"] in eligible_ids), None)
    if manual:
        result = {
            "content_id": manual["content_id"],
            "manual": True,
            "rule_id": None,
            "reason": "Protected by your watch plan",
            "actor": manual.get("actor", {"type": "admin", "id": "legacy", "name": "Admin"}),
        }
        # Backoff delays another launch; it does not relinquish a live manual
        # commitment to an automatic event or a lower watch-plan entry.
        if manual["content_id"] not in ids:
            result.update(
                retry_after=device["failures"][manual["content_id"]]["retry_after"],
                reason="Watch-plan playback retry pending; waiting before another attempt",
            )
    else:
        candidates = [e for e in candidates if priority(device, e) < 1_000_000]
        candidates.sort(
            key=lambda e: (
                priority(device, e),
                team_priority(device, e),
                e["content_id"] != preferred_id,
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
    # A backoff timer expiring must not repeatedly abort the fallback search it
    # just enabled. This holds only that already-issued automatic attempt; new
    # manual intent, configuration, routes, lifecycle, and deadlines still win.
    attempt = device.get("automatic_attempt") or {}
    pending = indexed.get(attempt.get("content_id"))
    retried = result["content_id"]
    prior_retry = attempt.get("deferred_retries", {}).get(retried)
    if (
        not result["manual"]
        and pending
        and pending["content_id"] in ids
        and priority(device, pending) < 1_000_000
        and device.get("playback_state") == "navigating"
        and device.get("desired") == pending["content_id"]
        and attempt.get("intent_version") == device.get("intent_version")
        and attempt.get("revision") == device.get("revision")
        and attempt.get("options") == pending["viewing_options"]
        and real_now.timestamp() < attempt.get("deadline_at", 0)
        and prior_retry
        and prior_retry == device.get("failures", {}).get(retried, {}).get("retry_after")
    ):
        result = {
            "content_id": pending["content_id"],
            "manual": False,
            "rule_id": device["rules"][priority(device, pending)]["id"],
            "reason": "Finishing the current automatic playback attempt before retrying a failed event",
        }
    selected_access = access.get(result["content_id"], {})
    selected_item = indexed.get(result["content_id"])
    if (
        selected_item
        and selected_access.get("options") == selected_item["viewing_options"]
        and selected_access.get("state") in {"waiting_for_feed", "access_unknown"}
        and real_now < parse_time(selected_access["retry_after"])
    ):
        result.update(
            retry_after=selected_access["retry_after"],
            prime_readiness=selected_access["state"],
            reason=selected_access["reason"],
        )
    current = indexed.get(current_id)
    observed = device.get("observed") or {}
    recovery = device.get("recovery") or {}
    grace = recovery.get("retry_after")
    holding = bool(grace and recovery.get("content_id") == current_id and real_now < parse_time(grace))
    current_access = access.get(current_id, {})
    current_locked = (
        current
        and current_access.get("state") in {"feeds_unavailable",
            "feeds_locked", "no_matching_feed"}
        and current_access.get("options") == current["viewing_options"]
    )
    route_present = (
        current
        and not current_locked
        and observed.get("viewing_option_id") in {o["id"] for o in current["viewing_options"]}
    )
    leaving_current = (
        device.get("playback_state") == "navigating"
        or waiting.get("state") in {"waiting_for_feed", "access_unknown", "feeds_unavailable",
            "feeds_locked", "no_matching_feed"}
    ) and device.get("desired") != current_id
    if (
        route_present
        and not leaving_current
        and (
            (current["lifecycle"]["state"] == "unknown" and observed.get("verified"))
            or (holding and current["lifecycle"]["state"] in {"live", "unknown"})
        )
    ):
        order = [p["content_id"] for p in ordered_plan(device)]
        wins_manual = result["manual"] and (
            current_id not in order or order.index(result["content_id"]) < order.index(current_id)
        )
        if not wins_manual:
            return {
                "content_id": current_id,
                "manual": current_id in order,
                "rule_id": None,
                "reason": recovery.get("reason") or "Playback evidence is missing; allowing time for recovery"
                if holding
                else "Event status is stale; retaining verified playback"
                if current["lifecycle"].get("stale")
                else "Event live status is unknown; retaining verified playback",
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
        for p in ordered_plan(device)
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
