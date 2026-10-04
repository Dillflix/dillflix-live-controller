"""Access/readiness observations must not become playback failures or stale launches."""

from datetime import UTC, datetime, timedelta

import pytest
from playback_fixtures import payload
from test_controller import command, overview
from test_playback import jobs, pending, successful_report
from test_prime_matching import GTI, results, tile
from test_prime_workflow import cleanup, launch, row
from test_prime_workflow import rig as workflow_rig

from controller.executor.config import ExecutorConfig
from controller.executor.models import CancelRequest, PlaybackReport
from controller.prime_player.matching import EventMatcher
from controller.service import Controller


@pytest.mark.parametrize(
    "event_state,expected",
    [
        ("UPCOMING", "waiting_for_feed"),
        ("LIVE", "ready"),
        ("UNAVAILABLE", "access_unknown"),
        ("ENDED", "no_matching_feed"),
    ],
)
async def test_catalogue_state_does_not_depend_on_action(event_state, expected):
    selected, _ = await EventMatcher(ExecutorConfig()).choose(
        payload(), results(tile(event_state=event_state, action={"target": "detail"})), "America/Vancouver"
    )
    assert selected["readiness"] == expected


async def test_accessible_alternative_wins_regardless_of_tile_order():
    for tiles in [
        (tile(entitlement_status="UNENTITLED"), tile(cid=GTI + "-open")),
        (tile(cid=GTI + "-open"), tile(entitlement_status="UNENTITLED")),
    ]:
        selected, _ = await EventMatcher(ExecutorConfig()).choose(
            payload(), results(*tiles), "America/Vancouver"
        )
        assert selected["content_id"] == GTI + "-open"
        assert selected["readiness"] == "ready"


@pytest.mark.parametrize(
    "changes",
    [
        {"entitlement_status": None},
        {"entitlement_status": "UNKNOWN"},
        {"event_state": None},
        {"event_state": "NEW_STATE"},
    ],
)
async def test_missing_or_malformed_evidence_is_unknown(changes):
    selected, _ = await EventMatcher(ExecutorConfig()).choose(
        payload(), results(tile(**changes)), "America/Vancouver"
    )
    assert selected["readiness"] == "access_unknown"


async def test_conflicting_occurrences_do_not_silently_use_first_lock_state():
    selected, _ = await EventMatcher(ExecutorConfig()).choose(
        payload(),
        results(tile(), tile(entitlement_status="UNENTITLED", handle="another")),
        "America/Vancouver",
    )
    assert selected["readiness"] == "access_unknown"


@pytest.mark.parametrize(
    "state,changes",
    [
        ("waiting_for_feed", {"event_state": "UPCOMING"}),
        ("feeds_locked", {"entitlement_status": "UNENTITLED"}),
        ("access_unknown", {"entitlement_status": "UNKNOWN"}),
    ],
)
async def test_completed_search_never_plays_and_survives_restart(tmp_path, state, changes):
    controller, workflow = workflow_rig(tmp_path)
    original = workflow.player.search

    async def search(*args):
        return {**await original(*args), **results(tile(**changes))}

    workflow.player.search = search
    try:
        token = await launch(workflow)
        report = workflow.store.report(token)
        PlaybackReport.model_validate(report)
        assert report["operation"]["state"] == state
        assert report["operation"]["error"] is None
        assert not any(c[0] == "play" for c in workflow.player.calls)
        workflow.recover()
        assert workflow.store.report(token)["operation"]["state"] == state
        assert not row(workflow, token)["cancel_requested"]
        workflow.store.request_cancel(CancelRequest(device_id="living-room", token=token))
        await workflow.cancel_one(token)
        assert row(workflow, token)["cancel_requested"] == 2
        assert not any(c[0] == "play" for c in workflow.player.calls)
    finally:
        await cleanup(controller)


async def test_new_search_can_launch_after_feed_becomes_ready(tmp_path):
    controller, workflow = workflow_rig(tmp_path)
    original = workflow.player.search

    async def upcoming(*args):
        return {**await original(*args), **results(tile(event_state="UPCOMING"))}

    workflow.player.search = upcoming
    try:
        first = await launch(workflow)
        workflow.store.request_cancel(CancelRequest(device_id="living-room", token=first))
        await workflow.cancel_one(first)
        workflow.player.search = original
        report, _ = workflow.store.submit(payload(intent=3))
        await workflow.navigate(row(workflow, report["token"]))
        assert workflow.store.report(report["token"])["operation"]["state"] == "playing_verified"
        assert [c[0] for c in workflow.player.calls].count("play") == 1
        assert [c[0] for c in workflow.player.calls].count("search") == 2
    finally:
        await cleanup(controller)


