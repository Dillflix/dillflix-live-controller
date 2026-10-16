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

from controller.entitlements import DISPATCH_INTERVAL, SEARCH_SECONDS, identity, retry_delay
from controller.entitlement_store import record_entitled
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
        # Equal start times use the opaque ID only as a final tie-break.
        await controller.check_entitlement_once()
        with controller.db.transaction() as db:
            failed = dict(db.execute("SELECT * FROM event_entitlements WHERE state='retry'").fetchone())
        assert 299 < failed["next_check"] - time.time() <= 300
        await controller.check_entitlement_once()
        assert len(workflow.player.calls) == 1
        with controller.db.transaction() as db:
            delay = controller.db.meta(db, "entitlement_dispatch_after") - time.time()
        assert 0 < delay <= DISPATCH_INTERVAL == 5
        assert [retry_delay(i) for i in range(1, 10)] == [300, 600, 1200, 2400, 4800, 9600, 18000, 18000, 18000]
        with controller.db.transaction() as db:
            controller.db.set_meta(db, "entitlement_dispatch_after", 0)
        await controller.check_entitlement_once()
        assert len(workflow.player.calls) == 2
        assert record(controller, failed["content_id"])["failures"] == 1
    finally:
        await cleanup(controller)


async def test_live_and_plan_checks_precede_future_events_and_due_retries_are_not_starved(tmp_path):
    controller, workflow = rig(tmp_path)
    entries = seed(controller, count=6)
    now = datetime.now(UTC)
    for i, entry in enumerate(entries):
        entry.update(status="scheduled", start_time=(now + timedelta(hours=i + 1)).isoformat())
    entries[-1].update(status="live", start_time=now.isoformat())
    with controller.db.transaction() as db:
        controller.replace_catalog(db, entries, "teamarr")
        device = controller.db.device(db)
        device["plan"] = [controller.entry("event-4")]
        controller.db.save_device(db, device)
        db.execute("UPDATE event_entitlements SET state='retry',failures=1,next_check=? WHERE content_id='event-3'",
                   (time.time() - 600,))

    async def failing(*args):
        raise TimeoutError()

    workflow.player.search = failing
    try:
        for cid in ["event-5", "event-4", "event-3", "fixture-game", "event-1", "event-2"]:
            with controller.db.transaction() as db:
                controller.db.set_meta(db, "entitlement_dispatch_after", 0)
            await controller.check_entitlement_once()
            assert record(controller, cid)["state"] == "retry", cid
            assert record(controller, cid)["started_at"] is not None
        assert record(controller, "event-3")["failures"] == 2
        assert record(controller, "event-3")["next_check"] > time.time() + 590
    finally:
        await cleanup(controller)


async def test_manual_check_precedes_live_and_overdue_retries(tmp_path):
    controller, workflow = rig(tmp_path)
    entries = seed(controller, count=3)
    entries[0].update(status="scheduled", start_time=(datetime.now(UTC) + timedelta(days=2)).isoformat())
    with controller.db.transaction() as db:
        controller.replace_catalog(db, entries, "teamarr")
        db.execute("UPDATE event_entitlements SET state='retry',next_check=? WHERE content_id='event-1'",
                   (time.time() - 600,))
    controller.entitlement_command("living-room", manual(controller))
    try:
        await controller.check_entitlement_once()
        assert record(controller)["started_at"] is not None
        assert record(controller, "event-1")["started_at"] is None
        assert record(controller, "event-2")["started_at"] is None
    finally:
        await cleanup(controller)


async def test_background_search_has_time_to_return_without_launching(tmp_path):
    controller, workflow = rig(tmp_path)
    seed(controller)
    original = workflow.player.search

    async def search(query, timeout, ownership):
        # Production requests timed out at five seconds before identity matching.
        assert timeout == SEARCH_SECONDS == 15
        return await original(query, timeout, ownership)

    workflow.player.search = search
    try:
        await controller.check_entitlement_once()
        assert record(controller)["status"] == "entitled"
        assert [c[0] for c in workflow.player.calls] == ["search"]
    finally:
        await cleanup(controller)


