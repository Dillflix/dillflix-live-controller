import asyncio
import copy
import json

import pytest
from broadcast_model import FixtureBroadcastModel
from test_prime_broadcast_workflow import install
from test_prime_broadcasts import ENGLISH
from test_prime_catalogue import saved, setup_selection
from test_prime_matching import GTI, results, tile
from test_prime_workflow import cleanup, launch, rig

from controller.executor.models import ExecutorError

SECOND = GTI + "-second"


class MatchingModel(FixtureBroadcastModel):
    async def completion(self, model, messages, schema, **kwargs):
        if kwargs.get("name") == "broadcast_selection":
            return await super().completion(model, messages, schema, **kwargs)
        candidates = json.loads(messages[1]["content"])["candidates"]
        candidate = next((c for c in candidates if c["title"] == "Jets vs. Lions"), None)
        return json.dumps(
            {
                "match_status": "matched" if candidate else "no_match",
                "content_id": candidate["content_id"] if candidate else None,
                "viewing_option_id": "prime-option" if candidate else None,
                "reason": "Match the fixture opponents; leave unrelated events alone",
                "evidence": [{"field": "title", "quote": candidate["title"]}] if candidate else [],
            }
        )

    async def close(self):
        pass


def alternatives(workflow, first="locked", second="ready"):
    original_search = workflow.player.search
    original_broadcasts = install(workflow)
    workflow.matcher.model = MatchingModel()

    async def search(*args):
        return {**await original_search(*args), **results(tile(), tile(cid=SECOND))}

    async def broadcasts(content_id, ownership):
        response = await original_broadcasts(content_id, ownership)
        mode = first if content_id == GTI else second
        items = response["resource"]["containerList"][0]["items"]
        if mode == "locked":
            # Reproduce Falcons: entitled French plus locked unlabeled TSN feeds.
            response["resource"]["containerList"][0]["items"] = [items[0], *items[2:]]
        elif mode == "french":
            response["resource"]["containerList"][0]["items"] = items[:1]
        elif mode == "upcoming":
            items[1]["liveliness"] = "UPCOMING"
        elif mode == "error":
            raise ExecutorError("prime_transport_unknown", "Broadcast request timed out")
        elif mode == "malformed":
            response["resource"] = None
        elif mode == "partial":
            response["resource"]["containerList"][0]["items"] = items[:1]
            response["resource"]["containerList"][0]["paginationLink"] = {"next": True}
        elif mode == "stale":
            response["session_id"] = "other-session"
        return response

    workflow.player.search = search
    workflow.player.broadcasts = broadcasts
    return broadcasts


@pytest.mark.parametrize("first", ["locked", "french", "upcoming", "error", "malformed", "partial"])
async def test_direct_launch_checks_second_parent_and_plays_its_entitled_broadcast(tmp_path, first):
    controller, workflow = rig(tmp_path)
    alternatives(workflow, first)
    try:
        token = await launch(workflow)
        report = workflow.store.report(token)
        assert report["operation"]["state"] == "playing_verified"
        assert [c[1] for c in workflow.player.calls if c[0] == "broadcasts"] == [GTI, SECOND]
        assert [c[1] for c in workflow.player.calls if c[0] == "play"] == [ENGLISH]
        assert report["prime_player"]["selected"]["parent_content_id"] == SECOND
        audit = report["prime_player"]["selection"]
        assert len(audit["parent_selections"]) == 2
        assert audit["parent_selections"][-1]["state"] == "ready"
        assert len(report["prime_player"]["catalogue"]["containers"][0]["items"]) == 2
    finally:
        await cleanup(controller)