def complete_search(rig, state="waiting_for_feed"):
    _, service, _ = rig
    job = pending(rig)
    report = successful_report(service, job)
    report.update(state=state, observation=None, finished_at=datetime.now(UTC).isoformat())
    service.receive_playback_report(job["id"], report)
    return job, report


def test_waiting_holds_selection_without_failure_then_retries_with_new_deadline(rig):
    client, service, _ = rig
    job, _ = complete_search(rig)
    before = overview(client)["device"]
    for _ in range(3):
        service.stage_playback()
    device = overview(client)["device"]
    assert device["desired"] == job["content_id"]
    assert device["playback_state"] == "waiting"
    assert device["failures"] == {}
    assert len(jobs(client)) == 1
    assert device["intent_version"] == before["intent_version"]
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["prime_access"][job["content_id"]]["retry_after"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        service.db.save_device(db, device)
    service.stage_playback()
    assert len(jobs(client)) == 2
    assert jobs(client)[0]["content_id"] == job["content_id"]
    assert jobs(client)[0]["deadline_at"] > job["deadline_at"]


@pytest.mark.parametrize("state", ["feeds_locked", "no_matching_feed"])
def test_locked_feed_falls_back_without_removing_plan_or_repeated_search(rig, state):
    client, service, _ = rig
    job, _ = complete_search(rig, state)
    service.stage_playback()
    device = overview(client)["device"]
    assert device["desired"] != job["content_id"]
    assert any(p["content_id"] == job["content_id"] for p in device["plan"])
    assert not device["failures"]
    service.stage_playback()
    assert len(jobs(client)) == 2
    command(client, {"type": "play_now", "content_id": job["content_id"]})
    assert job["content_id"] not in overview(client)["device"]["prime_access"]
    service.stage_playback()
    assert overview(client)["device"]["desired"] == job["content_id"]


@pytest.mark.parametrize("state", ["feeds_locked", "no_matching_feed"])
def test_changed_options_release_lock_exclusion(rig, monkeypatch, state):
    client, service, _ = rig
    job, _ = complete_search(rig, state)
    original = service.items

    def changed(*args, **kwargs):
        items = original(*args, **kwargs)
        for item in items:
            if item["content_id"] == job["content_id"]:
                item["viewing_options"] = [
                    *item["viewing_options"],
                    {"id": "new-route", "app": "prime_video"},
                ]
        return items

    monkeypatch.setattr(service, "items", changed)
    service.stage_playback()
    assert overview(client)["device"]["desired"] == job["content_id"]
    assert len(jobs(client)) == 2


async def test_controller_restart_keeps_waiting_due_time(rig):
    client, service, settings = rig
    job, _ = complete_search(rig)
    evidence = overview(client)["device"]["prime_access"]
    await service.stop()
    restarted = Controller(settings)
    try:
        restarted.stage_playback()
        assert restarted.overview()["device"]["prime_access"] == evidence
        assert len(jobs(client)) == 1
    finally:
        await restarted.stop()


@pytest.mark.parametrize("control", ["pause", "manual", "new_intent"])
def test_late_readiness_result_cannot_override_current_control(rig, control):
    client, service, _ = rig
    job = pending(rig)
    report = successful_report(service, job)
    report.update(state="feeds_locked", observation=None, finished_at=datetime.now(UTC).isoformat())
    with service.db.transaction() as db:
        device = service.db.device(db)
        if control == "pause":
            device["automation"] = "paused"
        elif control == "manual":
            device["manual_control"] = {"session_id": "manual"}
        else:
            device["intent_version"] += 1
        service.db.save_device(db, device)
    assert not service.receive_playback_report(job["id"], report)
    with service.db.transaction() as db:
        assert not service.db.device(db).get("prime_access")


async def test_unrelated_watchable_tile_does_not_hide_locked_target():
    selected, _ = await EventMatcher(ExecutorConfig()).choose(
        payload(),
        results(tile("Jets vs Ravens", cid=GTI + "-wrong"), tile(entitlement_status="UNENTITLED")),
        "America/Vancouver",
    )
    assert selected["readiness"] == "feeds_locked"


async def test_catalogue_launch_needs_no_tile_action_or_controller_resolution(tmp_path):
    controller, workflow = workflow_rig(tmp_path)
    original = workflow.player.search

    async def search(*args):
        return {**await original(*args), **results(tile(action=None))}

    workflow.player.search = search
    try:
        token = await launch(workflow)
        assert workflow.store.report(token)["operation"]["state"] == "playing_verified"
        assert not any(c[0] == "resolve" for c in workflow.player.calls)
        assert [c[1] for c in workflow.player.calls if c[0] == "play"] == [GTI]
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("finished_before_deadline", [False, True])
def test_completed_search_deadline_uses_completion_time(rig, finished_before_deadline):
    client, service, _ = rig
    job = pending(rig)
    report = successful_report(service, job)
    now = datetime.now(UTC)
    report.update(
        state="waiting_for_feed",
        observation=None,
        finished_at=(now - timedelta(seconds=2 if finished_before_deadline else 0)).isoformat(),
    )
    with service.db.transaction() as db:
        db.execute(
            "UPDATE jobs SET deadline_at=? WHERE id=?", ((now - timedelta(seconds=1)).timestamp(), job["id"])
        )
    service.receive_playback_report(job["id"], report)
    assert jobs(client)[0]["state"] == ("waiting_for_feed" if finished_before_deadline else "timed_out")


def test_waiting_target_is_not_displaced_by_old_playback_recovery(rig):
    from controller.planner import choose

    _, service, _ = rig
    job, _ = complete_search(rig)
    with service.db.transaction() as db:
        device = service.db.device(db)
        items = service.items(db)
        other = next(i for i in items if i["content_id"] != job["content_id"] and i["playable"])
        device["observed"] = {
            "content_id": other["content_id"],
            "verified": False,
            "viewing_option_id": other["viewing_options"][0]["id"],
        }
        device["recovery"] = {
            "content_id": other["content_id"],
            "retry_after": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }
        decision = choose(device, items, service.now(db), datetime.now(UTC))
    assert decision["content_id"] == job["content_id"]
    assert decision["prime_readiness"] == "waiting_for_feed"


def test_locked_current_event_cannot_regain_selection_through_recovery_grace(rig):
    from controller.planner import choose

    _, service, _ = rig
    job, _ = complete_search(rig, "feeds_locked")
    with service.db.transaction() as db:
        device = service.db.device(db)
        items = service.items(db)
        target = next(i for i in items if i["content_id"] == job["content_id"])
        device["observed"] = {
            "content_id": job["content_id"],
            "verified": False,
            "viewing_option_id": target["viewing_options"][0]["id"],
        }
        device["recovery"] = {
            "content_id": job["content_id"],
            "retry_after": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }
        decision = choose(device, items, service.now(db), datetime.now(UTC))
    assert decision["content_id"] != job["content_id"]


async def test_no_identifiable_tiles_completes_without_retry_or_play(tmp_path):
    controller, workflow = workflow_rig(tmp_path)
    original = workflow.player.search

    async def unidentifiable(*args):
        return {**await original(*args), **results(tile(title=None, availability=None, labels=["TRENDING"]))}

    workflow.player.search = unidentifiable
    try:
        token = await launch(workflow)
        report = workflow.store.report(token)
        PlaybackReport.model_validate(report)
        assert report["operation"]["state"] == "no_matching_feed"
        assert report["operation"]["error"] is None
        assert report["prime_player"]["selection"]["candidates"] == []
        assert report["prime_player"]["selection"]["complete"] is False
        assert [c[0] for c in workflow.player.calls] == ["search"]
        workflow.recover()
        assert workflow.store.report(token)["operation"]["state"] == "no_matching_feed"
    finally:
        await cleanup(controller)


def test_no_match_remains_suppressed_beyond_former_backoff(rig):
    from unittest.mock import patch

    client, service, _ = rig
    job, _ = complete_search(rig, "no_matching_feed")
    with patch("controller.coordinator.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime.now(UTC) + timedelta(minutes=10)
        service.stage_playback()
    device = overview(client)["device"]
    assert device["desired"] != job["content_id"]
    assert device["prime_access"][job["content_id"]]["retry_after"] is None
    assert job["content_id"] not in device["failures"]
