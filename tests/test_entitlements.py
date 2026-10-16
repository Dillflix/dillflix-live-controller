"""Imported entitlement work is durable, bounded and never a playback request."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from playback_fixtures import payload
from test_prime_matching import GTI, results, tile
from test_prime_workflow import cleanup, launch, rig

from controller.entitlements import identity, retry_delay
from controller.models import Command, EntitlementCommand
from controller.prime_player.entitlement import choose_entitlement
from controller.service import Controller


def seed(controller, count=1):
    snapshot = payload()["content_snapshot"]
    snapshot["source"] = "games"
    entries = [{**snapshot, "id": "fixture-game" if i == 0 else f"event-{i}"} for i in range(count)]
    with controller.db.transaction() as db:
        controller.replace_catalog(db, entries, "teamarr")
    return entries


def record(controller, cid="fixture-game"):
    with controller.db.transaction() as db:
        return dict(db.execute("SELECT * FROM event_entitlements WHERE content_id=?", (cid,)).fetchone())


def due(controller):
    with controller.db.transaction() as db:
        db.execute("UPDATE event_entitlements SET next_check=0 WHERE state IN ('retry','queued')")
        controller.db.set_meta(db, "entitlement_dispatch_after", 0)


def manual(controller, cid="fixture-game", command_id=None):
    return EntitlementCommand(command_id=command_id or uuid4().hex,
                              expected_revision=controller.overview()["device"]["revision"], content_id=cid)


@pytest.mark.parametrize("state", ["UPCOMING", "LIVE", "UNAVAILABLE"])
async def test_entitled_is_independent_of_time_and_restriction(tmp_path, state):
    controller, workflow = rig(tmp_path)
    try:
        selected, audit = await choose_entitlement(workflow.matcher, payload(), results(tile(
            event_state=state, availability_status="UNAVAILABLE")), "UTC")
        assert selected["entitlement_status"] == "ENTITLED"
        assert selected["readiness"] == "ready"
        assert not workflow.player.calls
    finally:
        await cleanup(controller)


async def test_entitled_provider_wins_and_incomplete_denial_stays_unknown(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        denied = tile(entitlement_status="UNENTITLED", event_state="UPCOMING")
        entitled = tile(cid=GTI + "-dazn", event_state="UPCOMING")
        selected, _ = await choose_entitlement(workflow.matcher, payload(), results(denied, entitled), "UTC")
        assert selected["content_id"] == entitled["content_id"]
        selected, _ = await choose_entitlement(workflow.matcher, payload(), results(denied), "UTC")
        assert selected is None
        selected, _ = await choose_entitlement(workflow.matcher, payload(), {**results(denied), "complete": True}, "UTC")
        assert selected["entitlement_status"] == "UNENTITLED"
    finally:
        await cleanup(controller)


async def test_first_import_checks_once_and_persists_across_restart(tmp_path):
    controller, workflow = rig(tmp_path)
    entries = seed(controller)
    try:
        await controller.check_entitlement_once()
        assert record(controller)["status"] == "entitled"
        assert [c[0] for c in workflow.player.calls] == ["search"]
        with controller.db.transaction() as db:
            controller.replace_catalog(db, [], "teamarr")
            controller.replace_catalog(db, entries, "teamarr")
        await controller.check_entitlement_once()
        assert len(workflow.player.calls) == 1
        settings = controller.settings
    finally:
        await cleanup(controller)
    controller = Controller(settings)
    try:
        assert record(controller)["status"] == "entitled"
        assert record(controller)["next_check"] is None
    finally:
        await cleanup(controller)


async def test_upcoming_event_is_checked_before_its_start_and_can_be_planned(tmp_path):
    controller, workflow = rig(tmp_path)
    entries = seed(controller)
    entries[0].update(status="scheduled", start_time=(datetime.now(UTC) + timedelta(hours=12)).isoformat())
    with controller.db.transaction() as db:
        controller.replace_catalog(db, entries, "teamarr")
    original = workflow.player.search

    async def upcoming(*args):
        return {**await original(*args), **results(tile(event_state="UPCOMING", starts_at=entries[0]["start_time"]))}

    workflow.player.search = upcoming
    try:
        await controller.check_entitlement_once()
        assert record(controller)["status"] == "entitled"
        item = controller.overview()["events"][0]
        assert not item["playable"]
        assert not item["entitlement"]["blocked"]
        controller.plan_command("living-room", Command(command_id=uuid4().hex, expected_revision=0,
                                action={"type": "add", "content_id": "fixture-game"}))
        assert [c[0] for c in workflow.player.calls] == ["search"]
    finally:
        await cleanup(controller)


async def test_automatic_retry_backoff_and_global_dispatch_limit(tmp_path):
    controller, workflow = rig(tmp_path)
    seed(controller, count=2)

    async def failing(*args):
        workflow.player.calls.append(("search",))
        raise TimeoutError()

    workflow.player.search = failing
    try:
        # Order is by event ID, deliberately independent of import order.
        await controller.check_entitlement_once()
        with controller.db.transaction() as db:
            failed = dict(db.execute("SELECT * FROM event_entitlements WHERE state='retry'").fetchone())
        assert 299 < failed["next_check"] - time.time() <= 300
        await controller.check_entitlement_once()
        assert len(workflow.player.calls) == 1
        assert [retry_delay(i) for i in range(1, 10)] == [300, 600, 1200, 2400, 4800, 9600, 18000, 18000, 18000]
        with controller.db.transaction() as db:
            controller.db.set_meta(db, "entitlement_dispatch_after", 0)
        await controller.check_entitlement_once()
        assert len(workflow.player.calls) == 2
        assert record(controller, failed["content_id"])["failures"] == 1
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("condition", ["manual_control", "input_handoff", "recovery", "player_recovery", "lock", "pending"])
async def test_background_work_defers_without_consuming_attempt(tmp_path, condition):
    controller, workflow = rig(tmp_path)
    seed(controller)
    try:
        if condition == "lock":
            await workflow.input_lock.acquire()
        elif condition == "pending":
            workflow.store.submit(payload())
        else:
            with controller.db.transaction() as db:
                device = controller.db.device(db)
                device[condition] = {"test": True}
                controller.db.save_device(db, device)
        await controller.check_entitlement_once()
        assert record(controller)["state"] == "queued"
        assert record(controller)["failures"] == 0
        assert not workflow.player.calls
    finally:
        if workflow.input_lock.locked():
            workflow.input_lock.release()
        await cleanup(controller)


async def test_search_does_not_change_playback_and_monitor_has_priority(tmp_path):
    controller, workflow = rig(tmp_path)
    seed(controller)
    try:
        token = await launch(workflow)
        with controller.db.transaction() as db:
            db.execute("UPDATE executor_jobs SET next_check=0 WHERE token=?", (token,))
        await controller.check_entitlement_once()
        assert record(controller)["state"] == "queued"
        with controller.db.transaction() as db:
            db.execute("UPDATE executor_jobs SET next_check=? WHERE token=?", (time.time() + 100, token))
            before = dict(workflow.store.get_row(db, token))
        calls = len(workflow.player.calls)
        await controller.check_entitlement_once()
        assert record(controller)["status"] == "entitled"
        assert [c[0] for c in workflow.player.calls[calls:]] == ["search"]
        with controller.db.transaction() as db:
            assert dict(workflow.store.get_row(db, token)) == before
    finally:
        await cleanup(controller)


async def test_manual_check_bypasses_backoff_and_is_idempotent(tmp_path):
    controller, workflow = rig(tmp_path)
    seed(controller)
    try:
        await controller.check_entitlement_once()
        command = manual(controller)
        first = controller.entitlement_command("living-room", command)
        assert controller.entitlement_command("living-room", command) == first
        await controller.check_entitlement_once()
        assert len(workflow.player.calls) == 2
        assert record(controller)["state"] == "complete"
    finally:
        await cleanup(controller)


async def test_repeated_manual_click_cannot_duplicate_active_request(tmp_path):
    controller, workflow = rig(tmp_path)
    seed(controller)
    workflow.player.search_release = asyncio.Event()
    task = asyncio.create_task(controller.check_entitlement_once())
    try:
        await asyncio.wait_for(workflow.player.search_entered.wait(), 2)
        check_id = record(controller)["check_id"]
        controller.entitlement_command("living-room", manual(controller))
        assert record(controller)["check_id"] == check_id
        assert record(controller)["state"] == "checking"
        workflow.player.search_release.set()
        await task
        await controller.check_entitlement_once()
        assert len(workflow.player.calls) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await cleanup(controller)


async def test_manual_check_can_inspect_retained_event_outside_import_window(tmp_path):
    controller, workflow = rig(tmp_path)
    seed(controller)
    try:
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            device["plan"] = [controller.entry("fixture-game")]
            controller.db.save_device(db, device)
            controller.replace_catalog(db, [], "teamarr")
        await controller.check_entitlement_once()
        assert not workflow.player.calls
        controller.entitlement_command("living-room", manual(controller))
        await controller.check_entitlement_once()
        assert record(controller)["status"] == "entitled"
        assert [c[0] for c in workflow.player.calls] == ["search"]
    finally:
        await cleanup(controller)


async def test_ownership_change_during_search_cannot_publish_entitlement(tmp_path):
    controller, workflow = rig(tmp_path)
    seed(controller)
    original = workflow.player.search

    async def changed(*args):
        result = await original(*args)
        workflow.player.epoch += 1
        return result

    workflow.player.search = changed
    try:
        await controller.check_entitlement_once()
        observation = record(controller)
        assert observation["status"] == "unknown"
        assert json.loads(observation["evidence"])["error"]["code"] == "prime_stale_result"
        assert observation["next_check"] > time.time() + 290
    finally:
        await cleanup(controller)


async def test_foreground_work_arriving_after_search_prevents_fallback_query(tmp_path):
    controller, workflow = rig(tmp_path)
    seed(controller)
    original = workflow.player.search

    async def interrupted(*args):
        result = await original(*args)
        workflow.store.submit(payload())
        return {**result, "containers": []}

    workflow.player.search = interrupted
    try:
        await controller.check_entitlement_once()
        assert [c[0] for c in workflow.player.calls] == ["search"]
        assert record(controller)["state"] == "queued"
        assert record(controller)["failures"] == 0
    finally:
        await cleanup(controller)


async def test_foreground_work_arriving_during_match_prevents_additional_model_calls(tmp_path):
    controller, workflow = rig(tmp_path)
    seed(controller)
    original = workflow.player.search
    matches = []

    async def mixed(*args):
        return {**await original(*args), **results(tile(), tile(cid=GTI + "-unknown", entitlement_status=None))}

    async def selecting(*args, **kwargs):
        matches.append(kwargs["eligible"])
        workflow.store.submit(payload())
        return None, {"match_status": "uncertain"}

    workflow.player.search = mixed
    workflow.matcher._choose = selecting
    try:
        await controller.check_entitlement_once()
        assert len(matches) == 1
        assert record(controller)["state"] == "queued"
        assert record(controller)["failures"] == 0
    finally:
        await cleanup(controller)


async def test_pruned_unchecked_event_keeps_once_only_marker(tmp_path):
    from controller.maintenance import prune

    controller, workflow = rig(tmp_path)
    entries = seed(controller)
    try:
        with controller.db.transaction() as db:
            db.execute("UPDATE contents SET active=0,seen_at='2000-01-01T00:00:00Z'")
        prune(controller.db, controller.settings, controller.owner)
        assert record(controller)["state"] == "stopped"
        with controller.db.transaction() as db:
            controller.replace_catalog(db, entries, "teamarr")
        await controller.check_entitlement_once()
        assert not workflow.player.calls
    finally:
        await cleanup(controller)


async def test_denial_gates_actions_and_unknown_does_not(tmp_path):
    controller, workflow = rig(tmp_path)
    seed(controller)
    original = workflow.player.search

    async def denied(*args):
        return {**await original(*args), **results(tile(entitlement_status="UNENTITLED")), "complete": True}

    workflow.player.search = denied
    try:
        await controller.check_entitlement_once()
        assert record(controller)["status"] == "unentitled"
        for action in ("add", "play_now"):
            command = Command(command_id=uuid4().hex, expected_revision=0,
                              action={"type": action, "content_id": "fixture-game"})
            with pytest.raises(HTTPException, match="Subscription required"):
                controller.plan_command("living-room", command)
        view = controller.overview()["events"][0]
        assert view["entitlement"]["blocked"]
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            device["plan"] = [controller.entry("fixture-game")]
            controller.db.save_device(db, device)
            # Unknown status is not a subscription denial and doesn't erase the plan.
            db.execute("UPDATE event_entitlements SET status='unknown'")
        view = controller.overview()
        assert view["device"]["plan"]
        assert not view["events"][0]["entitlement"]["blocked"]
        controller.plan_command("living-room", Command(command_id=uuid4().hex, expected_revision=0,
                                action={"type": "add", "content_id": "fixture-game"}))
    finally:
        await cleanup(controller)


async def test_stale_identity_cannot_apply_negative_and_recovery_backs_off(tmp_path):
    controller, workflow = rig(tmp_path)
    entries = seed(controller)
    try:
        with controller.db.transaction() as db:
            item = controller.items(db)[0]
            db.execute("UPDATE event_entitlements SET status='unentitled',state='complete',evidence=?",
                       (json.dumps({"identity": identity(item)}),))
            entries[0]["start_time"] = "2030-01-01T12:00:00+00:00"
            controller.replace_catalog(db, entries, "teamarr")
        assert not controller.overview()["events"][0]["entitlement"]["blocked"]
        assert record(controller)["state"] == "complete"  # No automatic resweep.
        with controller.db.transaction() as db:
            db.execute("UPDATE event_entitlements SET state='checking',check_id='lost',started_at=0")
        await controller.check_entitlement_once()
        assert record(controller)["state"] == "retry"
        assert record(controller)["status"] == "unknown"
        assert record(controller)["next_check"] > time.time() + 290
        assert not workflow.player.calls
    finally:
        await cleanup(controller)


async def test_confirmed_completion_stops_retry_and_departed_schedule_is_not_scanned(tmp_path):
    controller, workflow = rig(tmp_path)
    seed(controller)
    try:
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            device["manual_completions"]["fixture-game"] = "2026-01-01T00:00:00+00:00"
            controller.db.save_device(db, device)
        await controller.check_entitlement_once()
        assert record(controller)["state"] == "stopped"
        seed(controller, count=2)
        with controller.db.transaction() as db:
            controller.replace_catalog(db, [], "teamarr")
        await controller.check_entitlement_once()
        assert not workflow.player.calls
    finally:
        await cleanup(controller)


def test_admin_only_recheck_and_public_action_gate(tmp_path):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from playback_fixtures import settings
    from test_prime_workflow import Player
    from test_public_access import ADMIN, USER
    from controller.api import create_app

    app = create_app(replace(settings(tmp_path), proxy_secret="test-proxy-secret"), start_workers=False)
    service = app.state.controller
    service.executor.player = Player()
    with TestClient(app) as client:
        seed(service)
        with service.db.transaction() as db:
            item = service.items(db)[0]
            db.execute("UPDATE event_entitlements SET status='unentitled',state='complete',evidence=?",
                       (json.dumps({"identity": identity(item)}),))
            device = service.db.device(db)
            device["public_access"] = {"play_now": True, "add_to_plan": True}
            service.db.save_device(db, device)
        command = manual(service).model_dump()
        path = "/api/v1/devices/living-room/entitlement-checks"
        assert client.post(path, headers=USER, json=command).status_code == 403
        first = client.post(path, headers=ADMIN, json=command)
        assert first.status_code == 200
        assert client.post(path, headers=ADMIN, json=command).json() == first.json()
        for action in ("add", "play_now"):
            response = client.post("/api/public/v1/watch-plan", headers=USER, json={
                "command_id": uuid4().hex, "expected_revision": first.json()["revision"],
                "action": action, "content_id": "fixture-game",
            })
            assert response.status_code == 422, response.text
            assert "Subscription required" in response.text
        public = client.get("/api/public/v1/overview", headers=USER).json()
        assert public["events"][0]["entitlement"]["blocked"]
        assert "evidence" not in public["events"][0]["entitlement"]
