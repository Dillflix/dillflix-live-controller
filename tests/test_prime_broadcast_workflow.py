import asyncio
import copy

import pytest
from test_prime_broadcasts import CAPTURE, ENGLISH
from test_prime_catalogue import saved, setup_selection
from test_prime_matching import GTI
from test_prime_workflow import cleanup, launch, rig


def install(workflow, *, french_only=False):
    original = workflow.player.outcome

    async def broadcasts(content_id, ownership):
        workflow.player.calls.append(("broadcasts", content_id))
        response = copy.deepcopy(CAPTURE)
        response.update(session_id=workflow.player.session, content_id=content_id)
        if french_only:
            row = response["resource"]["containerList"][0]
            row["items"] = row["items"][:1]
        return response

    workflow.player.broadcasts = broadcasts
    workflow.player.outcome = lambda: {**original(), "requested_id": ENGLISH, "resolved_id": ENGLISH}
    return broadcasts


async def test_direct_launch_uses_captured_english_child_id_and_keeps_teamarr_identity(tmp_path):
    controller, workflow = rig(tmp_path)
    install(workflow)
    try:
        token = await launch(workflow)
        report = workflow.store.report(token)
        selected = report["prime_player"]["selected"]
        assert report["operation"]["state"] == "playing_verified"
        assert selected["content_id"] == ENGLISH and selected["parent_content_id"] == GTI
        assert report["content_id"] == "fixture-game"
        assert [c[1] for c in workflow.player.calls if c[0] == "play"] == [ENGLISH]
        assert [c[1] for c in workflow.player.calls if c[0] == "broadcasts"] == [GTI]
        assert report["prime_player"]["selection"]["broadcast_selection"]["selected"]["language"] is None
    finally:
        await cleanup(controller)


async def test_french_only_does_not_issue_play(tmp_path):
    controller, workflow = rig(tmp_path)
    install(workflow, french_only=True)
    try:
        token = await launch(workflow)
        assert workflow.store.report(token)["operation"]["state"] == "no_matching_feed"
        assert not any(c[0] == "play" for c in workflow.player.calls)
    finally:
        await cleanup(controller)


async def test_different_resolved_broadcast_is_never_verified_as_the_selected_language(tmp_path):
    controller, workflow = rig(tmp_path)
    install(workflow)
    outcome = workflow.player.outcome
    workflow.player.outcome = lambda: {**outcome(), "resolved_id": GTI + "-different"}
    try:
        token = await launch(workflow)
        report = workflow.store.report(token)
        assert report["operation"]["state"] == "failed"
        assert report["operation"]["error"]["code"] == "prime_broadcast_mismatch"
    finally:
        await cleanup(controller)


async def test_language_preflight_preserves_current_intent_when_no_preferred_feed(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    install(workflow, french_only=True)
    _, item, intent = setup_selection(controller, monkeypatch)
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        device, evidence = saved(controller)
        assert device["intent_version"] == intent and device["desired"] == "previous-event"
        assert device["observed"]["verified"]
        assert evidence["state"] == "no_matching_feed"
        assert evidence["audit"]["broadcast_selection"]["match_status"] == "no_match"
        assert not any(c[0] in {"play", "cancel", "stop"} for c in workflow.player.calls)
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("change", ["intent", "manual", "session"])
async def test_late_broadcast_selection_cannot_authorize_a_switch(tmp_path, monkeypatch, change):
    controller, workflow = rig(tmp_path)
    request = install(workflow)
    setup_selection(controller, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args):
        entered.set()
        await release.wait()
        return await request(*args)

    workflow.player.broadcasts = delayed
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        task = asyncio.create_task(workflow.observe_catalogue(probe))
        await asyncio.wait_for(entered.wait(), 2)
        if change == "session":
            workflow.player.session = "restarted"
        else:
            with controller.db.transaction() as db:
                device = controller.db.device(db)
                if change == "intent":
                    device["intent_version"] += 1
                else:
                    device["manual_control"] = {"active": True}
                controller.db.save_device(db, device)
        release.set()
        await task
        _, evidence = saved(controller)
        assert evidence["state"] in {"discarded", "access_unknown"}
        assert not any(c[0] == "play" for c in workflow.player.calls)
    finally:
        release.set()
        await cleanup(controller)
