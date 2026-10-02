import copy
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_content_status import report
from test_controller import command, overview

from controller.api import create_app
from controller.config import Settings
from controller.database import Database, encode
from controller.fixtures import fixtures
from controller.service import Controller

PATH = "/api/v1/devices/living-room/completions"


def complete(client, content_id="demo:lions", **changes):
    body = {
        "command_id": str(uuid4()),
        "expected_revision": overview(client)["device"]["revision"],
        "content_id": content_id,
        **changes,
    }
    return client.post(PATH, json=body)


def event(service, content_id="demo:lions", device_id="living-room"):
    return next(e for e in service.overview(device_id)["events"] if e["content_id"] == content_id)


async def test_complete_paused_out_of_feed_playback_survives_refresh_and_restart(rig):
    c, s, settings = rig
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    d = overview(c)["device"]
    c.post(
        "/api/v1/devices/living-room/automation",
        json={
            "command_id": str(uuid4()),
            "expected_revision": d["revision"],
            "mode": "paused",
        },
    )
    with s.db.transaction() as db:
        db.execute("UPDATE contents SET active=0 WHERE id='demo:lions'")
        original = dict(db.execute("SELECT * FROM contents WHERE id='demo:lions'").fetchone())
    before = overview(c)["device"]
    args = {"command_id": str(uuid4()), "expected_revision": before["revision"]}
    result = complete(c, **args)
    assert result.status_code == 200
    assert complete(c, **args).json() == result.json()
    assert complete(c, expected_revision=before["revision"]).status_code == 409
    assert event(s)["lifecycle"]["source"] == "manual_completion"
    assert event(s)["lifecycle"]["state"] == "ended"
    assert event(s)["lifecycle"]["stale"] is False
    assert event(s)["playable"] is False
    await s.refresh_status(force=True)
    for _ in range(3):
        s.tick()  # The simulator still observes the old event; it must not be readopted.
    after = overview(c)["device"]
    assert after["desired"] is after["observed"] is None
    assert after["automation"] == "paused"
    assert after["plan"] == before["plan"]
    for key in s.CONFIG_FIELDS:
        assert after[key] == before[key]
    with s.db.transaction() as db:
        assert dict(db.execute("SELECT * FROM contents WHERE id='demo:lions'").fetchone()) == original
        s.replace_catalog(db, fixtures()[0], "demo")
    restarted = Controller(settings)
    try:
        assert event(restarted)["lifecycle"]["state"] == "ended"
        assert restarted.overview()["status_health"]["state"] == "ok"
        assert restarted.overview()["device"]["manual_completions"] == after["manual_completions"]
    finally:
        await restarted.stop()


def test_complete_active_event_selects_fallback_and_undo_restores_eligibility(rig):
    c, s, _ = rig
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    plan = overview(c)["device"]["plan"]
    assert complete(c).status_code == 200
    s.tick()
    data = overview(c)
    assert data["device"]["observed"]["content_id"] == "demo:redzone"
    assert data["device"]["plan"] == plan
    response = c.post(
        "/api/v1/devices/living-room/undo",
        json={
            "command_id": str(uuid4()),
            "expected_revision": data["device"]["revision"],
            "history_id": data["undo"]["id"],
        },
    )
    assert response.status_code == 200
    assert event(s)["playable"]
    s.tick()
    assert overview(c)["device"]["observed"]["content_id"] == "demo:lions"


def test_completion_cancels_matching_navigation_and_rejects_late_result(rig):
    c, s, _ = rig
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.stage_playback()
    job = c.get("/api/v1/devices/living-room/jobs").json()["items"][0]
    old_report = s.playback.submit(job["payload"])
    assert complete(c).status_code == 200
    assert not s.receive_playback_report(job["id"], old_report)
    s.tick()
    assert overview(c)["device"]["observed"]["content_id"] != "demo:lions"
    with s.db.transaction() as db:
        row = db.execute("SELECT state,cancel_sent FROM jobs WHERE id=?", (job["id"],)).fetchone()
        assert tuple(row) == ("superseded", 1)


def test_completion_of_old_observation_preserves_new_pending_target(rig):
    c, s, _ = rig
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    command(c, {"type": "play_now", "content_id": "demo:golf"})
    s.stage_playback()
    before = overview(c)["device"]
    assert complete(c).status_code == 200
    after = overview(c)["device"]
    assert after["intent_version"] == before["intent_version"]
    assert after["desired"] == "demo:golf"
    assert after["playback_state"] == "navigating"
    s.tick()
    assert overview(c)["device"]["observed"]["content_id"] == "demo:golf"


