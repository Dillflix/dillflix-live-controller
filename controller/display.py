"""Compact display projection of retained controller evidence; no external I/O."""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from .artwork import image_url
from .current_playback import current_playback
from .launch_progress import PHASES
from .planner import ordered_plan, parse_time, priority

MAX_EVENTS = 100


class DisplayTeam(BaseModel):
    abbreviation: str | None
    full_name: str | None
    logo_url: str | None


class DisplayArtwork(BaseModel):
    league_logo_url: str | None
    away_team_logo_url: str | None
    home_team_logo_url: str | None


class DisplayLifecycle(BaseModel):
    state: str
    stale: bool
    observed_at: str | None
    effective_valid_until: str | None


class DisplayEvent(BaseModel):
    content_id: str
    title: str
    league: str
    kind: str
    start_time: str | None
    expected_end_time: str | None
    end_time_estimated: bool | None
    teams: list[DisplayTeam]
    scores: list[int | float | None]
    status_detail: str | None
    lifecycle: DisplayLifecycle
    planned: bool
    priority: int
    artwork: DisplayArtwork


class DisplayPlayback(BaseModel):
    state: Literal["playing", "idle", "unverified"]
    status: Literal["playing", "starting", "waiting", "paused", "manual", "rechecking", "unavailable"]
    detail: str
    observed_at: str | None
    valid_until: str | None
    simulated: bool | None
    event: DisplayEvent | None
    last_event: DisplayEvent | None
    target_event: DisplayEvent | None


class DisplaySchedule(BaseModel):
    state: Literal["ok", "stale", "unavailable"]
    observed_at: str | None
    valid_until: str | None


class DisplayStream(BaseModel):
    device_id: str
    name: str
    playback: DisplayPlayback


class DisplayView(BaseModel):
    schema_version: Literal[1] = 1
    device_id: str
    device_name: str
    streams: list[DisplayStream]
    generated_at: str
    timezone: str
    playback: DisplayPlayback
    events: list[DisplayEvent]
    schedule: DisplaySchedule


def display_event(item, device, planned):
    """Allowlist presentation fields and preserve away/home positional identity."""
    event = item["snapshot"].get("event") or {}
    sides = [event.get("away_team_details"), event.get("home_team_details")]
    teams = [
        DisplayTeam(
            abbreviation=(side or {}).get("abbreviation"),
            full_name=(side or {}).get("full_name"),
            logo_url=image_url((side or {}).get("logo_url")),
        )
        for side in sides
    ] if any(sides) else []
    lifecycle = item["lifecycle"]
    artwork = item["artwork"]
    return DisplayEvent(
        **{key: item[key] for key in (
            "content_id", "title", "league", "kind", "start_time", "expected_end_time",
            "end_time_estimated", "scores", "status_detail",
        )},
        teams=teams,
        lifecycle=DisplayLifecycle(**{key: lifecycle[key] for key in (
            "state", "stale", "observed_at", "effective_valid_until",
        )}),
        planned=item["content_id"] in planned,
        priority=priority(device, item),
        artwork=DisplayArtwork(**{key: image_url(artwork.get(key)) for key in (
            "league_logo_url", "away_team_logo_url", "home_team_logo_url",
        )}),
    )


def playback_status(db, device, current):
    """Use fixed public-facing labels, never raw exception or command details."""
    if device.get("manual_control") or device.get("input_handoff"):
        return "manual", "Manual control"
    if device["automation"] == "paused":
        return "paused", "Automation paused"
    if device["playback_state"] == "navigating":
        row = db.execute(
            "SELECT j.progress,e.report FROM jobs j LEFT JOIN executor_jobs e ON e.request_id=j.id "
            "WHERE j.device_id=? AND j.intent=? AND j.content_id=? AND j.state='pending' "
            "ORDER BY j.rowid DESC LIMIT 1",
            (device["id"], device["intent_version"], device.get("desired")),
        ).fetchone()
        report = json.loads(row["report"]) if row and row["report"] else {}
        phase = (report.get("operation") or {}).get("phase") or (row["progress"] if row else None)
        return "starting", PHASES.get(phase, "Starting playback")
    if current.state == "playing":
        return "playing", "Verified live playback"
    if (device.get("executor_health") or {}).get("state") == "offline":
        return "unavailable", "Playback service unavailable"
    if current.state == "unverified":
        return "rechecking", "Rechecking playback"
    if (device.get("prime_network") or {}).get("state") in {"transitioning", "recovering"}:
        return "starting", "Recovering playback service"
    return "waiting", "Waiting for live sports"


def display_view(service, device_id):
    """Read one WAL snapshot without taking the database's writer reservation."""
    with service.db.snapshot() as db:
        now = datetime.now(UTC)
        device = service.db.device(db, device_id)
        items = service.items(db, device_id)
        planned = {entry["content_id"]: i for i, entry in enumerate(ordered_plan(device))}
        indexed = {item["content_id"]: item for item in items}

        def card(content_id):
            item = indexed.get(content_id)
            return display_event(item, device, planned) if item else None

        clock = service.now(db)
        eligible = [item for item in items if (
            item["active"]
            and (item.get("entitlement") or {}).get("status") == "entitled"
            and (item["lifecycle"]["state"] in {"live", "scheduled"} or (
                item["lifecycle"]["state"] == "unknown" and parse_time(item["start_time"]) > clock
            ))
        )]
        def order(item):
            return (
                planned.get(item["content_id"], MAX_EVENTS + len(planned)),
                priority(device, item), parse_time(item["start_time"]), item["content_id"],
            )

        eligible.sort(key=order)
        live = [item for item in eligible if item["lifecycle"]["state"] == "live"]
        upcoming = [item for item in eligible if item["lifecycle"]["state"] != "live"]
        # A busy future schedule must not crowd every live event out (or vice versa).
        # Reserve half for each group and let the other use any unused capacity.
        upcoming = upcoming[:MAX_EVENTS - min(len(live), MAX_EVENTS // 2)]
        eligible = sorted(live[:MAX_EVENTS - len(upcoming)] + upcoming, key=order)
        health = service.feed_health(db)
        observed = parse_time(health.get("last_success"))
        until = observed + timedelta(seconds=max(60, 2 * service.settings.feed_interval)) if observed else None
        schedule_state = "unavailable" if not observed else (
            "ok" if health["state"] == "ok" and until > now else "stale"
        )
        streams = []
        for binding in service.settings.configured_devices:
            stream_device = service.db.device(db, binding.id)
            current = current_playback(service, db, binding.id, now=now)
            status, detail = playback_status(db, stream_device, current)
            playback = DisplayPlayback(
                state=current.state, status=status, detail=detail,
                observed_at=current.observed_at, valid_until=current.valid_until,
                simulated=current.simulated if current.simulated is not None
                else service.settings.executor.mode == "simulator",
                event=card(current.event.content_id) if current.event else None,
                last_event=card((stream_device.get("observed") or {}).get("content_id"))
                if current.state != "playing" else None,
                target_event=card(stream_device.get("desired")),
            )
            streams.append(DisplayStream(device_id=binding.id, name=stream_device["name"], playback=playback))
        return DisplayView(
            device_id=device_id, device_name=device["name"], streams=streams,
            generated_at=now.isoformat(),
            timezone=device["preferences"]["timezone"],
            playback=next(stream.playback for stream in streams if stream.device_id == device_id),
            events=[display_event(item, device, planned) for item in eligible],
            schedule=DisplaySchedule(
                state=schedule_state,
                observed_at=observed.isoformat() if observed else None,
                valid_until=until.isoformat() if until else None,
            ),
        )
