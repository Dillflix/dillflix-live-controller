import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from controller.artwork import matchup_thumbnail, playback_thumbnail
from controller.current_playback import current_playback
from controller.database import encode

URL = "/api/v1/devices/living-room/now-playing"
ART = "http://game-thumbs:3001/proxy/nfl/DetroitLions/BuffaloBills/"


def project(service, device_id="living-room"):
    with service.db.transaction() as db:
        return current_playback(service, db, device_id).model_dump()


@pytest.fixture
def playing(rig):
    client, service, settings = rig
    response = client.post(
        "/api/v1/devices/living-room/watch-plan",
        json={
            "command_id": "play",
            "expected_revision": 0,
            "action": {"type": "play_now", "content_id": "demo:lions"},
        },
    )
    assert response.status_code == 200
    service.tick()
    with service.db.transaction() as db:
        row = db.execute("SELECT snapshot FROM contents WHERE id='demo:lions'").fetchone()
        snapshot = json.loads(row["snapshot"])
        snapshot["artwork"] = {
            "matchup_logo_url": ART + "logo.png?style=1&logo=true&fallback=true",
            "cover_url": ART + "cover.png?style=6&logo=true&fallback=true",
        }
        db.execute("UPDATE contents SET snapshot=? WHERE id='demo:lions'", (encode(snapshot),))
    return client, service, settings


