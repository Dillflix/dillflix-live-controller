"""A shared controller database must never turn one TV's work into another's."""

import asyncio
import json
import os
from dataclasses import replace

import httpx
import pytest
from broadcast_model import FixtureBroadcastModel
from playback_fixtures import payload, settings
from test_prime_workflow import Player

from controller.database import Database
from controller.executor.integration import IntegratedPlaybackAdapter
from controller.executor.models import CancelRequest, ExecutorError
from controller.fixtures import default_device
from controller.prime_player.client import PrimePlayerClient
from controller.prime_player.workflow import PrimePlaybackWorkflow


class BoundPlayer(Player):
    def __init__(self, serial):
        super().__init__()
        self.serial = serial
        self.session = "session:" + serial

    async def health(self):
        return {**await super().health(), "serial": self.serial}


@pytest.fixture
async def pair(tmp_path):
    config = settings(tmp_path, monitor_interval=100)
    database = Database(config.database)
    workflows = []
    with database.transaction() as db:
        for device_id in ("living-room", "second-tv"):
            device = default_device("teamarr")
            device.update(id=device_id, name=device_id)
            database.save_device(db, device)
    for device_id in ("living-room", "second-tv"):
        bound = replace(config, device_id=device_id, screen_adb_serial=device_id + ":5555")
        workflow = PrimePlaybackWorkflow(database, bound, player=BoundPlayer(bound.screen_adb_serial))
        workflow.matcher.model = FixtureBroadcastModel()
        workflows.append(workflow)
    try:
        yield database, *workflows
    finally:
        for workflow in workflows:
            await workflow.close()


def submit(workflow, intent=2):
    request = {**payload(intent), "device_id": workflow.device_id}
    report, _ = workflow.store.submit(request)
    return report


def raw_rows(database, device_id):
    with database.snapshot() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM executor_jobs WHERE device_id=? ORDER BY token", (device_id,))]


async def test_queues_and_cancellation_are_independent(pair):
    database, first, second = pair
    a, b = submit(first), submit(second)
    assert first.pending_work()[1]["token"] == a["token"]
    assert second.pending_work()[1]["token"] == b["token"]
    before = raw_rows(database, second.device_id)
    first.store.request_cancel(CancelRequest(device_id=first.device_id, token=a["token"]))
    assert [row["token"] for row in first.pending_work()[0]] == [a["token"]]
    assert second.pending_work()[0] == []
    assert second.pending_work()[1]["token"] == b["token"]
    assert raw_rows(database, second.device_id) == before


async def test_wrong_device_requests_tokens_and_cancellation_do_not_write(pair):
    database, first, second = pair
    b = submit(second)
    before = raw_rows(database, second.device_id)
    operations = [
        lambda: first.store.submit({**payload(), "device_id": second.device_id}),
        lambda: first.store.report(b["token"]),
        lambda: first.store.by_request(b["request_id"]),
        lambda: first.store.request_cancel(CancelRequest(device_id=second.device_id, token=b["token"])),
        lambda: first.store.request_cancel(CancelRequest(device_id=first.device_id, token=b["token"])),
        lambda: first.store.request_cancel(CancelRequest(device_id=second.device_id, through_intent_version=50)),
        lambda: first.store.journal(b["token"], "WRONG_DEVICE"),
    ]
    for operation in operations:
        with pytest.raises(ExecutorError) as error:
            operation()
        assert error.value.code == "device_mismatch"
    assert raw_rows(database, second.device_id) == before
    with database.snapshot() as db:
        assert database.device(db, second.device_id)["intent_version"] == 2
        assert not db.execute("SELECT 1 FROM executor_devices WHERE device_id=?", (first.device_id,)).fetchone()


async def test_restart_recovers_only_its_own_jobs_actions_and_catalogue(pair):
    database, first, second = pair
    a, b = submit(first), submit(second)
    first.store.journal(a["token"], "PRIME_SEARCH")
    second_action = second.store.journal(b["token"], "PRIME_SEARCH")
    with database.transaction() as db:
        for workflow in (first, second):
            database.set_meta(db, "prime_catalogue_probe:" + workflow.device_id, {"id": workflow.device_id})
            device = database.device(db, workflow.device_id)
            device["prime_access"] = {"retained": workflow.device_id}
            database.save_device(db, device)
    before = raw_rows(database, second.device_id)
    first.recover()
    assert raw_rows(database, second.device_id) == before
    with database.snapshot() as db:
        assert database.meta(db, "prime_catalogue_probe:" + first.device_id) is None
        assert database.meta(db, "prime_catalogue_probe:" + second.device_id) == {"id": second.device_id}
        assert database.device(db, second.device_id)["prime_access"] == {"retained": second.device_id}
        assert db.execute("SELECT state FROM executor_actions WHERE id=?", (second_action,)).fetchone()[0] == "dispatching"
        assert first.store.get_row(db, a["token"])["cancel_requested"] == 1


