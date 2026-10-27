import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from controller.api import create_app
from controller.config import Settings
from controller.database import encode
from controller.display import MAX_EVENTS

URL = "/api/v1/devices/living-room/display"
ADMIN = {"x-dillflix-proxy-key": "proxy-secret", "x-dillflix-user": "owner", "x-dillflix-role": "admin"}


@pytest.fixture
def playing(rig):
    client, service, _ = rig
    response = client.post("/api/v1/devices/living-room/watch-plan", json={
        "command_id": "display-play", "expected_revision": 0,
        "action": {"type": "play_now", "content_id": "demo:lions"},
    })
    assert response.status_code == 200
    service.tick()
    with service.db.transaction() as db:
        snapshot = json.loads(db.execute("SELECT snapshot FROM contents WHERE id='demo:lions'").fetchone()[0])
        snapshot["event"].update(away_score=0, home_score=None, status_detail="2nd 08:42")
        snapshot["artwork"] = {
            "league_logo_url": "https://art.example/nfl/logo.png",
            "away_team_logo_url": "https://art.example/nfl/DetroitLions/logo.png",
            "home_team_logo_url": "https://art.example/nfl/BuffaloBills/logo.png",
            "cover_url": "https://art.example/unused.png",
        }
        snapshot["debug_secret"] = "must-not-leak"
        db.execute("UPDATE contents SET snapshot=? WHERE id='demo:lions'", (encode(snapshot),))
    return rig


def entitlements(monkeypatch, service, statuses):
    def views(db, device_id, items):
        for item in items:
            status = statuses.get(item["content_id"])
            item["entitlement"] = {"status": status} if status else None
        return items
    monkeypatch.setattr(service, "entitlement_views", views)


def test_display_reads_one_snapshot_without_writes_or_external_calls(playing, monkeypatch):
    client, service, _ = playing
    with service.db.snapshot() as db:
        before = list(db.iterdump())
    statements = []
    snapshots = 0
    snapshot = service.db.snapshot

    @contextmanager
    def tracked():
        nonlocal snapshots
        snapshots += 1
        with snapshot() as db:
            assert db.execute("PRAGMA query_only").fetchone()[0] == 1
            db.set_trace_callback(statements.append)
            yield db

    def forbidden(*args, **kwargs):
        pytest.fail("Display read performed a write or external operation")

    monkeypatch.setattr(service.db, "snapshot", tracked)
    monkeypatch.setattr(service.db, "transaction", forbidden)
    monkeypatch.setattr(service, "tick", forbidden)
    monkeypatch.setattr(service.playback, "observe", forbidden)
    monkeypatch.setattr(service.client, "fetch_snapshot", forbidden)
    response = client.get(URL)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert snapshots == 1
    assert not any(query.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "BEGIN IMMEDIATE"))
                   for query in statements)
    data = response.json()
    assert set(data) == {"schema_version", "device_id", "device_name", "streams", "generated_at", "timezone", "playback", "events", "schedule"}
    assert data["schema_version"] == 1
    assert datetime.now(UTC) - datetime.fromisoformat(data["generated_at"]) < timedelta(seconds=5)
    assert data["playback"]["state"] == data["playback"]["status"] == "playing"
    event = data["playback"]["event"]
    assert event["content_id"] == "demo:lions"
    assert event["scores"] == [0, None]
    assert [team["abbreviation"] for team in event["teams"]] == ["DET", "BUF"]
    assert event["status_detail"] == "2nd 08:42"
    assert set(event["artwork"]) == {"league_logo_url", "away_team_logo_url", "home_team_logo_url"}
    assert "must-not-leak" not in response.text
    assert "viewing_options" not in response.text
    assert "request_id" not in response.text
    with snapshot() as db:
        assert list(db.iterdump()) == before


