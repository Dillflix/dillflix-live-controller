"""Exercise Prime monitoring together with coordinator recovery, without a device."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from playback_fixtures import payload
from test_prime_catalogue import finish_check
from test_prime_workflow import cleanup, rig, row

from controller.planner import parse_time


@pytest.fixture
async def live_prime(tmp_path):
    controller, workflow = rig(tmp_path)
    workflow.loop = asyncio.get_running_loop()
    with controller.db.transaction() as db:
        controller.replace_catalog(db, [payload()["content_snapshot"]], "teamarr")
    controller.tick()
    await finish_check(controller)
    controller.tick()
    with controller.db.transaction() as db:
        job = db.execute("SELECT * FROM jobs").fetchone()
    report = workflow.store.by_request(job["id"])
    await workflow.navigate(row(workflow, report["token"]))
    controller.tick()
    before = controller.overview()["device"]
    assert before["playback_state"] == "verified"
    try:
        yield controller, workflow, report["token"], before
    finally:
        if workflow.active and not workflow.active.done():
            workflow.active.cancel()
            await asyncio.gather(workflow.active, return_exceptions=True)
        await cleanup(controller)


def job_count(controller):
    with controller.db.transaction() as db:
        return db.execute("SELECT count(*) FROM jobs").fetchone()[0]


def make_recovery_due(controller):
    deadline = datetime.now(UTC)
    with controller.db.transaction() as db:
        device = controller.db.device(db)
        device["recovery"]["retry_after"] = deadline.isoformat()
        device["observed"].update(
            observed_at=(deadline - timedelta(minutes=5)).isoformat(),
            valid_until=deadline.isoformat(),
        )
        row = db.execute(
            "SELECT * FROM executor_jobs WHERE request_id=?", (device["observed"]["request_id"],)
        ).fetchone()
        report = json.loads(row["report"])
        report["observation"] = dict(device["observed"])
        controller.executor.store.write(db, row["token"], report)
        controller.db.save_device(db, device)
    return deadline


async def test_missing_observation_honors_five_minutes_in_refresh_path(live_prime, monkeypatch):
    controller, _, _, before = live_prime
    with controller.db.transaction() as db:
        device = controller.db.device(db)
        old = datetime.now(UTC) - timedelta(seconds=30)
        device["observed"].update(
            observed_at=old.isoformat(), valid_until=(old + timedelta(minutes=5)).isoformat()
        )
        controller.db.save_device(db, device)
    monkeypatch.setattr(controller.playback, "observe_report", lambda _: None)
    controller.tick()
    device = controller.overview()["device"]
    assert device["observed"]["verified"] is True
    assert device["recovery"] is None
    assert device["observed"]["request_id"] == before["observed"]["request_id"]
    assert job_count(controller) == 1


async def test_missing_observation_really_expires_after_five_minutes(live_prime, monkeypatch):
    controller, _, _, _ = live_prime
    with controller.db.transaction() as db:
        device = controller.db.device(db)
        old = datetime.now(UTC) - timedelta(seconds=301)
        device["observed"].update(
            observed_at=old.isoformat(), valid_until=(old + timedelta(minutes=5)).isoformat()
        )
        controller.db.save_device(db, device)
    monkeypatch.setattr(controller.playback, "observe_report", lambda _: None)
    controller.tick()
    device = controller.overview()["device"]
    assert device["observed"]["verified"] is False
    assert device["recovery"]["since"]
    with controller.db.transaction() as db:
        detail = db.execute(
            "SELECT detail FROM activity WHERE message='Playback requires revalidation' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0]
    assert "expired" in detail


@pytest.mark.parametrize("state", ["paused", "buffering", "unknown"])
async def test_temporary_state_waits_through_original_evidence_window(live_prime, state):
    controller, workflow, token, before = live_prime
    workflow.player.state = state
    await workflow.monitor(row(workflow, token))
    controller.tick()
    device = controller.overview()["device"]
    assert device["playback_state"] == "unverified"
    assert not device["observed"]["verified"]
    assert device["observed"]["observed_at"] == before["observed"]["observed_at"]
    deadline = parse_time(before["observed"]["valid_until"])
    assert parse_time(device["recovery"]["retry_after"]) >= deadline
    if state in {"paused", "buffering"}:
        assert state in device["reason"]
    await workflow.monitor(row(workflow, token))
    controller.tick()
    assert controller.overview()["device"]["recovery"]["retry_after"] == device["recovery"]["retry_after"]
    with controller.db.transaction() as db:
        detail = db.execute(
            "SELECT detail FROM activity WHERE message='Playback requires revalidation' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0]
    assert state in detail and "expired" not in detail
    assert job_count(controller) == 1


@pytest.mark.parametrize("state", ["stopped", "not_current", "error"])
async def test_explicit_failure_keeps_normal_grace_without_completing_event(live_prime, state):
    controller, workflow, token, _ = live_prime
    workflow.player.state = state
    await workflow.monitor(row(workflow, token))
    controller.tick()
    recovery = controller.overview()["device"]["recovery"]
    assert (parse_time(recovery["retry_after"]) - parse_time(recovery["since"])).total_seconds() == 60
    assert workflow.store.report(token)["content_status"]["effective_state"] != "ended"
    assert job_count(controller) == 1


@pytest.mark.parametrize("fallback", [False, True])
async def test_ended_advances_without_recovery(live_prime, fallback):
    controller, workflow, token, before = live_prime
    if fallback:
        other = payload()["content_snapshot"]
        other.update(id="other-game", title="Other game")
        with controller.db.transaction() as db:
            controller.replace_catalog(db, [payload()["content_snapshot"], other], "teamarr")
    workflow.player.state = "ended"
    await workflow.monitor(row(workflow, token))
    controller.refresh_playback_observation()
    observed = controller.overview()["device"]["observed"]
    assert not observed["verified"]
    assert observed["request_id"] == before["observed"]["request_id"]
    controller.stage_playback()
    await finish_check(controller)
    device = controller.overview()["device"]
    assert device["desired"] == ("other-game" if fallback else None)
    assert device["playback_state"] == ("navigating" if fallback else "waiting")
    assert device["recovery"] is None
    assert job_count(controller) == (2 if fallback else 1)
    assert workflow.store.report(token)["content_status"]["effective_state"] == "ended"
    with controller.db.transaction() as db:
        assert not db.execute(
            "SELECT 1 FROM activity WHERE message='Playback requires revalidation'"
        ).fetchone()


async def test_resume_keeps_original_attempt_and_viewing_timers(live_prime):
    controller, workflow, token, before = live_prime
    workflow.player.state = "paused"
    await workflow.monitor(row(workflow, token))
    controller.tick()
    workflow.player.state = "playing"
    await workflow.monitor(row(workflow, token))
    controller.tick()
    device = controller.overview()["device"]
    assert device["playback_state"] == "verified"
    assert device["recovery"]["retry_after"] is None
    assert device["observed"]["request_id"] == before["observed"]["request_id"]
    assert device["started_at"] == before["started_at"]
    assert device["last_switch_at"] == before["last_switch_at"]
    assert job_count(controller) == 1
    assert [call[0] for call in workflow.player.calls].count("play") == 1


async def test_existing_short_recovery_deadline_is_corrected_after_upgrade(live_prime):
    controller, workflow, token, before = live_prime
    workflow.player.state = "paused"
    await workflow.monitor(row(workflow, token))
    controller.tick()
    with controller.db.transaction() as db:
        device = controller.db.device(db)
        device["recovery"]["retry_after"] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        controller.db.save_device(db, device)
    controller.tick()
    device = controller.overview()["device"]
    assert parse_time(device["recovery"]["retry_after"]) == parse_time(before["observed"]["valid_until"])
    assert job_count(controller) == 1


async def test_resume_between_observation_read_and_staging_cannot_be_reopened(live_prime, monkeypatch):
    controller, workflow, token, before = live_prime
    workflow.player.state = "paused"
    await workflow.monitor(row(workflow, token))
    controller.tick()
    make_recovery_due(controller)
    stale = controller.playback.observe_report("living-room")
    workflow.player.state = "playing"
    await workflow.monitor(row(workflow, token))
    with monkeypatch.context() as patch:
        patch.setattr(controller.playback, "observe_report", lambda _: stale)
        controller.tick()
    assert job_count(controller) == 1
    controller.tick()
    device = controller.overview()["device"]
    assert device["playback_state"] == "verified"
    assert device["observed"]["request_id"] == before["observed"]["request_id"]
    assert job_count(controller) == 1


async def test_due_recovery_waits_for_inflight_resume_check(live_prime, monkeypatch):
    controller, workflow, token, before = live_prime
    workflow.player.state = "paused"
    await workflow.monitor(row(workflow, token))
    controller.tick()
    make_recovery_due(controller)
    entered, release = asyncio.Event(), asyncio.Event()
    status = workflow.player.status

    async def delayed_status(*args):
        entered.set()
        await release.wait()
        return await status(*args)

    monkeypatch.setattr(workflow.player, "status", delayed_status)
    workflow.active_token = token
    workflow.active = asyncio.create_task(workflow.monitor(row(workflow, token)))
    await entered.wait()
    controller.tick()
    await asyncio.sleep(0)
    assert not workflow.active.done(), "recovery must not cancel the status query"
    assert job_count(controller) == 1
    assert controller.overview()["device"]["intent_version"] == before["intent_version"]
    workflow.player.state = "playing"
    release.set()
    await workflow.active
    workflow.active = workflow.active_token = None
    controller.tick()
    device = controller.overview()["device"]
    assert device["playback_state"] == "verified"
    assert device["observed"]["request_id"] == before["observed"]["request_id"]
    assert job_count(controller) == 1
    assert all(call[0] not in {"stop", "cancel"} for call in workflow.player.calls)


async def test_due_recovery_queues_final_check_then_reopens_if_still_paused(live_prime):
    controller, workflow, token, _ = live_prime
    workflow.player.state = "paused"
    await workflow.monitor(row(workflow, token))
    controller.tick()
    make_recovery_due(controller)
    controller.tick()
    assert job_count(controller) == 1
    assert row(workflow, token)["next_check"] == 0
    await workflow.monitor(row(workflow, token))
    controller.stage_playback()
    await finish_check(controller)
    with controller.db.transaction() as db:
        jobs = db.execute("SELECT * FROM jobs ORDER BY intent").fetchall()
    assert len(jobs) == 2
    assert json.loads(jobs[-1]["payload"])["purpose"] == "recovery"


async def test_stale_check_cannot_authorize_recovery_after_restart(live_prime):
    controller, workflow, token, _ = live_prime
    workflow.player.state = "paused"
    await workflow.monitor(row(workflow, token))
    controller.tick()
    make_recovery_due(controller)
    with controller.db.transaction() as db:
        device = controller.db.device(db)
        device["recovery"]["retry_after"] = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        controller.db.save_device(db, device)
        job = workflow.store.get_row(db, token)
        report = json.loads(job["report"])
        report["observation_status"]["checked_at"] = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        workflow.store.write(db, token, report)
    controller.tick()
    assert job_count(controller) == 1
    assert row(workflow, token)["next_check"] == 0


async def test_route_change_bypasses_pause_recheck(live_prime):
    controller, workflow, token, before = live_prime
    workflow.player.state = "paused"
    await workflow.monitor(row(workflow, token))
    controller.tick()
    with controller.db.transaction() as db:
        snapshot = json.loads(
            db.execute("SELECT snapshot FROM contents WHERE id=?", (before["desired"],)).fetchone()[0]
        )
        snapshot["viewing_options"][0]["channel"] = "New live locator"
        controller.replace_catalog(db, [snapshot], "teamarr")
    controller.stage_playback()
    await finish_check(controller)
    with controller.db.transaction() as db:
        job = db.execute("SELECT payload FROM jobs ORDER BY intent DESC LIMIT 1").fetchone()
    assert json.loads(job["payload"])["purpose"] == "route_handoff"
    assert job_count(controller) == 2


async def test_new_manual_choice_preempts_pause_recovery(live_prime):
    controller, workflow, token, _ = live_prime
    workflow.player.state = "paused"
    await workflow.monitor(row(workflow, token))
    controller.tick()
    with controller.db.transaction() as db:
        current = payload()["content_snapshot"]
        other = {**current, "id": "another-live-event"}
        controller.replace_catalog(db, [current, other], "teamarr")
        device = controller.db.device(db)
        device["plan"] = [controller.entry(other["id"])]
        controller.db.save_device(db, device)
    controller.stage_playback()
    await finish_check(controller)
    with controller.db.transaction() as db:
        job = db.execute("SELECT content_id FROM jobs ORDER BY intent DESC LIMIT 1").fetchone()
    assert job["content_id"] == other["id"]
    assert job_count(controller) == 2


async def test_paused_automation_does_not_queue_recovery_checks(live_prime):
    controller, workflow, token, _ = live_prime
    workflow.player.state = "paused"
    await workflow.monitor(row(workflow, token))
    controller.tick()
    make_recovery_due(controller)
    with controller.db.transaction() as db:
        device = controller.db.device(db)
        device["automation"] = "paused"
        controller.db.save_device(db, device)
    scheduled = row(workflow, token)["next_check"]
    controller.stage_playback()
    await finish_check(controller)
    assert row(workflow, token)["next_check"] == scheduled
    assert job_count(controller) == 1


async def test_explicit_play_now_bypasses_final_recheck(live_prime):
    controller, workflow, token, before = live_prime
    workflow.player.state = "paused"
    await workflow.monitor(row(workflow, token))
    controller.tick()
    with controller.db.transaction() as db:
        device = controller.db.device(db)
        device["retry_playback"] = before["desired"]
        controller.db.save_device(db, device)
    controller.stage_playback()
    await finish_check(controller)
    assert job_count(controller) == 2
