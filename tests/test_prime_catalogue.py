"""Catalogue reads cannot supersede playback or authorize stale launches."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from playback_fixtures import payload
from test_prime_matching import results, tile
from test_prime_workflow import cleanup, launch, rig, row

from controller.planner import content_view
from controller.prime_player.catalogue import key
from controller.prime_player.diagnostics import collect


def setup_selection(controller, monkeypatch):
    request = payload()
    item = content_view(request["content_snapshot"], True, {"state": "live"})
    monkeypatch.setattr(controller, "items", lambda *args, **kwargs: [item])
    with controller.db.transaction() as db:
        device = controller.db.device(db)
        device.update(
            desired="previous-event",
            playback_state="verified",
            observed={"content_id": "previous-event", "verified": True},
            plan=[{"content_id": item["content_id"]}],
        )
        controller.db.save_device(db, device)
    return request, item, device["intent_version"]


def saved(controller):
    with controller.db.transaction() as db:
        return controller.db.device(db), controller.db.meta(db, key("living-room"))


async def finish_check(controller):
    """Drive the real asynchronous catalogue boundary in coordinator tests."""
    await asyncio.sleep(0)
    workflow = controller.executor
    task = getattr(workflow, "catalogue_task", None)
    if task:
        await asyncio.wait_for(asyncio.shield(task), 2)
    else:
        _, probe = saved(controller)
        if probe and probe["state"] == "pending":
            await workflow.observe_catalogue(probe)
    controller.stage_playback()


async def test_upcoming_refresh_then_live_changes_intent_only_after_ready(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    request, item, intent = setup_selection(controller, monkeypatch)
    original = workflow.player.search

    async def upcoming(*args):
        return {**await original(*args), **results(tile(event_state="UPCOMING"))}

    workflow.player.search = upcoming
    try:
        controller.stage_playback()
        device, probe = saved(controller)
        assert device["intent_version"] == intent and device["desired"] == "previous-event"
        assert device["observed"]["verified"]
        await workflow.observe_catalogue(probe)
        controller.stage_playback()
        device, probe = saved(controller)
        assert device["intent_version"] == intent and device["desired"] == "previous-event"
        assert device["prime_access"][item["content_id"]]["state"] == "waiting_for_feed"
        assert device["observed"]["verified"]
        assert [c[0] for c in workflow.player.calls] == ["search"]
        with controller.db.transaction() as db:
            assert not db.execute("SELECT 1 FROM jobs").fetchone()
            probe.update(retry_at=0, expires=0)
            controller.db.set_meta(db, key(device["id"]), probe)
            device["prime_access"][item["content_id"]]["retry_after"] = (
                datetime.now(UTC) - timedelta(seconds=1)
            ).isoformat()
            controller.db.save_device(db, device)
        workflow.player.search = original
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        controller.stage_playback()
        device, probe = saved(controller)
        assert device["intent_version"] == intent + 1 and device["desired"] == item["content_id"]
        with controller.db.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE state='pending'").fetchone()
            body = json.loads(job["payload"])
            body["deadline_at"] = datetime.fromtimestamp(job["deadline_at"], UTC).isoformat()
        report, _ = workflow.store.submit(body)
        await workflow.navigate(row(workflow, report["token"]))
        assert workflow.store.report(report["token"])["operation"]["state"] == "playing_verified"
        assert [c[0] for c in workflow.player.calls].count("search") == 2
        assert [c[0] for c in workflow.player.calls].count("play") == 1
        exported = collect(controller.settings.database)["catalogue_probe"]
        assert exported["results"]["containers"][0]["items"][0]["event_state"] == "LIVE"
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("change", ["revision", "intent_version", "manual_control", "automation"])
async def test_late_catalogue_result_is_discarded(tmp_path, monkeypatch, change):
    controller, workflow = rig(tmp_path)
    setup_selection(controller, monkeypatch)
    workflow.player.search_release = asyncio.Event()
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        task = asyncio.create_task(workflow.observe_catalogue(probe))
        await asyncio.wait_for(workflow.player.search_entered.wait(), 2)
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            if change in {"revision", "intent_version"}:
                device[change] += 1
            elif change == "automation":
                device[change] = "paused"
            else:
                device[change] = {"session_id": "manual"}
            controller.db.save_device(db, device)
        workflow.player.search_release.set()
        await task
        device, probe = saved(controller)
        assert probe["state"] == "discarded"
        assert not device.get("prime_access")
        assert not any(c[0] in {"play", "cancel", "stop"} for c in workflow.player.calls)
    finally:
        await cleanup(controller)


async def test_read_only_probe_preserves_active_executor_attempt(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    try:
        token = await launch(workflow)
        setup_selection(controller, monkeypatch)
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        with workflow.db.transaction() as db:
            assert db.execute("SELECT current_token FROM executor_devices").fetchone()[0] == token
        assert not row(workflow, token)["cancel_requested"]
        await workflow.monitor(row(workflow, token))
        assert workflow.store.report(token)["observation"]["verified"]
        assert not any(c[0] in {"cancel", "stop"} for c in workflow.player.calls)
    finally:
        await cleanup(controller)


async def test_replacement_barrier_does_not_stop_previous_playback(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        first = await launch(workflow)
        report, _ = workflow.store.submit(payload(intent=3))
        await workflow.cancel_one(first)
        assert not any(c[0] == "stop" for c in workflow.player.calls)
        assert workflow.store.report(first)["prime_player"]["playback_preserved_for_replacement"]
        await workflow.navigate(row(workflow, report["token"]))
        assert workflow.store.report(report["token"])["operation"]["state"] == "playing_verified"
    finally:
        await cleanup(controller)


async def test_confirmed_launch_refusal_is_unknown_not_upcoming_or_denial(tmp_path):
    controller, workflow = rig(tmp_path)
    original = workflow.player.outcome
    workflow.player.outcome = lambda: {
        **original(),
        "state": "failed",
        "resolved_id": None,
        "evidence": {"launch": {"disposition": "not_invoked"}, "resolution": {"playbackClass": "detail"}},
        "reason": "Resolver returned a non-playback result",
    }
    try:
        token = await launch(workflow)
        report = workflow.store.report(token)
        assert report["operation"]["state"] == "access_unknown"
        assert report["operation"]["error"]["code"] == "prime_launch_refused"
        assert "non-playback result" in report["operation"]["error"]["message"]
        assert report["prime_player"]["launch_outcome"]["evidence"]["launch"]["disposition"] == "not_invoked"
    finally:
        await cleanup(controller)


async def test_unavailable_catalogue_preserves_plan_without_play_or_upcoming_retry(tmp_path, monkeypatch):
    from test_prime_matching import UNAVAILABLE_BADGE

    controller, workflow = rig(tmp_path)
    _, item, intent = setup_selection(controller, monkeypatch)
    original = workflow.player.search

    async def search(*args):
        return {**await original(*args), **results(tile(entitlement_messaging=UNAVAILABLE_BADGE))}

    workflow.player.search = search
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        device, evidence = saved(controller)
        assert evidence["state"] == "feeds_unavailable"
        assert evidence["selected"]["entitlement_status"] == "ENTITLED"
        assert evidence["selected"]["event_state"] == "LIVE"
        access = device["prime_access"][item["content_id"]]
        assert "unavailable" in access["reason"] and access["retry_after"] is None
        assert device["intent_version"] == intent
        assert any(p["content_id"] == item["content_id"] for p in device["plan"])
        assert not any(c[0] in {"play", "stop", "cancel"} for c in workflow.player.calls)
    finally:
        await cleanup(controller)
