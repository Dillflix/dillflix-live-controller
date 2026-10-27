"""One account queue, reusable receipts and independent physical playback checks."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from broadcast_model import FixtureBroadcastModel
from playback_fixtures import settings
from test_entitlements import due, seed
from test_prime_workflow import Player, cleanup, rig, row

from controller.config import DeviceSettings
from controller.database import Database
from controller.entitlement_store import identity, record_entitled
from controller.executor.models import ExecutorError
from controller.models import EntitlementCommand
from controller.planner import entitlement_eligible
from controller.prime_player.catalogue import key
from controller.service import Controller


@pytest.fixture
async def shared(tmp_path):
    controller = Controller(replace(settings(tmp_path), devices=(
        DeviceSettings("living-room", "One", "/test/one.sock", "fixture"),
        DeviceSettings("second-tv", "Two", "/test/two.sock", "fixture-two"),
    )))

    class SecondPlayer(Player):
        async def health(self):
            return {**await super().health(), "serial": "fixture-two"}

    second = controller.runtime("second-tv")
    controller.executor.player, second.executor.player = Player(), SecondPlayer()
    for runtime in controller.runtimes.values():
        runtime.executor.matcher.model = FixtureBroadcastModel()
    seed(controller, count=3)
    with controller.db.transaction() as db:
        first = controller.db.device(db)
        first.update(automation="paused", playback_state="verified",
                     observed={"content_id": "other-event", "verified": True})
        controller.db.save_device(db, first)
        item = next(i for i in controller.items(db) if i["content_id"] == "fixture-game")
        db.execute("UPDATE event_entitlements SET status='entitled',state='complete',evidence=? "
                   "WHERE device_id='living-room' AND content_id='fixture-game'",
                   (json.dumps({"identity": identity(item), "archive": "x" * 1000000}),))
    try:
        yield controller, second
    finally:
        await cleanup(controller)


async def test_shared_receipt_is_reused_without_rebuilding_other_catalogues_or_archives(shared, monkeypatch):
    controller, second = shared
    monkeypatch.setattr(controller, "items", lambda *a, **k: pytest.fail("Rebuilt another stream's catalogue"))
    with controller.db.snapshot() as db:
        db.set_authorizer(lambda action, table, column, *_:
                          sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ
                          and table == "event_entitlements" and column == "evidence"
                          else sqlite3.SQLITE_OK)
        item = next(i for i in second.planning_items(db) if i["content_id"] == "fixture-game")
        assert entitlement_eligible(item)
        assert item["entitlement"]["status"] == "entitled"
        assert not db.execute("SELECT 1 FROM event_entitlements WHERE device_id='second-tv'").fetchone()
    assert not second.executor.player.calls


@pytest.mark.parametrize("change", ["identity", "route", "no_positive", "local_failure", "claimed"])
async def test_shared_discovery_does_not_override_identity_or_local_constraints(shared, change):
    controller, second = shared
    with controller.db.transaction() as db:
        if change in {"identity", "route"}:
            snapshot = json.loads(db.execute("SELECT snapshot FROM contents WHERE id='fixture-game'").fetchone()[0])
            if change == "identity":
                snapshot["title"] = "A different event"
            else:
                snapshot["viewing_options"][0]["id"] = "changed-route"
            db.execute("UPDATE contents SET snapshot=? WHERE id='fixture-game'", (json.dumps(snapshot),))
        elif change == "no_positive":
            device_id = "living-room"
            db.execute("UPDATE event_entitlements SET status='unentitled' WHERE device_id=? "
                       "AND content_id='fixture-game'", (device_id,))
        elif change == "claimed":
            first = controller.db.device(db)
            first["observed"]["content_id"] = "fixture-game"
            controller.db.save_device(db, first)
        else:
            item = next(i for i in second.items(db) if i["content_id"] == "fixture-game")
            device = controller.db.device(db, second.device_id)
            device["prime_access"] = {"fixture-game": {"scope": None, "state": "feeds_locked",
                                                        "options": item["viewing_options"]}}
            controller.db.save_device(db, device)
    second.stage_playback()
    with controller.db.snapshot() as db:
        assert not controller.db.meta(db, key(second.device_id))
        assert not db.execute("SELECT 1 FROM jobs WHERE device_id='second-tv'").fetchone()


async def test_new_stream_checks_one_candidate_then_plays_without_background_rescan(shared):
    controller, second = shared
    workflow = second.executor
    second.stage_playback()
    with controller.db.snapshot() as db:
        probe = controller.db.meta(db, key(second.device_id))
        assert probe["content_id"] == "fixture-game"
        assert not db.execute("SELECT 1 FROM jobs WHERE device_id='second-tv'").fetchone()
    assert not workflow.player.calls  # Subscription access alone cannot launch.
    await workflow.observe_catalogue(probe)
    second.stage_playback()
    with controller.db.snapshot() as db:
        finished = controller.db.meta(db, key(second.device_id))
        assert finished["state"] == "ready" and finished["session_id"] == workflow.player.session
        job = db.execute("SELECT * FROM jobs WHERE device_id='second-tv' AND state='pending'").fetchone()
        request = json.loads(job["payload"])
        request["deadline_at"] = datetime.fromtimestamp(job["deadline_at"], UTC).isoformat()
        assert db.execute("SELECT COUNT(*) FROM event_entitlements WHERE device_id='living-room' "
                          "AND state='queued'").fetchone()[0] == 2
    report, _ = workflow.store.submit(request)
    await workflow.navigate(row(workflow, report["token"]))
    assert workflow.store.report(report["token"])["operation"]["state"] == "playing_verified"
    assert [c[0] for c in workflow.player.calls].count("search") == 1
    assert [c[0] for c in workflow.player.calls].count("play") == 1
    assert not controller.executor.player.calls


async def test_shared_positive_cannot_launch_when_local_search_is_unentitled(shared):
    controller, second = shared
    original = second.executor.player.search

    async def denied(*args):
        result = await original(*args)
        for container in result["containers"]:
            for item in container["items"]:
                item["entitlement_status"] = "UNENTITLED"
        return result

    second.executor.player.search = denied
    second.stage_playback()
    with controller.db.snapshot() as db:
        probe = controller.db.meta(db, key(second.device_id))
    await second.executor.observe_catalogue(probe)
    second.stage_playback()
    with controller.db.snapshot() as db:
        assert controller.db.meta(db, key(second.device_id))["state"] == "feeds_locked"
        assert not db.execute("SELECT 1 FROM jobs WHERE device_id='second-tv'").fetchone()
    assert not any(call[0] == "play" for call in second.executor.player.calls)


def queue_one(controller):
    with controller.db.transaction() as db:
        db.execute("DELETE FROM event_entitlements WHERE content_id!='fixture-game'")
        db.execute("UPDATE event_entitlements SET state='queued',status='unknown',evidence='{}',next_check=0")


async def test_two_stream_calls_share_one_search_and_result(shared):
    controller, second = shared
    queue_one(controller)
    await asyncio.gather(controller.check_entitlement_once(), second.check_entitlement_once())
    assert [c[0] for c in second.executor.player.calls] == ["search"]
    assert not controller.executor.player.calls
    for runtime in (controller, second):
        view = runtime.overview()
        event = next(e for e in view["events"] if e["content_id"] == "fixture-game")
        assert event["entitlement"]["status"] == "entitled"
        assert view["entitlement_queue"]["counts"]["complete"] == 1
    with controller.db.snapshot() as db:
        assert db.execute("SELECT COUNT(*) FROM event_entitlements").fetchone()[0] == 1
        evidence = json.loads(db.execute("SELECT evidence FROM event_entitlements").fetchone()[0])
        assert evidence["device_id"] == second.device_id


async def test_busy_player_is_skipped_and_service_failure_fails_over(shared):
    controller, second = shared
    queue_one(controller)
    async with second.executor.input_lock:
        await second.check_entitlement_once()
    assert [c[0] for c in controller.executor.player.calls] == ["search"]
    assert not second.executor.player.calls
    queue_one(controller)
    due(controller)
    probes = []
    async def unavailable():
        probes.append(1)
        raise ExecutorError("prime_unavailable", "Unavailable")
    second.executor.player.health = unavailable
    await controller.check_entitlement_once()
    assert len(probes) == 1
    assert [c[0] for c in controller.executor.player.calls] == ["search", "search"]
    assert controller.overview()["entitlement_queue"]["service_error"] is None
    assert second.overview()["entitlement_queue"]["service_error"] is None


async def test_manual_checks_from_either_stream_coalesce_while_in_flight(shared):
    controller, second = shared
    queue_one(controller)
    second.executor.player.search_release = asyncio.Event()
    task = asyncio.create_task(controller.check_entitlement_once())
    try:
        await asyncio.wait_for(second.executor.player.search_entered.wait(), 5)
        with controller.db.snapshot() as db:
            token = db.execute("SELECT check_id FROM event_entitlements").fetchone()[0]
        for runtime in (controller, second):
            runtime.entitlement_command(runtime.device_id, EntitlementCommand(
                command_id=runtime.device_id,
                expected_revision=runtime.overview()["device"]["revision"], content_id="fixture-game"))
        with controller.db.snapshot() as db:
            assert db.execute("SELECT check_id FROM event_entitlements").fetchone()[0] == token
            assert db.execute("SELECT COUNT(*) FROM event_entitlements").fetchone()[0] == 1
    finally:
        second.executor.player.search_release.set()
        await task
    await second.check_entitlement_once()
    assert [c[0] for c in second.executor.player.calls] == ["search"]
    assert not controller.executor.player.calls


async def test_foreground_positive_on_other_device_fences_background_result(shared):
    controller, second = shared
    queue_one(controller)
    second.executor.player.search_release = asyncio.Event()
    task = asyncio.create_task(controller.check_entitlement_once())
    try:
        await asyncio.wait_for(second.executor.player.search_entered.wait(), 5)
        with controller.db.transaction() as db:
            item = next(i for i in controller.items(db) if i["content_id"] == "fixture-game")
            record_entitled(db, controller.device_id,
                {"content_snapshot": item["snapshot"], "allowed_viewing_options": item["viewing_options"]},
                {"entitlement_status": "ENTITLED", "viewing_option_id": item["viewing_options"][0]["id"]},
                source="foreground", provenance={})
    finally:
        second.executor.player.search_release.set()
        await task
    with controller.db.snapshot() as db:
        result = db.execute("SELECT * FROM event_entitlements").fetchone()
        assert result["status"] == "entitled" and result["check_id"] is None
        assert json.loads(result["evidence"])["source"] == "foreground"
        assert json.loads(result["evidence"])["device_id"] == "living-room"


async def test_reimport_does_not_create_duplicate_jobs(shared):
    controller, second = shared
    for _ in range(2):
        with controller.db.transaction() as db:
            controller.seed_entitlements(db)
            second.seed_entitlements(db)
    with controller.db.snapshot() as db:
        assert db.execute("SELECT COUNT(*) FROM event_entitlements").fetchone()[0] == 3
        assert {r[0] for r in db.execute("SELECT DISTINCT device_id FROM event_entitlements")} == {"living-room"}
        assert next(i for i in second.items(db) if i["content_id"] == "fixture-game")["entitlement"]["status"] == "entitled"


async def test_adding_stream_reuses_existing_single_stream_catalogue(tmp_path):
    first, _ = rig(tmp_path)
    seed(first)
    config = first.settings
    try:
        await first.check_entitlement_once()
        with first.db.snapshot() as db:
            receipt = dict(db.execute("SELECT * FROM event_entitlements").fetchone())
    finally:
        await cleanup(first)
    controller = Controller(replace(config, devices=(
        DeviceSettings("living-room", "One", "/test/one.sock", "fixture"),
        DeviceSettings("second-tv", "Two", "/test/two.sock", "fixture-two"),
    )))
    try:
        with controller.db.snapshot() as db:
            assert db.execute("SELECT COUNT(*) FROM event_entitlements").fetchone()[0] == 1
            assert dict(db.execute("SELECT * FROM event_entitlements").fetchone()) == receipt
            assert controller.runtime("second-tv").items(db)[0]["entitlement"]["status"] == "entitled"
    finally:
        await cleanup(controller)


async def test_only_one_background_discovery_worker(shared, monkeypatch):
    from controller.background import BackgroundWorker
    controller, second = shared
    async def wait(self):
        await asyncio.Event().wait()
    monkeypatch.setattr(BackgroundWorker, "run", wait)
    controller.start()
    assert "entitlements" in controller.workers
    assert "entitlements" not in second.workers


async def test_upgrade_consolidates_receipts_and_preserves_original_audits(shared):
    controller, second = shared
    with controller.db.transaction() as db:
        db.execute("DELETE FROM metadata WHERE key='entitlement_account'")
        db.execute("PRAGMA user_version=16")
        for item in controller.items(db):
            valid = item["content_id"] != "event-2"
            status = "unentitled" if item["content_id"] == "fixture-game" else "entitled"
            evidence = json.dumps({"identity": identity(item) if valid else "obsolete", "archive": "x" * 10000})
            db.execute("INSERT INTO event_entitlements(device_id,content_id,status,state,checked_at,evidence) "
                       "VALUES ('second-tv',?,?,'complete','2099-01-01T00:00:00Z',?)",
                       (item["content_id"], status, evidence))
    Database(controller.db.path)
    with controller.db.snapshot() as db:
        assert db.execute("SELECT COUNT(*) FROM entitlement_migration_archive").fetchone()[0] == 6
        assert db.execute("SELECT COUNT(*) FROM event_entitlements").fetchone()[0] == 3
        values = {i["content_id"]: i["entitlement"]["status"] for i in second.items(db)}
        assert values == {"fixture-game": "entitled", "event-1": "entitled", "event-2": "unknown"}
        retained = json.loads(db.execute("SELECT evidence FROM event_entitlements WHERE content_id='event-1'").fetchone()[0])
        assert retained["device_id"] == "second-tv"
        assert retained["archive"] == "x" * 10000
        assert db.execute("SELECT state FROM event_entitlements WHERE content_id='event-2'").fetchone()[0] == "queued"
    Database(controller.db.path)
    with controller.db.snapshot() as db:
        assert db.execute("SELECT COUNT(*) FROM entitlement_migration_archive").fetchone()[0] == 6
