import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from controller.api import create_app
from controller.config import Settings
from controller.database import encode
from controller.fixtures import fixtures
from controller.planner import allowed_options, parse_time
from controller.service import Controller
from controller.teamarr import TeamarrClient


@pytest.fixture
def rig(tmp_path):
    settings = Settings(database=str(tmp_path / "controller.sqlite"), simulation_delay=0)
    app = create_app(settings, start_workers=False)
    with TestClient(app) as client:
        yield client, app.state.controller, settings


def overview(client):
    response = client.get("/api/v1/overview")
    assert response.status_code == 200, response.text
    return response.json()


def command(client, action, **extra):
    body = {
        "command_id": str(uuid4()),
        "expected_revision": overview(client)["device"]["revision"],
        "action": action,
        **extra,
    }
    return client.post("/api/v1/devices/living-room/watch-plan", json=body)


def scenario(client, name):
    result = client.post("/api/v1/simulation", json={"action": "scenario", "scenario": name})
    assert result.status_code == 200


def test_manual_overrides_auto_and_preserves_other_commitments(rig):
    c, service, _ = rig
    service.tick()
    assert overview(c)["device"]["observed"]["content_id"] == "demo:redzone"
    assert command(c, {"type": "play_now", "content_id": "demo:lions"}).status_code == 200
    service.tick()
    d = overview(c)["device"]
    assert d["observed"]["content_id"] == "demo:lions"
    assert [p["content_id"] for p in d["plan"]] == ["demo:lions", "demo:canadiens"]


def test_command_idempotency_and_conflicting_reuse(rig):
    c, _, _ = rig
    key = str(uuid4())
    action = {"type": "play_now", "content_id": "demo:lions"}
    first = command(c, action, command_id=key, expected_revision=0)
    second = command(c, action, command_id=key, expected_revision=0)
    assert first.json() == second.json()
    assert overview(c)["device"]["revision"] == 1
    assert (
        command(
            c, {"type": "add", "content_id": "demo:jays"}, command_id=key, expected_revision=0
        ).status_code
        == 409
    )


def test_two_clients_cannot_silently_overwrite_the_plan(rig):
    c, _, _ = rig
    assert command(c, {"type": "add", "content_id": "demo:jays"}, expected_revision=0).status_code == 200
    assert command(c, {"type": "add", "content_id": "demo:chiefs"}, expected_revision=0).status_code == 409
    assert len(overview(c)["device"]["plan"]) == 2


def test_conflict_preview_has_no_side_effects_and_preserves_nonoverlap(rig):
    c, _, _ = rig
    before = overview(c)["device"]
    response = c.post(
        "/api/v1/devices/living-room/watch-plan/preview",
        json={
            "command_id": str(uuid4()),
            "expected_revision": 0,
            "action": {"type": "add", "content_id": "demo:jays", "priority": "first"},
        },
    )
    assert response.status_code == 200
    assert response.json()["conflicts"]
    segments = response.json()["segments"]
    assert [s["content_id"] for s in segments][:3] == [None, "demo:canadiens", "demo:jays"]
    assert overview(c)["device"] == before


def test_reorder_rejects_duplicates_or_missing_entries(rig):
    c, _, _ = rig
    entry_id = overview(c)["device"]["plan"][0]["id"]
    for order in [[], [entry_id, entry_id], ["other"]]:
        assert command(c, {"type": "reorder", "ordered_entry_ids": order}).status_code == 422


def test_overtime_continues_past_estimated_end(rig):
    c, s, _ = rig
    scenario(c, "overtime")
    s.tick()
    data = overview(c)
    canadiens = next(e for e in data["events"] if e["content_id"] == "demo:canadiens")
    assert parse_time(data["meta"]["now"]) > parse_time(canadiens["expected_end_time"])
    assert canadiens["lifecycle"]["state"] == "live"
    assert data["device"]["observed"]["content_id"] == "demo:canadiens"


def test_delayed_manual_commitment_survives_live_fallback(rig):
    c, s, _ = rig
    scenario(c, "delayed")
    s.tick()
    d = overview(c)["device"]
    assert d["observed"]["content_id"] == "demo:redzone"
    assert d["plan"][0]["content_id"] == "demo:canadiens"


def test_playback_failure_selects_live_fallback_and_records_retry(rig):
    c, s, _ = rig
    scenario(c, "failure")
    s.tick()
    assert overview(c)["device"]["playback_state"] == "failed"
    s.tick()
    d = overview(c)["device"]
    assert d["observed"]["content_id"] == "demo:lions"
    assert d["failures"]["demo:redzone"]["retry_after"]


