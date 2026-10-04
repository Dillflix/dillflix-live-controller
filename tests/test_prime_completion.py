"""Only fresh Ended evidence bound to the owning verified live attempt completes an event."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from playback_fixtures import payload
from test_prime_workflow import cleanup, launch, rig, row

from controller.database import encode
from controller.executor.models import ExecutorError, PlaybackReport
from controller.service import Controller


def seed(controller):
    snapshot = payload()["content_snapshot"]
    snapshot["status"] = "live"
    fallback = copy.deepcopy(snapshot)
    fallback.update(id="fallback-game", title="Other live game")
    with controller.db.transaction() as db:
        controller.replace_catalog(db, [snapshot, fallback], "teamarr")
        device = controller.db.device(db)
        device["plan"] = [controller.entry(snapshot["id"]), controller.entry(fallback["id"])]
        controller.db.save_device(db, device)
    return snapshot


async def ended_status(workflow, token):
    workflow.player.state = "ended"
    return await workflow.player.status(workflow.store.report(token)["prime_player"]["attempt_id"], 60)


async def test_bound_ended_persists_immediately_and_planner_moves_on(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        seed(controller)
        token = await launch(workflow)
        before = workflow.store.report(token)
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            device.update(desired="fixture-game", observed=before["observation"], playback_state="verified")
            controller.db.save_device(db, device)
        status = await ended_status(workflow, token)
        assert workflow.record_status(token, status) is False
        report = workflow.store.report(token)
        PlaybackReport.model_validate(report)
        assert report["operation"]["state"] == "completed"
        assert report["operation"]["error"] is None
        assert not report["observation"]["verified"]
        assert report["content_status"]["effective_state"] == "ended"
        assert report["content_status"]["observation"]["source"] == "prime_player"
        controller.stage_playback()
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            assert device["desired"] == "fallback-game"
            assert [p["content_id"] for p in device["plan"]] == ["fixture-game", "fallback-game"]
            assert "fixture-game" not in device["failures"]
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM activity WHERE message='Event completed by Prime'"
                ).fetchone()[0]
                == 1
            )
        # Replacing and cleaning up the old attempt preserves its completion.
        workflow.store.submit(payload(intent=10))
        assert row(workflow, token)["cancel_requested"] == 1
        await workflow.cancel_one(token)
        retained = workflow.store.report(token)
        assert retained["operation"]["state"] == "completed"
        assert retained["content_status"]["effective_state"] == "ended"
    finally:
        await cleanup(controller)


@pytest.mark.parametrize(
    "change",
    [
        {"state": "paused"},
        {"state": "stopped"},
        {"state": "buffering"},
        {"state": "error"},
        {"state": "unknown"},
        {"state": "not_current"},
        {"observation_age_seconds": 30},
        {"observation_age_seconds": None},
        {"matches_attempt": False},
        {"current_content_id": "other"},
        {"is_playing": True},
        {"is_playing": None},
        {"error": "observation incomplete"},
    ],
)
async def test_unconfirmed_or_nonended_status_does_not_complete(tmp_path, change):
    controller, workflow = rig(tmp_path)
    try:
        token = await launch(workflow)
        status = {**await ended_status(workflow, token), **change}
        workflow.record_status(token, status)
        assert workflow.store.report(token)["content_status"]["effective_state"] != "ended"
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("field", ["session_id", "attempt_id", "requested_id", "resolved_id"])
async def test_wrong_identity_cannot_complete(tmp_path, field):
    controller, workflow = rig(tmp_path)
    try:
        token = await launch(workflow)
        status = {**await ended_status(workflow, token), field: "other"}
        with pytest.raises(ExecutorError):
            workflow.record_status(token, status)
        assert workflow.store.report(token)["content_status"]["effective_state"] != "ended"
    finally:
        await cleanup(controller)


@pytest.mark.parametrize(
    "change",
    ["intent", "owner", "manual", "paused", "cancelled", "unverified_launch", "replay", "navigating"],
)
async def test_late_ended_cannot_complete_after_authority_changes(tmp_path, change):
    controller, workflow = rig(tmp_path)
    try:
        token = await launch(workflow)
        status = await ended_status(workflow, token)
        report = workflow.store.report(token)
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            if change == "intent":
                device["intent_version"] += 1
            elif change == "manual":
                device["manual_control"] = {"session_id": "manual"}
            elif change == "paused":
                device["automation"] = "paused"
            elif change == "owner":
                db.execute("UPDATE executor_devices SET current_token=NULL")
            elif change == "cancelled":
                db.execute("UPDATE executor_jobs SET cancel_requested=1 WHERE token=?", (token,))
            elif change == "unverified_launch":
                report["prime_player"]["launch_outcome"]["state"] = "unknown"
                workflow.store.write(db, token, report)
            elif change == "replay":
                report["prime_player"]["launch_outcome"]["evidence"]["resolution"]["playbackClass"] = "replay"
                workflow.store.write(db, token, report)
            else:
                workflow.store.write(db, token, report, state="navigating")
            controller.db.save_device(db, device)
        try:
            workflow.record_status(token, status)
        except ExecutorError:
            pass
        assert workflow.store.report(token)["content_status"]["effective_state"] != "ended"
    finally:
        await cleanup(controller)


async def test_completion_survives_restart_expiry_and_late_feed_response(tmp_path):
    controller, workflow = rig(tmp_path)
    restarted = None
    try:
        snapshot = seed(controller)
        await controller.refresh_status(force=True)
        with controller.db.transaction() as db:
            check = dict(
                db.execute("SELECT * FROM content_status WHERE content_id='fixture-game'").fetchone()
            )
        old_request = {"schema_version": 1, "request_id": check["request_id"], "content_id": "fixture-game"}
        old_report = {**old_request, "observation": json.loads(check["observation"])}
        token = await launch(workflow)
        workflow.record_status(token, await ended_status(workflow, token))
        assert not controller.accept_status_report(old_request, old_report)
        # Even a later matching Playing observation must not reopen a completed event.
        workflow.player.state = "playing"
        workflow.record_status(token, await workflow.player.status(workflow.player.attempt_id, 60))
        assert row(workflow, token)["state"] == "completed"
        with controller.db.transaction() as db:
            ended = json.loads(
                db.execute(
                    "SELECT observation FROM content_status WHERE content_id='fixture-game'"
                ).fetchone()[0]
            )
            ended["observed_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
            ended["valid_until"] = (datetime.now(UTC) - timedelta(minutes=58)).isoformat()
            db.execute(
                "UPDATE content_status SET observation=? WHERE content_id='fixture-game'", (encode(ended),)
            )
            # Teamarr still reports live in a fresh catalog response.
            controller.replace_catalog(db, [snapshot], "teamarr")
        await controller.stop()
        restarted = Controller(controller.settings)
        restarted.executor.recover()
        await restarted.refresh_status(force=True)
        with restarted.db.transaction() as db:
            item = next(i for i in restarted.items(db) if i["content_id"] == "fixture-game")
            stored = json.loads(
                db.execute(
                    "SELECT observation FROM content_status WHERE content_id='fixture-game'"
                ).fetchone()[0]
            )
        assert item["lifecycle"]["state"] == "ended"
        assert item["lifecycle"]["source"] == "prime_player"
        assert not item["playable"]
        assert stored == ended
    finally:
        if restarted:
            await cleanup(restarted)
        await cleanup(controller)