async def test_foreground_upcoming_match_completes_background_job_without_playback(tmp_path):
    from controller.prime_player.catalogue import key

    controller, workflow = rig(tmp_path)
    seed(controller)
    original = workflow.player.search

    async def upcoming(*args):
        return {**await original(*args), **results(tile(event_state="UPCOMING"))}

    workflow.player.search = upcoming
    try:
        with controller.db.transaction() as db:
            item = controller.items(db)[0]
            workflow.prepare_selection(db, controller.db.device(db), item)
            probe = controller.db.meta(db, key("living-room"))
        await workflow.observe_catalogue(probe)
        row = record(controller)
        assert row["status"] == "entitled" and row["state"] == "complete"
        assert json.loads(row["evidence"])["source"] == "foreground_catalogue"
        await controller.check_entitlement_once()
        assert [c[0] for c in workflow.player.calls] == ["search"]
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("playing", [True, False])
async def test_existing_verified_playback_repairs_entitlement_without_another_search(tmp_path, playing):
    from test_prime_workflow import row as job_row

    controller, workflow = rig(tmp_path)
    entries = seed(controller)
    body = payload()
    body["content_snapshot"] = entries[0]
    try:
        report, _ = workflow.store.submit(body)
        token = report["token"]
        await workflow.navigate(job_row(workflow, token))
        assert record(controller)["status"] == "entitled"
        # Simulate the retained job from a pre-fix installation, then monitor.
        with controller.db.transaction() as db:
            db.execute("UPDATE event_entitlements SET state='queued',status='unknown',evidence='{}',next_check=0")
        workflow.player.calls.clear()
        workflow.player.state = "playing" if playing else "paused"
        await workflow.monitor(job_row(workflow, token))
        assert record(controller)["status"] == ("entitled" if playing else "unknown")
        assert [c[0] for c in workflow.player.calls] == ["status"]
        if playing:
            before = record(controller)
            assert json.loads(before["evidence"])["source"] == "verified_playback"
            await workflow.monitor(job_row(workflow, token))
            assert record(controller) == before  # No periodic entitlement refresh.
            assert controller.overview()["events"][0]["entitlement"]["status"] == "entitled"
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("change", ["event", "route", "not_entitled", "other_route", "other_device"])
async def test_foreground_evidence_must_match_current_event_route_and_device(tmp_path, change):
    controller, workflow = rig(tmp_path)
    entries = seed(controller)
    request = {"content_snapshot": entries[0], "allowed_viewing_options": entries[0]["viewing_options"]}
    selected = {"content_id": GTI, "viewing_option_id": "prime-option", "entitlement_status": "ENTITLED"}
    try:
        with controller.db.transaction() as db:
            changed = json.loads(json.dumps(entries))
            if change == "event":
                changed[0]["title"] = "Different event"
            elif change == "route":
                changed[0]["viewing_options"][0]["channel"] = "Another provider"
            elif change == "not_entitled":
                selected["entitlement_status"] = "UNENTITLED"
            elif change == "other_route":
                selected["viewing_option_id"] = "not-permitted"
            else:
                device = controller.db.device(db)
                device["id"] = "other-device"
                controller.db.save_device(db, device)
            controller.replace_catalog(db, changed, "teamarr")
            record_entitled(db, "other-device" if change == "other_device" else "living-room", request,
                            selected, source="foreground_catalogue", provenance={})
        with controller.db.transaction() as db:
            row = db.execute("SELECT status FROM event_entitlements WHERE device_id='living-room'").fetchone()
        assert row[0] == "unknown"
    finally:
        await cleanup(controller)


async def test_late_background_failure_cannot_replace_foreground_positive(tmp_path):
    controller, workflow = rig(tmp_path)
    entries = seed(controller)

    async def search(*args):
        with controller.db.transaction() as db:
            record_entitled(db, "living-room", {
                "content_snapshot": entries[0], "allowed_viewing_options": entries[0]["viewing_options"],
            }, {"content_id": GTI, "viewing_option_id": "prime-option", "entitlement_status": "ENTITLED"},
                source="foreground_catalogue", provenance={})
        raise TimeoutError()

    workflow.player.search = search
    try:
        await controller.check_entitlement_once()
        row = record(controller)
        assert row["status"] == "entitled" and row["state"] == "complete"
        assert row["check_id"] is None and row["next_check"] is None and row["failures"] == 0
        assert "error" not in json.loads(row["evidence"])
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("message,code", [
    ("catalogue search timed out; request may still be settling", "prime_search_timeout"),
    ("Unexpected application text", "prime_operation_unknown"),
])
async def test_search_timeout_is_identified_without_exposing_arbitrary_server_text(message, code):
    import httpx
    from controller.executor.models import ExecutorError
    from controller.prime_player.client import PrimePlayerClient

    def respond(request):
        return httpx.Response(409, json={"error": {"type": "UnavailableError", "message": message}})

    player = PrimePlayerClient("unused", transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ExecutorError) as error:
            await player.rpc("search", query="Wuhan Open: Day 5", timeout=15)
        assert error.value.detail()["code"] == code
        assert "Unexpected application text" not in str(error.value)
    finally:
        await player.close()


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


