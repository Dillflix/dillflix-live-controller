"""Read-only, device-scoped projection of accepted live playback evidence."""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from .artwork import matchup_thumbnail
from .planner import parse_time


class PlayingEvent(BaseModel):
    content_id: str
    title: str
    thumbnail_url: str | None
    kind: str
    league: str | None
    start_time: str | None
    expected_end_time: str | None
    end_time_estimated: bool | None


class NowPlaying(BaseModel):
    device_id: str
    state: Literal["playing", "idle", "unverified"]
    playback_state: str
    simulated: bool | None
    observed_at: str | None
    valid_until: str | None
    event: PlayingEvent | None


def current_playback(service, db, device_id, *, now=None):
    """Project accepted evidence in the caller transaction; perform no I/O."""
    now = now or datetime.now(UTC)
    device = service.db.device(db, device_id)
    observed = device.get("observed")
    result = NowPlaying(
        device_id=device_id,
        state="unverified" if observed else "idle",
        playback_state=device["playback_state"],
        simulated=observed.get("simulated") if observed else None,
        observed_at=observed.get("observed_at") if observed else None,
        valid_until=None,
        event=None,
    )
    if not observed or device.get("manual_control") or device.get("input_handoff"):
        return result
    job = db.execute(
        "SELECT * FROM jobs WHERE id=? AND device_id=?", (observed.get("request_id"), device_id)
    ).fetchone()
    if not job or service.observation_error(job, observed, now=now):
        return result
    row = db.execute("SELECT * FROM contents WHERE id=?", (observed["content_id"],)).fetchone()
    if not row:
        return result
    check = db.execute(
        "SELECT * FROM content_status WHERE content_id=?", (observed["content_id"],)
    ).fetchone()
    lifecycle = service.content_lifecycle(
        row,
        check,
        service.now(db),
        now,
        True,
        device["manual_completions"].get(observed["content_id"]),
    )
    if lifecycle["state"] in {"ended", "cancelled"}:
        return result
    snapshot = json.loads(row["snapshot"])
    result.state = "playing"
    result.valid_until = min(
        parse_time(observed["valid_until"]),
        parse_time(observed["observed_at"]) + timedelta(seconds=service.settings.playback_evidence_ttl),
    ).isoformat()
    result.event = PlayingEvent(
        content_id=snapshot["id"],
        title=snapshot["title"],
        thumbnail_url=matchup_thumbnail(snapshot.get("artwork") or {}),
        kind=snapshot["kind"],
        league=(snapshot.get("event") or {}).get("league") or snapshot.get("competition"),
        start_time=snapshot.get("start_time"),
        expected_end_time=snapshot.get("expected_end_time"),
        end_time_estimated=snapshot.get("end_time_estimated"),
    )
    return result