def test_no_live_events_wait_without_replay(rig):
    c, s, _ = rig
    scenario(c, "empty")
    s.tick()
    d = overview(c)["device"]
    assert d["playback_state"] == "waiting"
    assert d["observed"] is None


def test_pause_persists_across_restart_and_cancels_pending_job(rig):
    c, s, settings = rig
    object.__setattr__(s.settings, "simulation_delay", 100)
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    assert c.get("/api/v1/devices/living-room/jobs").json()["items"][0]["state"] == "pending"
    d = overview(c)["device"]
    response = c.post(
        "/api/v1/devices/living-room/automation",
        json={"command_id": str(uuid4()), "expected_revision": d["revision"], "mode": "paused"},
    )
    assert response.status_code == 200
    assert c.get("/api/v1/devices/living-room/jobs").json()["items"][0]["state"] == "cancelled"
    restarted = Controller(settings)
    assert restarted.overview()["device"]["automation"] == "paused"
    assert restarted.overview()["device"]["plan"][0]["content_id"] == "demo:lions"


def test_old_pending_job_is_superseded_before_completion(rig):
    c, s, _ = rig
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    # Make the first job genuinely pending rather than simulating a callback in isolation.
    object.__setattr__(s.settings, "simulation_delay", 100)
    s.tick()
    command(c, {"type": "play_now", "content_id": "demo:golf"})
    object.__setattr__(s.settings, "simulation_delay", 0)
    s.tick()
    assert overview(c)["device"]["observed"]["content_id"] == "demo:golf"
    jobs = c.get("/api/v1/devices/living-room/jobs").json()["items"]
    assert next(j for j in jobs if j["content_id"] == "demo:lions")["state"] == "superseded"


def test_worker_lease_excludes_second_coordinator(rig):
    c, first, settings = rig
    first.tick()
    second = Controller(settings)
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    second.tick()
    assert overview(c)["device"]["desired"] == "demo:redzone"
    first.tick()
    assert overview(c)["device"]["desired"] == "demo:lions"


def test_original_snapshot_and_every_valid_option_are_passed_through(rig):
    c, s, _ = rig
    with s.db.transaction() as db:
        row = db.execute("SELECT snapshot FROM contents WHERE id='demo:golf'").fetchone()
        snapshot = json.loads(row[0])
        snapshot["future_provider_extension"] = {"nested": [1, 2, "untouched"]}
        snapshot["preferred_option_id"] = snapshot["viewing_options"][1]["id"]
        db.execute("UPDATE contents SET snapshot=? WHERE id='demo:golf'", (encode(snapshot),))
    command(c, {"type": "play_now", "content_id": "demo:golf"})
    s.tick()
    request = c.get("/api/v1/devices/living-room/jobs").json()["items"][0]["payload"]
    assert request["content_snapshot"] == snapshot
    assert request["allowed_viewing_options"] == snapshot["viewing_options"]
    assert request["mode"] == "live"


def test_wrong_coverage_excluded_but_uncertain_end_retained():
    entry = fixtures()[0][1]
    good = {"id": "review", "decision": "review", "reasons": ["unknown_end_time"]}
    entry["viewing_options"] = [
        good,
        {"id": "replay", "presentation": "replay"},
        {"id": "redzone", "decision": "review", "reasons": ["multi_event_coverage_not_full_game"]},
    ]
    assert allowed_options(entry) == [good]


def test_team_identity_is_scoped_to_provider_and_league(rig):
    c, _, _ = rig
    teams = [t for e in overview(c)["events"] for t in e["teams"]]
    assert "demo:nhl:TOR" in [t["key"] for t in teams]
    assert "demo:mlb:TOR" in [t["key"] for t in teams]


async def test_feed_pagination_uses_cursor_only_and_restarts_expired_snapshot():
    calls = []
    entry = fixtures()[0][0]

    def handler(request):
        calls.append(dict(request.url.params))
        if len(calls) == 1:
            return httpx.Response(200, json={"schema_version": 1, "items": [entry], "next_cursor": "old"})
        if len(calls) == 2:
            return httpx.Response(410)
        return httpx.Response(200, json={"schema_version": 1, "items": [entry], "next_cursor": None})

    items, schema = await TeamarrClient(
        "http://teamarr", transport=httpx.MockTransport(handler)
    ).fetch_snapshot()
    assert calls[1] == {"cursor": "old"}
    assert "start" in calls[2]
    assert items == [entry]
    assert schema == 1


