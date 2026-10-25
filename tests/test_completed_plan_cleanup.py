import copy
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_controller import command, overview

from controller.content_status import TeamarrContentStatusAdapter
from controller.database import encode
from controller.service import Controller
from controller.teamarr import TeamarrClient


@pytest.mark.parametrize("mode", ["paused", "offline", "manual"])
async def test_cleanup_is_revisioned_idempotent_and_preserves_unfinished_entries(rig, mode):
    _, service, _ = rig
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["plan"] = [service.entry(cid) for cid in ("demo:lions", "demo:golf", "missing")]
        device["manual_completions"]["demo:lions"] = datetime.now(UTC).isoformat()
        if mode == "paused":
            device["automation"] = "paused"
        elif mode == "offline":
            device["executor_health"] = {"state": "offline"}
        else:
            device["manual_control"] = {"owner": "user"}
        before = copy.deepcopy(device)
        service.db.save_device(db, device)
    service.stage_playback()
    service.stage_playback()
    with service.db.transaction() as db:
        after = service.db.device(db)
        assert after["plan"] == before["plan"][1:]
        assert after["revision"] == before["revision"] + 1
        assert after["intent_version"] == before["intent_version"]
        assert after["manual_completions"] == before["manual_completions"]
        assert (
            db.execute(
                "SELECT COUNT(*) FROM activity WHERE message='Completed events removed from watch plan'"
            ).fetchone()[0]
            == 1
        )


async def test_startup_removes_preexisting_confirmed_entries(rig):
    _, service, settings = rig
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["plan"] = [service.entry("demo:lions"), service.entry("demo:golf")]
        device["manual_completions"]["demo:lions"] = datetime.now(UTC).isoformat()
        service.db.save_device(db, device)
    await service.stop()
    restarted = Controller(settings)
    try:
        assert restarted.overview()["device"]["plan"] == device["plan"][1:]
    finally:
        await restarted.stop()


def test_cleanup_only_uses_the_completion_record_of_each_saved_device(rig):
    _, service, _ = rig
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["plan"] = [service.entry("demo:lions"), service.entry("demo:golf")]
        other = copy.deepcopy(device)
        other["id"] = "other-device"
        device["manual_completions"]["demo:lions"] = datetime.now(UTC).isoformat()
        service.db.save_device(db, device)
        service.db.save_device(db, other)
        service.prune_completed_plans(db)
        assert service.db.device(db)["plan"] == device["plan"][1:]
        assert service.db.device(db, "other-device")["plan"] == other["plan"]


@pytest.mark.parametrize("state", ["final", "cancelled"])
async def test_historical_completion_removes_old_plan_entry_without_playback(rig, state):
    _, service, _ = rig
    with service.db.transaction() as db:
        row = db.execute("SELECT snapshot FROM contents WHERE id='demo:lions'").fetchone()
        snapshot = json.loads(row[0])
        snapshot.update(status="live", source="games", competition="nfl")
        db.execute(
            "UPDATE contents SET snapshot=?,active=0,seen_at=? WHERE id=?",
            (encode(snapshot), (datetime.now(UTC) - timedelta(days=3)).isoformat(), snapshot["id"]),
        )
        device = service.db.device(db)
        device["plan"] = [service.entry(snapshot["id"])]
        device["automation"] = "paused"
        service.db.save_device(db, device)
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(200, json={"schema_version": 1, "items": [{**snapshot, "status": state}]})

    service.status_adapter = TeamarrContentStatusAdapter(
        TeamarrClient("https://teamarr.invalid", transport=httpx.MockTransport(respond)), 120
    )
    await service.refresh_status(force=True)
    data = service.overview()
    assert data["device"]["plan"] == []
    assert data["device"]["automation"] == "paused"
    assert data["device"]["intent_version"] == device["intent_version"]
    assert data["device"]["revision"] == device["revision"] + 1
    assert len(calls) == 1
    with service.db.transaction() as db:
        assert not db.execute("SELECT * FROM jobs").fetchall()
        assert (
            json.loads(
                db.execute("SELECT snapshot FROM contents WHERE id=?", (snapshot["id"],)).fetchone()[0]
            )
            == snapshot
        )


def test_cleanup_rejects_stale_plan_edits_and_undo_does_not_resurrect_completed_entry(rig):
    client, service, _ = rig
    command(client, {"type": "add", "content_id": "demo:lions"})
    before = overview(client)
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["manual_completions"]["demo:lions"] = datetime.now(UTC).isoformat()
        service.db.save_device(db, device)
    service.stage_playback()
    response = client.post(
        "/api/v1/devices/living-room/watch-plan",
        json={
            "command_id": "stale-after-cleanup",
            "expected_revision": before["device"]["revision"],
            "action": {"type": "add", "content_id": "demo:golf"},
        },
    )
    assert response.status_code == 409
    assert overview(client)["undo"] is None
