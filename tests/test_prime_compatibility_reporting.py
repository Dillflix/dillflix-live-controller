from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from test_prime_catalogue import saved, setup_selection
from test_prime_workflow import Player, cleanup, rig

from controller.executor.models import ExecutorError
from controller.prime_player.catalogue import key
from controller.prime_player.client import COMPATIBILITY_MESSAGES, PrimePlayerClient


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
    assert error.value.retryable is False


async def test_new_version_label_alone_does_not_mean_unsupported():
    health = await Player().health()
    health["compatibility"]["runtimeVersion"] = "future-version"
    PrimePlayerClient.require(health, "search")
