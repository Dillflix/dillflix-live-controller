"""Read-only, device-scoped projection of accepted live playback evidence."""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

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


def matchup_thumbnail(artwork):
    """Use Teamarr's game-thumbs matchup identity, never infer teams or a host."""
    logo = artwork.get("matchup_logo_url")
    if not isinstance(logo, str):
        return None
    try:
        source = urlsplit(logo)
        if source.scheme not in {"http", "https"} or not source.netloc:
            return None
        prefix, _, filename = source.path.rpartition("/")
        if filename not in {"logo", "logo.png"}:
            return None
        # The companion cover has the event artwork style, while the logo
        # has the transparent-logo style. Only reuse a cover of this matchup.
        cover = urlsplit(artwork.get("cover_url") or "")
        if (
            cover.scheme == source.scheme
            and cover.netloc == source.netloc
            and cover.path in {prefix + "/cover", prefix + "/cover.png"}
        ):
            source = cover
        return urlunsplit(source._replace(path=prefix + "/thumb.png"))
    except ValueError:
        return None


def now_playing(service, device_id):
    # Read only the observed event and its owning request. Polling this endpoint
    # must not build the full catalogue or dispatch player/status/navigation RPCs.
    with service.db.transaction() as db:
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
        if not job or service.observation_error(job, observed):
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
            datetime.now(UTC),
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