async def test_pruning_and_action_acknowledgment_stay_on_owner(pair):
    database, first, second = pair
    a, b = submit(first), submit(second)
    action = second.store.journal(b["token"], "PRIME_SEARCH")
    first.store.journal_done(action)
    with database.snapshot() as db:
        assert db.execute("SELECT state FROM executor_actions WHERE id=?", (action,)).fetchone()[0] == "dispatching"
    for workflow, report in ((first, a), (second, b)):
        workflow.store.request_cancel(CancelRequest(device_id=workflow.device_id, token=report["token"]))
        workflow.store.acknowledge_cancel(report["token"], "already_inactive")
    with database.transaction() as db:
        db.execute("UPDATE executor_jobs SET retired_at=1")
    before = raw_rows(database, second.device_id)
    first.store.prune()
    assert raw_rows(database, second.device_id) == before
    with database.snapshot() as db:
        assert first.store.get_row(db, a["token"])["request"] is None
        assert second.store.get_row(db, b["token"])["request"] is not None


async def test_health_and_runtime_changes_are_scoped(pair):
    database, first, second = pair
    with database.snapshot() as db:
        before = database.device(db, first.device_id)
    health = await second.player.health()
    health["playback_inspection"] = {"state": "degraded"}
    health["compatibility"]["adaptation"] = {"state": "discovering", "revision": "new-runtime"}
    health["network"] = {"enabled": True, "state": "ready", "context_id": "second-network",
                         "context": {"target_id": "montreal"}}
    second.record_runtime_adaptation(health)
    second.record_player_health(health)
    with database.snapshot() as db:
        assert database.device(db, first.device_id) == before
        updated = database.device(db, second.device_id)
        assert updated["player_recovery"]["session_id"] == second.player.session
        assert updated["prime_runtime_adaptation"]["revision"] == "new-runtime"
        assert updated["prime_network_scope"]["context_id"] == "second-network"


async def test_network_route_policy_comes_from_the_bound_device(pair):
    database, first, second = pair
    with database.transaction() as db:
        for workflow, target in ((first, "direct"), (second, "montreal")):
            device = database.device(db, workflow.device_id)
            device["preferences"]["vpn_policy"] = {"enabled": True, "routes": {
                "prime-option": {"targets": [target], "fallback_on": ["feeds_unavailable"]}}}
            database.save_device(db, device)
    options = payload()["allowed_viewing_options"]
    assert first.network_policy.rule(options)["targets"] == ["direct"]
    assert second.network_policy.rule(options)["targets"] == ["montreal"]


async def test_catalogue_probe_loses_authority_when_fleet_assignment_changes(pair):
    database, _, second = pair
    request = payload()
    probe = {"id": "probe-second", "device_id": second.device_id,
             "content_id": request["content_id"], "revision": 0, "intent": 0,
             "request": request, "signature": "previous-allocation"}
    with database.transaction() as db:
        device = database.device(db, second.device_id)
        device["stream_assignment"] = {"content_id": request["content_id"], "revision": 0}
        database.save_device(db, device)
        database.set_meta(db, "prime_catalogue_probe:" + second.device_id, probe)
    assert second.catalogue_current(probe)
    with database.transaction() as db:
        device = database.device(db, second.device_id)
        device["stream_assignment"]["content_id"] = "another-game"
        database.save_device(db, device)
    assert not second.catalogue_current(probe)
    second.finish_catalogue(probe, {**probe, "state": "ready"})
    with database.snapshot() as db:
        assert database.meta(db, "prime_catalogue_probe:" + second.device_id)["state"] == "discarded"
        assert not database.device(db, second.device_id).get("prime_access")


async def test_wrong_player_serial_cannot_launch_or_cancel(pair):
    _, first, second = pair
    first.player.serial = second.player.serial
    report = submit(first)
    with first.db.snapshot() as db:
        row = dict(first.store.get_row(db, report["token"]))
    await first.navigate(row)
    assert first.player.calls == []
    failed = first.store.report(report["token"])
    assert failed["operation"]["error"]["code"] == "prime_device_mismatch"
    # A retained owned attempt also cannot dispatch cleanup to a wrong socket.
    with first.db.transaction() as db:
        db.execute("UPDATE executor_devices SET current_token=? WHERE device_id=?", (report["token"], first.device_id))
        db.execute("UPDATE executor_jobs SET touched_device=1 WHERE token=?", (report["token"],))
    with pytest.raises(ExecutorError) as error:
        await first.cancel_one(report["token"])
    assert error.value.code == "prime_device_mismatch"
    assert first.player.calls == []