async def test_partial_failed_feed_keeps_previous_catalog_and_commitments(tmp_path):
    s = Controller(Settings(database=str(tmp_path / "t.db"), mode="teamarr", teamarr_url="http://teamarr"))
    entry = fixtures()[0][1]
    entry["status"] = "live"
    with s.db.transaction() as db:
        s.replace_catalog(db, [entry], "teamarr")
        d = s.db.device(db)
        d["plan"] = [s.entry(entry["id"])]
        s.db.save_device(db, d)

    def handler(request):
        return (
            httpx.Response(503)
            if "cursor" in request.url.params
            else httpx.Response(
                200, json={"schema_version": 1, "items": [fixtures()[0][0]], "next_cursor": "next"}
            )
        )

    s.client = TeamarrClient("http://teamarr", transport=httpx.MockTransport(handler))
    await s.refresh_catalog()
    data = s.overview()
    assert [e["content_id"] for e in data["events"]] == [entry["id"]]
    assert data["health"]["state"] == "degraded"
    assert data["device"]["plan"]


def test_real_feed_estimate_and_disappearance_do_not_complete_event(tmp_path):
    s = Controller(
        Settings(
            database=str(tmp_path / "t.db"), mode="teamarr", teamarr_url="http://teamarr", simulation_delay=0
        )
    )
    entry = fixtures()[0][1]
    entry.update(status="live", expected_end_time=(datetime.now(UTC) - timedelta(hours=5)).isoformat())
    with s.db.transaction() as db:
        s.replace_catalog(db, [entry], "teamarr")
        d = s.db.device(db)
        d["plan"] = [s.entry(entry["id"])]
        s.db.save_device(db, d)
    s.tick()
    with s.db.transaction() as db:
        s.replace_catalog(db, [], "teamarr")
    s.tick()
    data = s.overview()
    assert data["device"]["observed"]["content_id"] == entry["id"]
    assert data["device"]["plan"]


def test_invalid_timezone_and_future_play_now_rejected(rig):
    c, _, _ = rig
    assert command(c, {"type": "play_now", "content_id": "demo:jays"}).status_code == 422
    d = overview(c)["device"]
    response = c.put(
        "/api/v1/devices/living-room/rules",
        json={
            "command_id": str(uuid4()),
            "expected_revision": d["revision"],
            "rules": d["rules"],
            "team_ranks": {},
            "preferences": {**d["preferences"], "timezone": "Not/AZone"},
        },
    )
    assert response.status_code == 422


def test_editing_future_plan_does_not_interrupt_current_unknown_status(rig):
    c, s, _ = rig
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    with s.db.transaction() as db:
        row = db.execute("SELECT snapshot FROM contents WHERE id='demo:lions'").fetchone()
        entry = json.loads(row[0])
        entry["_simulation"]["override"] = "unknown"
        db.execute("UPDATE contents SET snapshot=? WHERE id='demo:lions'", (encode(entry),))
    command(c, {"type": "add", "content_id": "demo:jays"})
    s.tick()
    assert overview(c)["device"]["observed"]["content_id"] == "demo:lions"
    assert "stale" in overview(c)["device"]["reason"]
    # An explicit, higher-priority live choice still takes effect immediately.
    command(c, {"type": "play_now", "content_id": "demo:golf"})
    s.tick()
    assert overview(c)["device"]["observed"]["content_id"] == "demo:golf"


def test_inactive_uncommitted_catalog_item_is_not_newly_selected(rig):
    c, s, _ = rig
    with s.db.transaction() as db:
        db.execute("UPDATE contents SET active=0 WHERE id='demo:redzone'")
    s.tick()
    assert overview(c)["device"]["observed"]["content_id"] == "demo:lions"


async def test_restart_recovers_pending_request_without_creating_a_duplicate(rig):
    c, first, settings = rig
    object.__setattr__(settings, "simulation_delay", 100)
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    first.tick()
    pending = c.get("/api/v1/devices/living-room/jobs").json()["items"][0]
    await first.stop()
    restarted = Controller(settings)
    with restarted.db.transaction() as db:
        db.execute("UPDATE jobs SET ready_at=0 WHERE id=?", (pending["id"],))
    restarted.tick()
    state = restarted.overview()["device"]
    assert state["observed"]["request_id"] == pending["id"]
    assert len(c.get("/api/v1/devices/living-room/jobs").json()["items"]) == 1