@pytest.mark.parametrize("fallback", [False, True])
async def test_monitor_becoming_due_during_search_does_not_discard_result(tmp_path, fallback):
    """Production's short monitor interval must not cause checking -> queued loops."""
    from dataclasses import replace

    controller, workflow = rig(tmp_path)
    seed(controller)
    workflow.config = replace(workflow.config, monitor_interval=5)
    try:
        token = await launch(workflow)
        workflow.player.calls.clear()
        original = workflow.player.search
        searches = []

        async def search(*args):
            result = await original(*args)
            searches.append(args[0])
            # Deterministically simulate a monitor becoming due during the RPC.
            with controller.db.transaction() as db:
                db.execute("UPDATE executor_jobs SET next_check=0 WHERE token=?", (token,))
            if fallback and len(searches) == 1:
                return {**result, "containers": []}
            return result

        workflow.player.search = search
        workflow.worker = asyncio.create_task(workflow.run())
        await controller.check_entitlement_once()
        assert record(controller)["status"] == "entitled"
        assert record(controller)["state"] == "complete"
        assert record(controller)["checked_at"] is not None
        assert len(searches) == (2 if fallback else 1)
        assert len(set(searches)) == len(searches)  # No replay of already fetched data.
        assert all(c[0] in {"search", "status"} for c in workflow.player.calls)
        if fallback:
            assert [c[0] for c in workflow.player.calls[:3]] == ["search", "status", "search"]
        assert workflow.store.report(token)["observation"]["verified"]
    finally:
        await cleanup(controller)


async def test_matching_can_finish_while_monitor_holds_device_lane(tmp_path):
    controller, workflow = rig(tmp_path)
    seed(controller)
    original_search, original_choose = workflow.player.search, workflow.matcher._choose
    release = asyncio.Event()

    async def monitor():
        async with workflow.input_lock:
            await release.wait()

    async def search(*args):
        result = await original_search(*args)
        workflow.active = asyncio.create_task(monitor())
        return result

    async def matching(*args, **kwargs):
        await asyncio.sleep(0)
        assert workflow.input_lock.locked()
        # The final priority fence must also allow this ordinary monitor.
        return await original_choose(*args, **kwargs)

    workflow.player.search, workflow.matcher._choose = search, matching
    try:
        await controller.check_entitlement_once()
        assert record(controller)["status"] == "entitled"
        assert not release.is_set()
    finally:
        release.set()
        if workflow.active:
            await workflow.active
        await cleanup(controller)


async def test_queue_exposes_never_checked_waiting_errors_and_retry(tmp_path):
    controller, workflow = rig(tmp_path)
    seed(controller)
    try:
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            device["manual_control"] = {"test": True}
            controller.db.save_device(db, device)
        await controller.check_entitlement_once()
        queue = controller.overview()["entitlement_queue"]
        assert queue["phase"] == "waiting"
        assert queue["reason"] == "Manual control is active"
        assert queue["counts"]["queued"] == 1
        assert queue["rows"][0]["entitlement"]["checked_at"] is None
        assert queue["failed_jobs"] == 0
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            device["manual_control"] = None
            controller.db.save_device(db, device)

        async def fail(*args):
            raise TimeoutError()

        workflow.player.search = fail
        await controller.check_entitlement_once()
        queue = controller.overview()["entitlement_queue"]
        assert queue["counts"]["retry"] == 1
        assert queue["failed_jobs"] == 1
        assert queue["reason"] == "Waiting for scheduled retries"
        assert queue["rows"][0]["error"]["code"] == "TimeoutError"
        assert queue["rows"][0]["entitlement"]["next_check_at"]
        assert queue["rows"][0]["failures"] == 1
        with controller.db.transaction() as db:
            controller.db.set_meta(db, "entitlement_worker", {"phase": "idle", "updated_at": 0})
        assert controller.overview()["entitlement_queue"]["phase"] == "unavailable"
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
        evidence = json.loads(record(controller)["evidence"])
        assert evidence["last_deferral"]["reason"] == "Playback launch or cancellation has priority"
        assert controller.overview()["entitlement_queue"]["rows"][0]["last_deferral"] == evidence["last_deferral"]
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