def test_current_playback_is_observed_not_desired_and_read_only(playing, monkeypatch):
    client, service, _ = playing
    with service.db.transaction() as db:
        device = service.db.device(db)
        device.update(desired="demo:jays", playback_state="navigating")
        device["intent_version"] += 1
        service.db.save_device(db, device)
        before = list(db.iterdump())

    def forbidden(*args, **kwargs):
        pytest.fail("Current playback reads must not poll the player or build the full catalogue")

    monkeypatch.setattr(service, "items", forbidden)
    monkeypatch.setattr(service.playback, "observe", forbidden)
    monkeypatch.setattr(service, "tick", forbidden)
    data = project(service)
    assert data["state"] == "playing"
    assert data["playback_state"] == "navigating"
    assert data["simulated"] is True
    assert data["event"]["content_id"] == "demo:lions"
    assert data["event"]["title"] == "Lions at Bills"
    assert data["event"]["thumbnail_url"] == ART + "thumb.png?style=6&logo=true&fallback=true"
    assert data["event"]["league"] == "nfl"
    assert data["observed_at"] and data["valid_until"]
    response = client.get(URL)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == data
    with service.db.transaction() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize(
    "condition",
    [
        "expired",
        "age_cap",
        "unverified",
        "replay",
        "wrong_request",
        "manual",
        "handoff",
        "completed",
    ],
)
def test_no_active_event_without_current_live_evidence(playing, condition):
    client, service, _ = playing
    with service.db.transaction() as db:
        device = service.db.device(db)
        observed = device["observed"]
        if condition == "expired":
            observed["valid_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        elif condition == "age_cap":
            observed["observed_at"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
            observed["valid_until"] = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        elif condition == "unverified":
            observed["verified"] = False
        elif condition == "replay":
            observed["presentation"] = "replay"
        elif condition == "wrong_request":
            observed["request_id"] = "missing"
        elif condition == "manual":
            device["manual_control"] = {"id": "owner"}
        elif condition == "handoff":
            device["input_handoff"] = {"intent": device["intent_version"]}
        elif condition == "completed":
            device["manual_completions"]["demo:lions"] = datetime.now(UTC).isoformat()
        service.db.save_device(db, device)
    data = project(service)
    assert data["state"] == "unverified"
    assert data["event"] is None
    assert data["valid_until"] is None
    assert client.get(URL).json() == data


def test_idle_pending_and_unknown_device(rig):
    client, service, _ = rig
    assert project(service)["state"] == "idle"
    with service.db.transaction() as db:
        device = service.db.device(db)
        device.update(desired="demo:lions", playback_state="navigating")
        service.db.save_device(db, device)
    assert project(service)["event"] is None
    with pytest.raises(KeyError):
        project(service, "missing")
    assert client.get("/api/v1/devices/missing/now-playing").status_code == 404


def test_retained_event_survives_missing_feed_unknown_status_and_paused_automation(playing):
    client, service, _ = playing
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["automation"] = "paused"
        service.db.save_device(db, device)
        snapshot = json.loads(db.execute("SELECT snapshot FROM contents WHERE id='demo:lions'").fetchone()[0])
        snapshot["_simulation"]["status_missing"] = True
        snapshot["expected_end_time"] = "2000-01-01T00:00:00Z"
        db.execute("UPDATE contents SET active=0,snapshot=? WHERE id='demo:lions'", (encode(snapshot),))
        db.execute("DELETE FROM content_status WHERE content_id='demo:lions'")
    assert project(service)["event"]["content_id"] == "demo:lions"


def test_broadcast_without_matchup_has_null_thumbnail(rig):
    client, service, _ = rig
    service.tick()
    data = project(service)
    assert data["state"] == "playing"
    assert data["event"]["title"] == "NFL RedZone"
    assert data["event"]["thumbnail_url"] is None


def test_prime_retention_window_caps_reported_expiry(playing):
    client, service, settings = playing
    service.settings = replace(settings, executor=replace(settings.executor, mode="prime-player"))
    observed_at = datetime.now(UTC) - timedelta(minutes=2)
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["observed"].update(
            observed_at=observed_at.isoformat(),
            valid_until=(observed_at + timedelta(minutes=10)).isoformat(),
            simulated=False,
        )
        service.db.save_device(db, device)
    data = project(service)
    assert data["state"] == "playing"
    assert data["simulated"] is False
    assert data["valid_until"] == (observed_at + timedelta(minutes=5)).isoformat()


def test_completed_lifecycle_is_not_reported_before_coordinator_tick(playing):
    client, service, _ = playing
    with service.db.transaction() as db:
        snapshot = json.loads(db.execute("SELECT snapshot FROM contents WHERE id='demo:lions'").fetchone()[0])
        snapshot["_simulation"]["override"] = "ended"
        db.execute("UPDATE contents SET snapshot=? WHERE id='demo:lions'", (encode(snapshot),))
        db.execute("DELETE FROM content_status WHERE content_id='demo:lions'")
    assert project(service)["event"] is None


def test_observation_from_another_device_cannot_be_reported(playing):
    client, service, _ = playing
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["observed"]["device_id"] = "another-tv"
        service.db.save_device(db, device)
    assert project(service)["state"] == "unverified"


@pytest.mark.parametrize(
    ("artwork", "expected"),
    [
        ({}, None),
        ({"cover_url": "https://provider.example/cover.png"}, None),
        ({"matchup_logo_url": "/nfl/a/b/logo.png"}, None),
        ({"matchup_logo_url": "http://[invalid"}, None),
        ({"matchup_logo_url": ART + "logo?style=1"}, ART + "thumb.png?style=1"),
        (
            {"matchup_logo_url": ART + "logo.png", "cover_url": "https://provider.example/cover.png"},
            ART + "thumb.png",
        ),
        (
            {"matchup_logo_url": "https://thumbs.example/tennis/A%20B+C/D/logo.png?fallback=true"},
            "https://thumbs.example/tennis/A%20B+C/D/thumb.png?fallback=true",
        ),
    ],
)
def test_thumbnail_preserves_upstream_identity_and_never_rewrites_provider_art(artwork, expected):
    assert matchup_thumbnail(artwork) == expected


def test_diagnostic_endpoint_is_documented_and_reports_idle(rig):
    client, _, _ = rig
    schema = client.get("/openapi.json").json()
    response = schema["paths"]["/api/v1/devices/{device_id}/now-playing"]["get"]["responses"]["200"]
    assert response["content"]["application/json"]["schema"]["$ref"].endswith("/NowPlaying")
    assert client.get(URL).json()["state"] == "idle"


@pytest.mark.parametrize(
    "cover",
    [
        None,
        "",
        42,
        {},
        "file:///tmp/a.png",
        "/tennis/a.jpg",
        "http://[invalid",
        "https://a:bad/a.jpg",
        "https://user:pass@example.com/a.jpg",
        "https://example.com/a\nb.jpg",
    ],
)
def test_provider_cover_rejects_invalid_or_credentialed_urls(cover):
    assert playback_thumbnail({"cover_url": cover}) is None


def test_provider_cover_is_preserved_and_matchup_keeps_precedence():
    provider = "https://image.discovery.indazn.com/ca/v2/ca/image?id=day_7&width=666&height=374&format=jpg"
    assert playback_thumbnail({"cover_url": provider, "matchup_logo_url": None}) == provider
    assert (
        playback_thumbnail({"cover_url": provider, "matchup_logo_url": ART + "logo.png?style=6"})
        == ART + "thumb.png?style=6"
    )
    assert playback_thumbnail({"cover_url": provider, "matchup_logo_url": "http://[invalid"}) == provider
    assert (
        playback_thumbnail({"cover_url": {}, "matchup_logo_url": ART + "logo.png?style=6"})
        == ART + "thumb.png?style=6"
    )
