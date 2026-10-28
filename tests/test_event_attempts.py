import copy
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from controller.event_attempts import attempt_status, exhausted, record_failure
from controller.executor.integration import IntegratedPlaybackAdapter
from controller.models import Preferences
from controller.planner import choose
from controller.service import Controller
from test_controller import command, overview
from test_playback import jobs, pending, successful_report


def finish(service, job, state="feeds_unavailable", *, conclusive=True):
    report = successful_report(service, job)
    report.update(state=state, observation=None, finished_at=datetime.now(UTC).isoformat(),
                  nontechnical_failure=conclusive)
    assert service.receive_playback_report(job["id"], report)
    return report


def expire(service, content_id):
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["prime_access"][content_id]["expires_at"] = time.time() - 1
        service.db.save_device(db, device)


@pytest.mark.parametrize("state", ["feeds_unavailable", "feeds_locked", "no_matching_feed"])
def test_two_cycles_stop_across_expiry_network_changes_and_restart(rig, state):
    client, service, settings = rig
    first = pending(rig)
    cid = first["content_id"]
    report = finish(service, first, state)
    assert not service.receive_playback_report(first["id"], report)  # Receipt replay cannot count twice.
    assert overview(client)["device"]["event_attempts"][cid]["attempts"] == 1
    expire(service, cid)
    service.stage_playback()
    second = jobs(client)[0]
    assert second["id"] != first["id"] and second["content_id"] == cid
    finish(service, second, state)
    expire(service, cid)
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["prime_access"] = {}
        device["prime_network_scope"] = {"context_id": "replacement"}
        service.db.save_device(db, device)
    restarted = Controller(settings)
    with restarted.db.snapshot() as db:
        device = restarted.db.device(db)
        assert exhausted(device, cid)
        assert choose(device, restarted.items(db), restarted.now(db), datetime.now(UTC))["content_id"] != cid
        assert any(p["content_id"] == cid for p in device["plan"])
    card = next(e for e in overview(client)["events"] if e["content_id"] == cid)
    assert card["playback_retry"]["exhausted"]
    assert "Play now" in card["playback_retry"]["message"]
    # Explicit user intent resets only this event, and the normal coordinator can stage it again.
    assert command(client, {"type": "play_now", "content_id": cid}).status_code == 200
    assert cid not in overview(client)["device"]["event_attempts"]
    service.stage_playback()
    assert jobs(client)[0]["content_id"] == cid


@pytest.mark.parametrize("state,conclusive", [
    ("failed", False), ("timed_out", False), ("access_unknown", True),
    ("waiting_for_feed", True), ("feeds_unavailable", False),
])
def test_technical_and_incomplete_outcomes_do_not_consume_budget(rig, state, conclusive):
    client, service, _ = rig
    job = pending(rig)
    finish(service, job, state, conclusive=conclusive)
    assert not overview(client)["device"].get("event_attempts")


def test_success_resets_budget_and_other_device_is_independent(rig):
    client, service, _ = rig
    job = pending(rig)
    with service.db.transaction() as db:
        device = service.db.device(db)
        record_failure(device, job["content_id"], "previous", "feeds_locked", "before")
        other = copy.deepcopy(device)
        other.pop("event_attempts")
        assert not exhausted(other, job["content_id"])
        service.db.save_device(db, device)
    assert service.receive_playback_report(job["id"], successful_report(service, job))
    assert not overview(client)["device"]["event_attempts"]


def test_configurable_limit_preserved_by_legacy_clients_and_idempotent(rig):
    client, service, _ = rig
    first = pending(rig)
    cid = first["content_id"]
    finish(service, first)
    device = overview(client)["device"]
    body = {"command_id": str(uuid4()), "expected_revision": device["revision"],
            "rules": device["rules"], "team_ranks": device["team_ranks"],
            "preferences": {**device["preferences"], "event_attempt_limit": 1}}
    url = "/api/v1/devices/living-room/rules"
    result = client.put(url, json=body)
    assert result.status_code == 200, result.text
    assert client.put(url, json=body).json() == result.json()
    assert exhausted(overview(client)["device"], cid)
    body["command_id"] = str(uuid4())
    body["expected_revision"] = overview(client)["device"]["revision"]
    body["preferences"].pop("event_attempt_limit")
    assert client.put(url, json=body).status_code == 200
    assert overview(client)["device"]["preferences"]["event_attempt_limit"] == 1
    body.update(command_id=str(uuid4()), expected_revision=overview(client)["device"]["revision"])
    body["preferences"]["event_attempt_limit"] = 3
    assert client.put(url, json=body).status_code == 200
    assert not exhausted(overview(client)["device"], cid)


@pytest.mark.parametrize("limit", [0, -1, 11, True, 1.5, "2"])
def test_attempt_limit_validation(limit):
    with pytest.raises(ValueError):
        Preferences(event_attempt_limit=limit)


@pytest.mark.parametrize("unavailable", [False, True])
async def test_all_network_routes_count_as_one_cycle_unavailable_setup_does_not(tmp_path, monkeypatch, unavailable):
    from playback_fixtures import payload
    from test_prime_network import CITY_POLICY, network_rig
    from test_prime_workflow import cleanup

    controller, worker = network_rig(tmp_path)
    try:
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            device["preferences"]["vpn_policy"] = CITY_POLICY
            controller.db.save_device(db, device)
        request = payload()
        for side, city in zip(("away", "home"), ("Toronto", "Montreal")):
            request["content_snapshot"]["event"][side + "_team_details"]["city"] = city
        if unavailable:
            async def locations():
                return {"locations": []}
            worker.player.vpn_locations = locations
        async def locked(request, timezone, matcher, search, fetch, progress):
            return await search("Jets Lions"), {"readiness": "feeds_locked"}, {}
        monkeypatch.setattr("controller.prime_player.network.choose_search", locked)
        report, _ = worker.store.submit(request)
        await worker.acquire_network_catalogue(report["token"], request)
        final = worker.store.report(report["token"])
        projected = IntegratedPlaybackAdapter.project(final)
        assert projected["nontechnical_failure"] is not unavailable
        assert len(worker.player.transitions) == (0 if unavailable else 2)
        device = {"preferences": {}, "event_attempts": {}}
        if projected["nontechnical_failure"]:
            record_failure(device, request["content_id"], projected["request_id"], projected["state"], projected["finished_at"])
        assert (attempt_status(device, request["content_id"]) or {}).get("attempts", 0) == (0 if unavailable else 1)
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("fallback", [False, True])
async def test_catalogue_counts_only_complete_cycle_not_network_preflight(tmp_path, monkeypatch, fallback):
    from test_prime_catalogue import setup_selection, saved
    from test_prime_network import CITY_POLICY
    from test_prime_workflow import cleanup, rig as workflow_rig

    controller, worker = workflow_rig(tmp_path)
    _, item, _ = setup_selection(controller, monkeypatch)
    try:
        if fallback:
            with controller.db.transaction() as db:
                device = controller.db.device(db)
                device["preferences"]["vpn_policy"] = CITY_POLICY
                controller.db.save_device(db, device)
        controller.stage_playback()
        _, probe = saved(controller)
        evidence = {**probe, "state": "feeds_locked"}
        worker.finish_catalogue(probe, evidence)
        worker.finish_catalogue(probe, evidence)
        device, _ = saved(controller)
        assert (attempt_status(device, item["content_id"]) or {}).get("attempts", 0) == (0 if fallback else 1)
    finally:
        await cleanup(controller)