async def test_inflight_status_cannot_revive_completed_event_and_completion_is_device_scoped(rig):
    c, s, _ = rig
    command(c, {"type": "add", "content_id": "demo:lions"})
    with s.db.transaction() as db:
        other = copy.deepcopy(s.db.device(db))
        other["id"] = "other-room"
        s.db.save_device(db, other)

    async def lookup(request):
        if request["content_id"] == "demo:lions":
            assert complete(c).status_code == 200
        return report(request)

    s.status_adapter.lookup = lookup
    await s.refresh_status(force=True)
    assert event(s)["lifecycle"]["state"] == "ended"
    assert event(s, device_id="other-room")["lifecycle"]["state"] == "live"
    assert event(s, device_id="other-room")["playable"]


def test_invalid_completion_does_not_mutate_device(rig):
    c, _, _ = rig
    before = overview(c)["device"]
    assert complete(c, "absent").status_code == 404
    assert complete(c, "").status_code == 422
    assert overview(c)["device"] == before


async def test_stale_teamarr_target_can_be_completed_without_touching_feed_health(tmp_path):
    settings = Settings(database=str(tmp_path / "teamarr.db"), mode="teamarr", teamarr_url="http://unused")
    app = create_app(settings, start_workers=False)
    s = app.state.controller
    snapshot = {**fixtures()[0][1], "status": "live"}
    with TestClient(app) as client:
        with s.db.transaction() as db:
            s.replace_catalog(db, [snapshot], "teamarr")
            db.execute("UPDATE contents SET active=0,seen_at='2000-01-01T00:00:00Z'")
            device = s.db.device(db)
            device["plan"] = [s.entry(snapshot["id"])]
            device["desired"] = snapshot["id"]
            device["automation"] = "paused"
            s.db.save_device(db, device)
        await s.refresh_status(force=True)
        before = overview(client)
        assert before["health"]["state"] == "ok"
        assert before["status_health"]["state"] == "degraded"
        assert event(s)["lifecycle"]["state"] == "unknown"
        assert complete(client).status_code == 200
        await s.refresh_status(force=True)
        after = overview(client)
        assert after["health"] == before["health"]
        assert after["status_health"]["state"] == "ok"
        assert after["device"]["plan"] == before["device"]["plan"]
        assert after["device"]["desired"] is None
        assert event(s)["lifecycle"]["state"] == "ended"
        with s.db.transaction() as db:
            assert json.loads(db.execute("SELECT snapshot FROM contents").fetchone()[0]) == snapshot


async def test_schema_five_upgrade_preserves_device_and_fences_older_release(tmp_path):
    settings = Settings(database=str(tmp_path / "upgrade.db"))
    s = Controller(settings)
    await s.stop()
    with sqlite3.connect(settings.database) as db:
        row = db.execute("SELECT payload FROM devices").fetchone()[0]
        payload = json.loads(row)
        payload.pop("manual_completions", None)
        db.execute("UPDATE devices SET payload=?", (encode(payload),))
        db.execute("PRAGMA user_version=5")
    upgraded = Controller(settings)
    try:
        assert upgraded.overview()["device"]["manual_completions"] == {}
        with upgraded.db.transaction() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == Database.SCHEMA_VERSION
            stored = json.loads(db.execute("SELECT payload FROM devices").fetchone()[0])
            assert stored == payload

        class OlderDatabase(Database):
            SCHEMA_VERSION = 5

        with pytest.raises(ValueError, match="newer controller"):
            OlderDatabase(settings.database)
    finally:
        await upgraded.stop()


async def test_schema_six_hotfix_upgrade_preserves_completion_and_adds_executor_tables(tmp_path):
    settings = Settings(database=str(tmp_path / "hotfix.db"))
    s = Controller(settings)
    with s.db.transaction() as db:
        device = s.db.device(db)
        device["manual_completions"] = {"demo:lions": "2026-10-02T21:47:19+00:00"}
        s.db.save_device(db, device)
        for table in ("executor_jobs", "executor_devices", "executor_actions"):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version=6")
    await s.stop()
    upgraded = Controller(settings)
    try:
        assert event(upgraded)["lifecycle"]["source"] == "manual_completion"
        with upgraded.db.transaction() as db:
            assert upgraded.db.device(db)["manual_completions"] == device["manual_completions"]
            assert db.execute("PRAGMA user_version").fetchone()[0] == Database.SCHEMA_VERSION
            for table in ("executor_jobs", "executor_devices", "executor_actions"):
                assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        await upgraded.stop()
