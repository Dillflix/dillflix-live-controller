"""Shared discovery nominates candidates; the selected player verifies its own feed."""

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from broadcast_model import FixtureBroadcastModel
from playback_fixtures import settings
from test_entitlements import seed
from test_prime_workflow import Player, cleanup, row

from controller.config import DeviceSettings
from controller.entitlement_store import identity
from controller.planner import entitlement_eligible, selection_eligible
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


async def test_shared_receipt_only_nominates_and_never_reads_full_archives(shared, monkeypatch):
    controller, second = shared
    monkeypatch.setattr(controller, "items", lambda *a, **k: pytest.fail("Rebuilt another stream's catalogue"))
    with controller.db.snapshot() as db:
        db.set_authorizer(lambda action, table, column, *_:
                          sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ
                          and table == "event_entitlements" and column == "evidence"
                          else sqlite3.SQLITE_OK)
        item = next(i for i in second.selection_items(db) if i["content_id"] == "fixture-game")
        assert selection_eligible(item) and not entitlement_eligible(item)
        assert item["entitlement"]["status"] == "unknown"
        assert item["selection_entitlement"]["device_id"] == "living-room"
        assert db.execute("SELECT status FROM event_entitlements WHERE device_id='second-tv' "
                          "AND content_id='fixture-game'").fetchone()[0] == "unknown"
    assert not second.executor.player.calls


@pytest.mark.parametrize("change", ["identity", "route", "denied", "no_positive", "local_failure", "claimed"])
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
        elif change in {"denied", "no_positive"}:
            device_id = "second-tv" if change == "denied" else "living-room"
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
        assert probe["selection_entitlement"]["device_id"] == "living-room"
        assert not db.execute("SELECT 1 FROM jobs WHERE device_id='second-tv'").fetchone()
    assert not workflow.player.calls  # A borrowed receipt alone cannot launch.
    await workflow.observe_catalogue(probe)
    second.stage_playback()
    with controller.db.snapshot() as db:
        finished = controller.db.meta(db, key(second.device_id))
        assert finished["state"] == "ready" and finished["session_id"] == workflow.player.session
        job = db.execute("SELECT * FROM jobs WHERE device_id='second-tv' AND state='pending'").fetchone()
        request = json.loads(job["payload"])
        request["deadline_at"] = datetime.fromtimestamp(job["deadline_at"], UTC).isoformat()
        assert db.execute("SELECT COUNT(*) FROM event_entitlements WHERE device_id='second-tv' "
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
                item["entitlement_status"] = "NOT_ENTITLED"
        return result

    second.executor.player.search = denied
    second.stage_playback()
    with controller.db.snapshot() as db:
        probe = controller.db.meta(db, key(second.device_id))
    await second.executor.observe_catalogue(probe)
    second.stage_playback()
    with controller.db.snapshot() as db:
        assert not db.execute("SELECT 1 FROM jobs WHERE device_id='second-tv'").fetchone()
    assert not any(call[0] == "play" for call in second.executor.player.calls)