def test_current_last_and_target_remain_distinct(playing):
    client, service, _ = playing
    with service.db.transaction() as db:
        device = service.db.device(db)
        device.update(desired="demo:jays", playback_state="navigating")
        service.db.save_device(db, device)
    first = client.get(URL).json()["playback"]
    assert first["state"] == "playing"
    assert first["status"] == "starting"
    assert first["event"]["content_id"] == "demo:lions"
    assert first["target_event"]["content_id"] == "demo:jays"
    assert first["last_event"] is None
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["observed"]["valid_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        service.db.save_device(db, device)
    second = client.get(URL).json()["playback"]
    assert second["state"] == "unverified"
    assert second["event"] is None
    assert second["last_event"]["content_id"] == "demo:lions"
    assert second["target_event"]["content_id"] == "demo:jays"
    assert second["valid_until"] is None
    assert second["observed_at"] == first["observed_at"]


@pytest.mark.parametrize("condition,status,state", [
    ("expired", "rechecking", "unverified"),
    ("offline", "unavailable", "unverified"),
    ("offline_fresh", "playing", "playing"),
    ("manual", "manual", "unverified"),
    ("handoff", "manual", "unverified"),
    ("paused", "paused", "playing"),
    ("wrong_device", "rechecking", "unverified"),
    ("completed", "rechecking", "unverified"),
])
def test_state_truth_and_safe_status_labels(playing, condition, status, state):
    client, service, _ = playing
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["reason"] = "password=must-not-leak"
        if condition in {"expired", "offline"}:
            device["observed"]["valid_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        if condition in {"offline", "offline_fresh"}:
            device["executor_health"] = {"state": "offline", "error": "password=must-not-leak"}
        elif condition == "manual":
            device["manual_control"] = {"session_id": "must-not-leak"}
        elif condition == "handoff":
            device["input_handoff"] = {"error": "password=must-not-leak"}
        elif condition == "paused":
            device["automation"] = "paused"
        elif condition == "wrong_device":
            device["observed"]["device_id"] = "different-tv"
        elif condition == "completed":
            device["manual_completions"]["demo:lions"] = datetime.now(UTC).isoformat()
        service.db.save_device(db, device)
    response = client.get(URL)
    data = response.json()["playback"]
    assert data["state"] == state
    assert data["status"] == status
    assert bool(data["event"]) == (state == "playing")
    assert "must-not-leak" not in response.text


def test_schedule_filters_entitlement_activity_and_terminal_states(rig, monkeypatch):
    client, service, _ = rig
    entitlements(monkeypatch, service, {
        "demo:lions": "entitled", "demo:canadiens": "entitled", "demo:jays": "unentitled",
        "demo:chiefs": "unknown", "demo:celtics": "entitled", "demo:redzone": "entitled",
    })
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["manual_completions"]["demo:celtics"] = datetime.now(UTC).isoformat()
        service.db.save_device(db, device)
        db.execute("UPDATE contents SET active=0 WHERE id='demo:redzone'")
    events = client.get(URL).json()["events"]
    assert [event["content_id"] for event in events] == ["demo:canadiens", "demo:lions"]
    assert events[0]["planned"] is True
    assert events[1]["planned"] is False


def test_schedule_is_capped_and_stably_sorted(rig, monkeypatch):
    client, service, _ = rig
    ids = [f"display:{i:03}" for i in range(MAX_EVENTS + 10)]
    entitlements(monkeypatch, service, dict.fromkeys(ids, "entitled"))
    with service.db.transaction() as db:
        template = json.loads(db.execute("SELECT snapshot FROM contents WHERE id='demo:lions'").fetchone()[0])
        for content_id in reversed(ids):
            entry = {**deepcopy(template), "id": content_id}
            db.execute("INSERT INTO contents VALUES (?, 'demo', 1, ?, ?)",
                       (content_id, datetime.now(UTC).isoformat(), encode(entry)))
    events = client.get(URL).json()["events"]
    assert len(events) == MAX_EVENTS
    assert [event["content_id"] for event in events] == ids[:MAX_EVENTS]


def test_future_unknown_is_scheduled_without_claiming_live(rig, monkeypatch):
    client, service, _ = rig
    entitlements(monkeypatch, service, {"demo:canadiens": "entitled", "demo:lions": "entitled"})
    with service.db.transaction() as db:
        for content_id in ("demo:canadiens", "demo:lions"):
            snapshot = json.loads(db.execute("SELECT snapshot FROM contents WHERE id=?", (content_id,)).fetchone()[0])
            snapshot["_simulation"]["override"] = "unknown"
            db.execute("UPDATE contents SET snapshot=? WHERE id=?", (encode(snapshot), content_id))
    result = client.get(URL).json()
    assert [event["content_id"] for event in result["events"]] == ["demo:canadiens"]
    assert result["events"][0]["lifecycle"]["state"] == "unknown"
    assert result["playback"]["simulated"] is True
    assert result["schedule"]["state"] == "ok"


@pytest.mark.parametrize("live_count,upcoming_count,expected_live,expected_upcoming", [
    (1, 110, 1, 99), (110, 1, 99, 1), (110, 110, 50, 50),
])
def test_cap_preserves_live_and_upcoming_capacity(
    rig, monkeypatch, live_count, upcoming_count, expected_live, expected_upcoming,
):
    client, service, _ = rig
    ids = {**{f"live:{i:03}": "entitled" for i in range(live_count)},
           **{f"future:{i:03}": "entitled" for i in range(upcoming_count)}}
    entitlements(monkeypatch, service, ids)
    with service.db.transaction() as db:
        template = json.loads(db.execute("SELECT snapshot FROM contents WHERE id='demo:lions'").fetchone()[0])
        device = service.db.device(db)
        device["plan"] = []
        device["rules"] = [{"enabled": True, "league": "nfl"}]
        service.db.save_device(db, device)
        for content_id in ids:
            entry = {**deepcopy(template), "id": content_id}
            upcoming = content_id.startswith("future:")
            entry["event"]["league"] = "nfl" if upcoming else "nhl"
            entry["_simulation"]["override"] = "scheduled" if upcoming else "live"
            db.execute("INSERT INTO contents VALUES (?, 'demo', 1, ?, ?)",
                       (content_id, datetime.now(UTC).isoformat(), encode(entry)))
    events = client.get(URL).json()["events"]
    assert len(events) == MAX_EVENTS
    assert sum(event["lifecycle"]["state"] == "live" for event in events) == expected_live
    assert sum(event["lifecycle"]["state"] == "scheduled" for event in events) == expected_upcoming
    # Preserve configured priority within the retained set, independently of grouping.
    assert events[0]["content_id"] == "future:000"


def test_feed_age_does_not_renew_and_unavailable_feed_is_distinct(rig):
    client, service, _ = rig
    observed = datetime.now(UTC) - timedelta(hours=1)
    with service.db.transaction() as db:
        service.db.set_meta(db, "feed_health", {"state": "ok", "last_success": observed.isoformat()})
    first = client.get(URL).json()["schedule"]
    second = client.get(URL).json()["schedule"]
    assert first == second == {
        "state": "stale", "observed_at": observed.isoformat(),
        "valid_until": (observed + timedelta(seconds=120)).isoformat(),
    }
    with service.db.transaction() as db:
        service.db.set_meta(db, "feed_health", {"state": "starting", "error": "must-not-leak"})
    assert client.get(URL).json()["schedule"] == {"state": "unavailable", "observed_at": None, "valid_until": None}


def test_prime_playback_expiry_is_bounded_not_renewed(playing):
    client, service, settings = playing
    service.settings = replace(settings, executor=replace(settings.executor, mode="prime-player"))
    observed = datetime.now(UTC) - timedelta(minutes=2)
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["observed"].update(observed_at=observed.isoformat(),
                                  valid_until=(observed + timedelta(minutes=10)).isoformat(), simulated=False)
        service.db.save_device(db, device)
    first, second = client.get(URL).json()["playback"], client.get(URL).json()["playback"]
    assert first["state"] == "playing"
    assert first["simulated"] is False
    assert first["valid_until"] == second["valid_until"] == (observed + timedelta(minutes=5)).isoformat()


def test_broadcast_and_partial_teams_preserve_unknown_data(rig):
    client, service, _ = rig
    service.tick()
    event = client.get(URL).json()["playback"]["event"]
    assert event["title"] == "NFL RedZone"
    assert event["teams"] == []
    assert event["scores"] == [None, None]
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["desired"] = "demo:lions"
        service.db.save_device(db, device)
        snapshot = json.loads(db.execute("SELECT snapshot FROM contents WHERE id='demo:lions'").fetchone()[0])
        snapshot["event"]["away_team_details"] = None
        snapshot["event"].update(away_score=None, home_score=3)
        snapshot["event"]["home_team_details"]["logo_url"] = "file:///private/secret.png"
        db.execute("UPDATE contents SET snapshot=? WHERE id='demo:lions'", (encode(snapshot),))
    event = client.get(URL).json()["playback"]["target_event"]
    assert event["teams"][0] == {"abbreviation": None, "full_name": None, "logo_url": None}
    assert event["teams"][1]["abbreviation"] == "BUF"
    assert event["teams"][1]["logo_url"] is None
    assert event["scores"] == [None, 3]


@pytest.mark.parametrize("mode,secret,headers,expected", [
    ("proxy", "proxy-secret", {}, 401),
    ("proxy", "proxy-secret", {**ADMIN, "x-dillflix-role": "user"}, 403),
    ("proxy", "proxy-secret", {**ADMIN, "x-dillflix-proxy-key": "wrong"}, 401),
    ("proxy", "proxy-secret", ADMIN, 200),
    ("guest", "", {}, 403),
    ("guest", "proxy-secret", {}, 401),
    ("guest", "proxy-secret", ADMIN, 200),
])
def test_display_keeps_existing_admin_auth_boundary(tmp_path, mode, secret, headers, expected):
    app = create_app(Settings(database=str(tmp_path / "auth.sqlite"),
                              public_auth_mode=mode, proxy_secret=secret), start_workers=False)
    with TestClient(app) as client:
        assert client.get(URL, headers=headers).status_code == expected


def test_trusted_lan_and_unknown_device(tmp_path):
    app = create_app(Settings(database=str(tmp_path / "lan.sqlite"),
                              public_auth_mode="guest", admin_auth_mode="trusted-lan"), start_workers=False)
    with TestClient(app) as client:
        result = client.get(URL)
        assert result.status_code == 200
        assert result.json()["playback"]["status"] == "waiting"
        assert result.json()["playback"]["simulated"] is True
        assert client.get("/api/v1/devices/missing/display").status_code == 404
        assert client.post(URL).status_code in {403, 405}
        schema = client.get("/openapi.json").json()
        response = schema["paths"]["/api/v1/devices/{device_id}/display"]["get"]["responses"]["200"]
        assert response["content"]["application/json"]["schema"]["$ref"].endswith("/DisplayView")