async def test_two_workflows_launch_without_cross_cancel(pair):
    _, first, second = pair
    a, b = submit(first), submit(second)
    with first.db.snapshot() as db:
        rows = [dict(workflow.store.get_row(db, report["token"]))
                for workflow, report in ((first, a), (second, b))]
    await asyncio.gather(first.navigate(rows[0]), second.navigate(rows[1]))
    for workflow, report in ((first, a), (second, b)):
        assert workflow.store.report(report["token"])["operation"]["state"] == "playing_verified"
        assert len([call for call in workflow.player.calls if call[0] == "play"]) == 1
        assert not [call for call in workflow.player.calls if call[0] in {"cancel", "stop"}]
    assert first.store.report(a["token"])["prime_player"]["session_id"] != second.store.report(b["token"])["prime_player"]["session_id"]


async def test_blocked_first_worker_does_not_block_or_get_cancelled_by_second(pair):
    _, first, second = pair
    release = asyncio.Event()
    first.player.search_release = release
    await first.start()
    await second.start()
    try:
        a, _ = first.submit({**payload(), "device_id": first.device_id})
        await asyncio.wait_for(first.player.search_entered.wait(), 3)
        first_task = first.active
        b, _ = second.submit({**payload(), "device_id": second.device_id})
        async with asyncio.timeout(20):
            while second.store.report(b["token"])["operation"]["state"] != "playing_verified":
                await asyncio.sleep(0.05)
        assert first.active is first_task and not first_task.done()
        assert not [call for call in first.player.calls if call[0] in {"cancel", "stop"}]
        release.set()
        async with asyncio.timeout(20):
            while first.store.report(a["token"])["operation"]["state"] != "playing_verified":
                await asyncio.sleep(0.05)
    finally:
        release.set()


async def test_failed_stream_startup_releases_resources_and_other_stream_runs(pair, monkeypatch):
    database, first, second = pair
    closed = []

    def fail_recovery():
        raise RuntimeError("Cannot reconcile this stream")

    async def close_player():
        closed.append(first.device_id)

    monkeypatch.setattr(first, "recover", fail_recovery)
    monkeypatch.setattr(first.player, "close", close_player)
    results = await asyncio.gather(first.start(), second.start(), return_exceptions=True)
    assert isinstance(results[0], RuntimeError) and results[1] is None
    assert first.stopping and first.loop is first.worker is first.guard is None
    assert closed == [first.device_id]
    with pytest.raises(ExecutorError) as error:
        first.submit({**payload(), "device_id": first.device_id})
    assert error.value.code == "executor_starting"
    assert raw_rows(database, first.device_id) == []
    with pytest.raises(ConnectionError, match="not running"):
        IntegratedPlaybackAdapter(first).observe_report(first.device_id)
    with pytest.raises(ExecutorError) as error:
        IntegratedPlaybackAdapter(second).observe_report(first.device_id)
    assert error.value.code == "device_mismatch"
    report, _ = second.submit({**payload(), "device_id": second.device_id})
    async with asyncio.timeout(20):
        while second.store.report(report["token"])["operation"]["state"] != "playing_verified":
            await asyncio.sleep(0.05)
    assert not first.player.calls


async def test_resource_close_failure_releases_only_its_own_worker_lock(pair, monkeypatch):
    _, first, second = pair
    await first.start()
    await second.start()
    guard, task = first.guard, first.worker

    async def fail_close():
        raise RuntimeError("Client teardown failed")

    with monkeypatch.context() as patch:
        patch.setattr(first, "close_resources", fail_close)
        with pytest.raises(RuntimeError, match="Client teardown failed"):
            await first.close()
    assert guard.closed and task.done()
    assert first.stopping and first.guard is first.loop is first.worker is None
    assert not second.guard.closed and not second.worker.done() and not second.stopping


@pytest.mark.skipif(os.name != "posix", reason="Requires the production Linux flock boundary")
async def test_independent_executor_locks_with_duplicate_owner_rejected(pair):
    _, first, second = pair
    await first.start()
    await second.start()
    duplicate = PrimePlaybackWorkflow(first.db, first.settings, player=BoundPlayer(first.player.serial))
    try:
        with pytest.raises(RuntimeError, match="Only one executor"):
            await duplicate.start()
        assert first.guard.name != second.guard.name
    finally:
        await duplicate.close()


async def test_client_checks_configured_serial_on_health():
    requests = []

    def handle(request):
        requests.append(json.loads(request.content)["method"])
        return httpx.Response(200, json={"result": {"session_id": "other-session", "serial": "other-tv"}})

    client = PrimePlayerClient("/test/first.sock", transport=httpx.MockTransport(handle), expected_serial="first-tv")
    try:
        with pytest.raises(ExecutorError) as error:
            await client.control_health()
        assert error.value.code == "prime_device_mismatch"
        assert error.value.retryable is False
        assert requests == ["health"]
    finally:
        await client.close()