async def test_preflight_checks_alternatives_before_changing_intent(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    alternatives(workflow)
    _, _, intent = setup_selection(controller, monkeypatch)
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        device, evidence = saved(controller)
        assert device["intent_version"] == intent and device["desired"] == "previous-event"
        assert device["observed"]["verified"]
        assert evidence["state"] == "ready"
        assert evidence["selected"]["parent_content_id"] == SECOND
        assert len(evidence["audit"]["parent_selections"]) == 2
        assert not any(c[0] in {"play", "cancel", "stop"} for c in workflow.player.calls)
        controller.stage_playback()
        assert saved(controller)[0]["intent_version"] == intent + 1
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("first", ["error", "malformed", "partial"])
async def test_unresolved_parent_prevents_durable_locked_exclusion(tmp_path, monkeypatch, first):
    controller, workflow = rig(tmp_path)
    alternatives(workflow, first, "locked")
    _, item, _ = setup_selection(controller, monkeypatch)
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        device, evidence = saved(controller)
        assert evidence["state"] == "access_unknown"
        assert device["prime_access"][item["content_id"]]["retry_after"] is not None
        assert len(evidence["audit"]["parent_selections"]) == 2
        assert not any(c[0] == "play" for c in workflow.player.calls)
        if first == "malformed":
            assert evidence["audit"]["error"]["code"] == "prime_invalid_broadcasts"
            reason = "Prime broadcast data could not be parsed; catalogue check retries in 60 seconds"
            assert device["prime_access"][item["content_id"]]["reason"] == reason
            with controller.db.transaction() as db:
                activity = db.execute(
                    "SELECT * FROM activity WHERE kind='prime_catalogue' ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
            assert activity["message"] == reason
            assert json.loads(activity["detail"])["error"]["code"] == "prime_invalid_broadcasts"
    finally:
        await cleanup(controller)


async def test_all_inspected_parents_locked_keeps_language_and_entitlement_distinct(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    alternatives(workflow, "locked", "locked")
    _, item, _ = setup_selection(controller, monkeypatch)
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        device, evidence = saved(controller)
        assert evidence["state"] == "feeds_locked"
        assert "English or unlabeled" in device["prime_access"][item["content_id"]]["reason"]
        assert len(evidence["audit"]["parent_selections"]) == 2
        assert not any(c[0] == "play" for c in workflow.player.calls)
    finally:
        await cleanup(controller)


async def test_success_stops_scan(tmp_path):
    controller, workflow = rig(tmp_path)
    alternatives(workflow, "ready")
    try:
        await launch(workflow)
        assert [c[1] for c in workflow.player.calls if c[0] == "broadcasts"] == [GTI]
    finally:
        await cleanup(controller)


async def test_upcoming_alternative_is_not_hidden_by_locked_parent(tmp_path):
    controller, workflow = rig(tmp_path)
    alternatives(workflow, "locked", "upcoming")
    try:
        token = await launch(workflow)
        report = workflow.store.report(token)
        assert report["operation"]["state"] == "waiting_for_feed"
        assert len(report["prime_player"]["selection"]["parent_selections"]) == 2
        assert not any(c[0] == "play" for c in workflow.player.calls)
    finally:
        await cleanup(controller)


async def test_unrelated_search_result_is_never_inspected_as_an_alternative(tmp_path):
    controller, workflow = rig(tmp_path)
    alternatives(workflow, "locked")
    search = workflow.player.search

    async def unrelated(*args):
        value = await search(*args)
        value["containers"][0]["items"][1]["title"] = "Ravens vs. Dolphins"
        return value

    workflow.player.search = unrelated
    try:
        token = await launch(workflow)
        assert workflow.store.report(token)["operation"]["state"] == "feeds_locked"
        assert [c[1] for c in workflow.player.calls if c[0] == "broadcasts"] == [GTI]
    finally:
        await cleanup(controller)


async def test_ambiguous_remaining_parent_prevents_locked_exclusion(tmp_path):
    controller, workflow = rig(tmp_path)
    alternatives(workflow, "locked")
    search = workflow.player.search

    async def ambiguous(*args):
        value = await search(*args)
        value["containers"][0]["items"][1]["title"] = "Monday football"
        return value

    workflow.player.search = ambiguous
    workflow.matcher.model = None  # The unknown label cannot be independently matched.
    try:
        token = await launch(workflow)
        assert workflow.store.report(token)["operation"]["state"] == "access_unknown"
        assert not any(c[0] == "play" for c in workflow.player.calls)
    finally:
        await cleanup(controller)


async def test_stale_session_does_not_try_another_parent(tmp_path):
    controller, workflow = rig(tmp_path)
    alternatives(workflow, "stale")
    try:
        token = await launch(workflow)
        assert workflow.store.report(token)["operation"]["error"]["code"] == "prime_stale_result"
        assert [c[1] for c in workflow.player.calls if c[0] == "broadcasts"] == [GTI]
        assert not any(c[0] == "play" for c in workflow.player.calls)
    finally:
        await cleanup(controller)


async def test_timeout_retains_partial_scan_without_excluding_event(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    broadcasts = alternatives(workflow)
    _, _, intent = setup_selection(controller, monkeypatch)
    entered = asyncio.Event()

    async def delayed(content_id, ownership):
        if content_id == SECOND:
            entered.set()
            await asyncio.Event().wait()
        return await broadcasts(content_id, ownership)

    workflow.player.broadcasts = delayed
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        task = asyncio.create_task(workflow.observe_catalogue(probe))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        device, evidence = saved(controller)
        assert evidence["state"] == "access_unknown"
        assert device["intent_version"] == intent
        assert [p["state"] for p in evidence["audit"]["parent_selections"]] == ["feeds_locked", "pending"]
    finally:
        await cleanup(controller)


async def test_upgrade_clears_old_parent_based_exclusions_but_retains_plan(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            device["prime_access"] = {"fixture-game": {"state": "feeds_locked", "retry_after": None}}
            device["plan"] = [{"content_id": "fixture-game"}]
            controller.db.save_device(db, device)
            controller.db.set_meta(db, "prime_catalogue_contract", 12)
            plan = copy.deepcopy(device["plan"])
        workflow.recover()
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            assert device["prime_access"] == {}
            assert device["plan"] == plan
            assert controller.db.meta(db, "prime_catalogue_contract") == 13
    finally:
        await cleanup(controller)
