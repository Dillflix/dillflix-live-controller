import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from test_prime_catalogue import saved, setup_selection
from test_prime_workflow import Player, cleanup, launch, rig, row

from controller.executor.models import ExecutorError
from controller.prime_player.catalogue import CATALOGUE_ERROR_MESSAGES, key
from controller.prime_player.client import COMPATIBILITY_MESSAGES, PrimePlayerClient, adaptation_status


def disable(health, kind):
    health = deepcopy(health)
    if kind == "old_api":
        health["api_version"] = 3
    elif kind == "old_catalogue_api":
        health["api_version"] = 10
    elif kind == "missing_api":
        health["capabilities"].remove("search")
    else:
        report = health["compatibility"]
        report["capabilities"]["search"] = {
            "available": False, "reasons": ["owning_dispatcher_not_validated"]
        }
        if kind == "quarantined":
            # Relevant fields from the October 6 runtime 116633 capture.
            report.update(
                runtimeVersion="lrc-1.0.116633.0",
                adapter="ignite-arm32-lrc116444-v1",
                quarantine={"javascript": True, "behavior": {"catalog_search": True}},
            )
    return health


@pytest.mark.parametrize(("kind", "code"), [
    ("quarantined", "prime_runtime_unsupported"),
    ("unvalidated", "prime_incompatible"),
    ("old_api", "prime_api_incompatible"),
    ("old_catalogue_api", "prime_api_incompatible"),
    ("missing_api", "prime_api_incompatible"),
])
async def test_compatibility_diagnosis_preserves_plan_and_recovers(tmp_path, monkeypatch, kind, code):
    controller, workflow = rig(tmp_path)
    _, item, intent = setup_selection(controller, monkeypatch)
    original = workflow.player.health

    async def unavailable():
        return disable(await original(), kind)

    monkeypatch.setattr(workflow.player, "health", unavailable)
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        controller.stage_playback()
        device, probe = saved(controller)
        assert probe["error"]["code"] == code
        access = device["prime_access"][item["content_id"]]
        assert access["reason"] == COMPATIBILITY_MESSAGES[code]
        assert access["state"] == "access_unknown" and access["retry_after"]
        assert device["reason"] == COMPATIBILITY_MESSAGES[code]
        assert device["intent_version"] == intent
        assert device["plan"] == [{"content_id": item["content_id"]}]
        assert device["observed"]["verified"]
        assert not workflow.player.calls
        with controller.db.transaction() as db:
            assert not db.execute("SELECT 1 FROM jobs").fetchone()
            activity = db.execute("SELECT * FROM activity WHERE kind='prime_catalogue'").fetchone()
            assert COMPATIBILITY_MESSAGES[code] in tuple(activity)
            # Simulate the next periodic check after installing a supported player.
            probe.update(retry_at=0, expires=0)
            controller.db.set_meta(db, key(device["id"]), probe)
            access["retry_after"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            controller.db.save_device(db, device)
        monkeypatch.setattr(workflow.player, "health", original)
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        controller.stage_playback()
        device, probe = saved(controller)
        assert probe["state"] == "ready"
        assert device["desired"] == item["content_id"]
        assert device["intent_version"] == intent + 1
    finally:
        await cleanup(controller)


async def test_unsupported_operation_does_not_disable_other_validated_capabilities():
    health = disable(await Player().health(), "quarantined")
    PrimePlayerClient.require(health, "playback_status", "cancel")
    with pytest.raises(ExecutorError) as error:
        PrimePlayerClient.require(health, "search")
    assert error.value.code == "prime_runtime_unsupported"


@pytest.mark.parametrize("mode", ["blocked", "transitioning", "manual"])
async def test_catalogue_ownership_failure_is_not_reported_as_entitlement(tmp_path, monkeypatch, mode):
    controller, workflow = rig(tmp_path)
    _, item, intent = setup_selection(controller, monkeypatch)
    original = workflow.player.health

    async def unavailable():
        health = await original()
        health["ownership"].update(mode=mode, acknowledged=mode == "manual")
        return health

    monkeypatch.setattr(workflow.player, "health", unavailable)
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        controller.stage_playback()
        device, probe = saved(controller)
        assert probe["error"]["code"] == "prime_ownership_unavailable"
        reason = CATALOGUE_ERROR_MESSAGES["prime_ownership_unavailable"]
        assert device["reason"] == reason
        assert device["prime_access"][item["content_id"]]["reason"] == reason
        assert device["intent_version"] == intent
        assert device["plan"] == [{"content_id": item["content_id"]}]
        assert not workflow.player.calls
    finally:
        await cleanup(controller)
    assert error.value.retryable is False


async def test_new_version_label_alone_does_not_mean_unsupported():
    health = await Player().health()
    health["compatibility"]["runtimeVersion"] = "future-version"
    PrimePlayerClient.require(health, "search")


@pytest.mark.parametrize("state", ["observing", "discovering", "validating", "backoff"])
async def test_adapting_runtime_retries_only_unavailable_capabilities(state):
    health = disable(await Player().health(), "quarantined")
    health["compatibility"]["adaptation"] = {"state": state, "revision": 7, "retry_after_seconds": 20}
    PrimePlayerClient.require(health, "playback_status", "cancel", "play")
    with pytest.raises(ExecutorError) as error:
        PrimePlayerClient.require(health, "search")
    assert error.value.code == "prime_runtime_recovering"
    assert error.value.retryable is True


async def test_incompatible_contract_without_legacy_quarantine_is_terminal_for_affected_capability():
    health = disable(await Player().health(), "unvalidated")
    health["compatibility"]["adaptation"] = {"state": "contract_incompatible"}
    PrimePlayerClient.require(health, "playback_status", "cancel")
    with pytest.raises(ExecutorError) as error:
        PrimePlayerClient.require(health, "search")
    assert error.value.code == "prime_runtime_unsupported"
    assert error.value.retryable is False


@pytest.mark.parametrize("kind", ["old_api", "missing_api"])
async def test_adaptation_does_not_mask_missing_service_api(kind):
    health = disable(await Player().health(), kind)
    health["compatibility"]["adaptation"] = {"state": "validating"}
    with pytest.raises(ExecutorError) as error:
        PrimePlayerClient.require(health, "search")
    assert error.value.code == "prime_api_incompatible"


@pytest.mark.parametrize(("hint", "expected"), [(0, 5), (3600, 300), (float("inf"), 15),
                                               (float("nan"), 15), ("soon", 15), (True, 15)])
def test_adaptation_retry_hints_are_bounded(hint, expected):
    health = {"compatibility": {"adaptation": {"state": "backoff", "retry_after_seconds": hint}}}
    assert adaptation_status(health)["retry_after_seconds"] == expected


async def test_adaptation_readiness_wakes_catalogue_without_plan_edits(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    _, item, intent = setup_selection(controller, monkeypatch)
    healthy = await workflow.player.health()
    health = disable(healthy, "quarantined")
    health["compatibility"]["adaptation"] = {"state": "backoff", "revision": 1, "retry_after_seconds": 120}

    async def latest():
        return deepcopy(health)

    monkeypatch.setattr(workflow.player, "health", latest)
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        controller.stage_playback()
        device, probe = saved(controller)
        assert probe["error"]["code"] == "prime_runtime_recovering"
        access = device["prime_access"][item["content_id"]]
        assert access["reason"] == COMPATIBILITY_MESSAGES["prime_runtime_recovering"]
        delay = datetime.fromisoformat(access["retry_after"]) - datetime.fromisoformat(access["observed_at"])
        assert delay == timedelta(seconds=120)
        assert device["prime_runtime_adaptation"]["revision"] == 1
        assert device["intent_version"] == intent and device["observed"]["verified"]
        assert not workflow.player.calls and not device["failures"]

        health = deepcopy(healthy)
        health["compatibility"]["adaptation"] = {"state": "ready", "revision": 2}
        await workflow.probe_player_recovery()
        device, probe = saved(controller)
        assert probe["retry_at"] == 0
        assert device["prime_runtime_adaptation"]["revision"] == 2
        assert device["intent_version"] == intent and device["observed"]["verified"]
        assert not workflow.player.calls  # Health refresh never searches or launches.
        controller.stage_playback()
        _, probe = saved(controller)
        assert probe["state"] == "pending"
        await workflow.observe_catalogue(probe)
        controller.stage_playback()
        device, probe = saved(controller)
        assert probe["state"] == "ready"
        assert device["intent_version"] == intent + 1
        assert device["desired"] == item["content_id"]
    finally:
        await cleanup(controller)


async def test_new_runtime_readiness_cannot_override_manual_takeover(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    _, _, intent = setup_selection(controller, monkeypatch)
    healthy = await workflow.player.health()
    unhealthy = disable(healthy, "quarantined")
    unhealthy["compatibility"]["adaptation"] = {"state": "discovering"}

    async def unavailable():
        return unhealthy

    original = workflow.player.health
    monkeypatch.setattr(workflow.player, "health", unavailable)
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            device["manual_control"] = {"session_id": "manual"}
            controller.db.save_device(db, device)
        monkeypatch.setattr(workflow.player, "health", original)
        await workflow.probe_player_recovery()
        controller.stage_playback()
        device, _ = saved(controller)
        assert device["manual_control"]["session_id"] == "manual"
        assert device["intent_version"] == intent and device["observed"]["verified"]
        assert not workflow.player.calls
        with controller.db.transaction() as db:
            assert not db.execute("SELECT 1 FROM jobs").fetchone()
    finally:
        await cleanup(controller)


async def test_adaptation_defers_new_launch_without_event_failure(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    healthy = await workflow.player.health()
    unhealthy = disable(healthy, "quarantined")
    unhealthy["compatibility"]["adaptation"] = {"state": "validating"}
    original = workflow.player.health

    async def unavailable():
        return unhealthy

    monkeypatch.setattr(workflow.player, "health", unavailable)
    try:
        token = await launch(workflow)
        report = workflow.store.report(token)
        assert report["prime_player"]["device_recovery"] is True
        assert report["observation_status"]["error"]["code"] == "prime_runtime_recovering"
        assert not row(workflow, token)["cancel_requested"] and not workflow.player.calls
        monkeypatch.setattr(workflow.player, "health", original)
        await workflow.navigate(row(workflow, token))
        assert workflow.store.report(token)["operation"]["state"] == "playing_verified"
        assert len([call for call in workflow.player.calls if call[0] == "play"]) == 1
    finally:
        await cleanup(controller)


async def test_generation_change_after_matching_prevents_play(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    original = workflow.player.health
    reads = 0

    async def changed_generation():
        nonlocal reads
        reads += 1
        health = await original()
        health["compatibility"]["generation"] = 1 if reads == 1 else 2
        return health

    monkeypatch.setattr(workflow.player, "health", changed_generation)
    try:
        token = await launch(workflow)
        report = workflow.store.report(token)
        assert report["operation"]["error"]["code"] == "prime_stale_result"
        assert not any(call[0] == "play" for call in workflow.player.calls)
    finally:
        await cleanup(controller)


async def test_controller_restart_retains_adaptation_and_refreshes_access_after_ready(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    _, item, intent = setup_selection(controller, monkeypatch)
    healthy = await workflow.player.health()
    unhealthy = disable(healthy, "quarantined")
    unhealthy["compatibility"]["adaptation"] = {"state": "discovering", "revision": "candidate-2"}
    original = workflow.player.health

    async def unavailable():
        return unhealthy

    monkeypatch.setattr(workflow.player, "health", unavailable)
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        with controller.db.transaction() as db:
            controller.db.set_meta(db, "prime_catalogue_contract", 13)
        workflow.recover()
        device, probe = saved(controller)
        assert not probe
        assert device["prime_runtime_adaptation"]["revision"] == "candidate-2"
        assert device["prime_access"][item["content_id"]]["runtime_error_code"] == "prime_runtime_recovering"
        monkeypatch.setattr(workflow.player, "health", original)
        await workflow.probe_player_recovery()
        controller.stage_playback()
        device, probe = saved(controller)
        assert probe["state"] == "pending"
        assert device["intent_version"] == intent
        assert not workflow.player.calls
    finally:
        await cleanup(controller)


async def test_existing_probe_lane_checks_adaptation_without_blocking_planner(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    original = workflow.player.health
    observed = asyncio.Event()

    async def health():
        observed.set()
        return await original()

    monkeypatch.setattr(workflow.player, "health", health)
    workflow.loop = asyncio.get_running_loop()
    try:
        value = await original()
        value["compatibility"]["adaptation"] = {"state": "validating"}
        workflow.record_runtime_adaptation(value)
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            device["prime_runtime_adaptation"]["next_probe_at"] = 0
            controller.db.save_device(db, device)
            assert workflow.player_recovery_ready(db, device) is True
        await asyncio.wait_for(observed.wait(), 2)
        await workflow.player_recovery_task
        assert not workflow.player.calls
    finally:
        await cleanup(controller)


async def test_prepared_catalogue_from_prior_generation_is_refetched(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    setup_selection(controller, monkeypatch)
    original_health = workflow.player.health
    original_search = workflow.player.search
    original_broadcasts = workflow.player.broadcasts
    generation = 1

    async def health():
        result = await original_health()
        result["compatibility"]["generation"] = generation
        return result

    async def search(*args):
        return {**await original_search(*args), "generation": generation}

    async def broadcasts(*args):
        return {**await original_broadcasts(*args), "generation": generation}

    monkeypatch.setattr(workflow.player, "health", health)
    monkeypatch.setattr(workflow.player, "search", search)
    monkeypatch.setattr(workflow.player, "broadcasts", broadcasts)
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        controller.stage_playback()
        with controller.db.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE state='pending'").fetchone()
            request = json.loads(job["payload"])
            request["deadline_at"] = datetime.fromtimestamp(job["deadline_at"], UTC).isoformat()
        generation = 2
        report, _ = workflow.store.submit(request)
        await workflow.navigate(row(workflow, report["token"]))
        report = workflow.store.report(report["token"])
        assert report["operation"]["state"] == "playing_verified"
        assert report["prime_player"]["search_generation"] == 2
        assert len([call for call in workflow.player.calls if call[0] == "search"]) == 2
        assert len([call for call in workflow.player.calls if call[0] == "play"]) == 1
    finally:
        await cleanup(controller)
