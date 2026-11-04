import copy
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest

from controller.database import Database
from controller.executor.models import ExecutorError
from controller.fleet import validate_saved_plan
from controller.models import VPNPolicy
from controller.network_policy import NetworkPolicy
from controller.planner import parse_time
from playback_fixtures import payload
from test_multi_stream import fleet
from test_prime_network import CITY_POLICY, network_rig
from test_prime_workflow import cleanup, row


@pytest.mark.parametrize("legacy", [None, 300, 600, 1800])
@pytest.mark.parametrize("locations", [False, True])
def test_legacy_policy_cannot_extend_acquisition(legacy, locations):
    policy = {"enabled": True, **({"locations": []} if locations else {"routes": {}})}
    if legacy is not None:
        policy["acquisition_timeout"] = legacy
    assert NetworkPolicy(policy).timeout == 300
    assert VPNPolicy().acquisition_timeout == 300
    assert NetworkPolicy({**policy, "acquisition_timeout": 120}).timeout == 120
    with pytest.raises(ValueError):
        VPNPolicy(acquisition_timeout=301)


async def test_navigation_and_network_fallback_share_five_minutes(tmp_path):
    c, w = network_rig(tmp_path)
    try:
        request = payload()
        request["deadline_at"] = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
        assert w.navigation_budget(request["allowed_viewing_options"], 1800) == 300
        report, _ = w.store.submit(request)
        operation = report["operation"]
        assert (parse_time(operation["deadline_at"]) - parse_time(operation["created_at"])).total_seconds() == 300
        original = row(w, report["token"])
        await w.navigate(original)
        assert w.store.report(report["token"])["operation"]["state"] == "playing_verified"
        assert len(w.player.transitions) == 1
        assert w.player.transitions[0]["deadline_at"] <= parse_time(operation["deadline_at"]).timestamp()
        # Idempotency compares the original authenticated body, not our cap.
        replay, created = w.store.submit(request)
        assert not created and replay["token"] == report["token"]
        saved = row(w, report["token"])
        assert saved["request"] == original["request"]
        assert saved["request_hash"] == original["request_hash"]
    finally:
        await cleanup(c)


async def test_expired_legacy_request_cancels_without_relaunch_after_restart(tmp_path):
    c, w = network_rig(tmp_path)
    try:
        request = payload()
        request["deadline_at"] = (datetime.now(UTC) + timedelta(minutes=25)).isoformat()
        report, _ = w.store.submit(request)
        token = report["token"]
        report["operation"].update(state="navigating", phase="network_starting_prime",
                                   created_at=(datetime.now(UTC) - timedelta(minutes=6)).isoformat(),
                                   deadline_at=request["deadline_at"])
        op = {"operation_id": "already-submitted", "target_id": "calgary",
              "deadline_at": parse_time(request["deadline_at"]).timestamp(), "interrupt_playback": True}
        report["prime_player"] = {"network_acquisition": {
            "operation": op, "deadline_at": op["deadline_at"], "policy_revision": w.network_policy.revision}}
        with w.db.transaction() as db:
            w.store.write(db, token, report, state="navigating")
        original = row(w, token)
        w.recover()
        recovered = w.store.report(token)
        assert parse_time(recovered["operation"]["deadline_at"]) < datetime.now(UTC)
        with w.db.snapshot() as db, pytest.raises(ExecutorError, match="deadline expired"):
            w.store.allowed(db, token, navigation=True)
        await w.navigate(row(w, token))
        assert w.store.report(token)["operation"]["state"] == "timed_out"
        assert row(w, token)["cancel_requested"] == 1
        assert not w.player.calls and not w.player.transitions
        saved = row(w, token)
        assert saved["request"] == original["request"]
        assert saved["request_hash"] == original["request_hash"]
        assert w.store.report(token)["prime_player"]["network_acquisition"]["operation"] == op
        cancelled = []
        async def cancel(operation_id, ownership):
            cancelled.append(operation_id)
            return {"acknowledged": True, "state": "cancelled"}
        w.player.vpn_cancel = cancel
        await w.interrupt_network(token, w.store.report(token)["prime_player"])
        assert cancelled == ["already-submitted"]
    finally:
        await cleanup(c)


@pytest.mark.parametrize("seconds", [120, 1800])
def test_upgrade_preserves_fleet_plan_and_tightens_pending_deadline(fleet, seconds):
    _, service, _ = fleet
    before = {}
    with service.db.transaction() as db:
        for device_id in ("living-room", "second-tv"):
            device = service.db.device(db, device_id)
            device["preferences"]["vpn_policy"] = {**copy.deepcopy(CITY_POLICY), "acquisition_timeout": seconds}
            service.db.save_device(db, device)
            before[device_id] = copy.deepcopy(device)
        canonical = service.db.meta(db, "fleet_plan")
        canonical["preferences"]["vpn_policy"] = copy.deepcopy(device["preferences"]["vpn_policy"])
        service.db.set_meta(db, "fleet_plan", canonical)
        db.execute("INSERT INTO jobs(id,device_id,intent,content_id,state,ready_at,payload,deadline_at) "
                   "VALUES ('old-queued','living-room',0,'game','pending',?,'{}',?)",
                   (time.time() - 600, time.time() + 1200))
        db.execute("PRAGMA user_version=21")
    upgraded = Database(service.db.path)
    with upgraded.snapshot() as db:
        validate_saved_plan(service, db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == Database.SCHEMA_VERSION
        assert db.execute("SELECT deadline_at FROM jobs WHERE id='old-queued'").fetchone()[0] < time.time()
        for device_id, original in before.items():
            after = service.db.device(db, device_id)
            assert after["preferences"]["vpn_policy"]["acquisition_timeout"] == min(seconds, 300)
            after["preferences"]["vpn_policy"]["acquisition_timeout"] = seconds
            after["revision"] = original["revision"]
            assert after == original
        snapshot = [tuple(r) for r in db.execute("SELECT * FROM devices ORDER BY id")]
    Database(service.db.path)
    with sqlite3.connect(service.db.path) as db:
        assert db.execute("SELECT * FROM devices ORDER BY id").fetchall() == snapshot
