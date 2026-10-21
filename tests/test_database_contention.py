import asyncio
import sqlite3
from unittest.mock import Mock

import pytest
from playback_fixtures import payload
from test_prime_workflow import cleanup, rig

from controller.database import is_database_busy
from controller.public import public_overview


def test_overviews_and_queue_scan_read_while_a_writer_holds_the_database(tmp_path):
    controller, workflow = rig(tmp_path)
    report, _ = workflow.store.submit(payload())
    with controller.db.transaction() as writer:
        # This write is deliberately uncommitted. Neither overview nor queue
        # polling should acquire a writer lock or observe partial changes.
        writer.execute("UPDATE devices SET revision=revision+1")
        admin = controller.overview()
        public = public_overview(controller, {"id": "guest:anonymous", "name": "Guest"})
        cancellations, row = workflow.pending_work()
        assert public["revision"] == admin["device"]["revision"]
        assert writer.execute("SELECT revision FROM devices").fetchone()[0] == public["revision"] + 1
        assert not cancellations and row["token"] == report["token"]


def test_snapshot_cannot_write_or_notify_and_keeps_consistent_reads(tmp_path):
    controller, _ = rig(tmp_path)
    controller.db.before_commit = Mock()
    controller.db.after_commit = Mock()
    with controller.db.snapshot() as snapshot:
        original = snapshot.execute("SELECT revision FROM devices").fetchone()[0]
        with controller.db.transaction() as writer:
            writer.execute("UPDATE devices SET revision=revision+1")
        controller.db.before_commit.reset_mock()
        controller.db.after_commit.reset_mock()
        assert snapshot.execute("SELECT revision FROM devices").fetchone()[0] == original
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            snapshot.execute("UPDATE devices SET revision=0")
    controller.db.before_commit.assert_not_called()
    controller.db.after_commit.assert_not_called()


def test_begin_failure_closes_connection(tmp_path, monkeypatch):
    controller, _ = rig(tmp_path)
    connection = Mock()
    connection.execute.side_effect = [None, sqlite3.OperationalError("database is locked")]
    monkeypatch.setattr("controller.database.sqlite3.connect", Mock(return_value=connection))
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        with controller.db.connection_transaction():
            pytest.fail("transaction did not begin")
    connection.close.assert_called_once()


async def wait_until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("stage", ["prune", "pending_work"])
async def test_executor_recovers_from_busy_scheduling_and_reloads_intent(tmp_path, monkeypatch, stage):
    controller, workflow = rig(tmp_path)
    original, _ = workflow.store.submit(payload())
    calls = 0
    owner = workflow.store if stage == "prune" else workflow
    saved = getattr(owner, stage)

    def busy_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return saved()

    monkeypatch.setattr(owner, stage, busy_once)
    launched, cancelled = [], []

    async def cancel_one(token):
        cancelled.append(token)
        with workflow.db.transaction() as db:
            db.execute("UPDATE executor_jobs SET cancel_requested=2 WHERE token=?", (token,))

    async def navigate(row):
        launched.append(row["token"])
        workflow.stopping = True

    monkeypatch.setattr(workflow, "navigate", navigate)
    monkeypatch.setattr(workflow, "cancel_one", cancel_one)
    task = asyncio.create_task(workflow.run())
    try:
        await wait_until(lambda: workflow.database_status["state"] == "waiting")
        assert not task.done() and not workflow.stopping and not launched
        # New intent arrives during backoff. Recovery must re-read it, process
        # the obsolete request's cancellation, and never launch that old intent.
        replacement, _ = workflow.store.submit(payload(intent=3))
        await asyncio.wait_for(task, 4)
        assert cancelled == [original["token"]]
        assert launched == [replacement["token"]]
        assert workflow.database_status["state"] == "ready"
        assert not workflow.player.calls
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await cleanup(controller)


async def test_non_lock_database_errors_still_stop_worker_visibly(tmp_path, monkeypatch, caplog):
    controller, workflow = rig(tmp_path)
    monkeypatch.setattr(workflow.store, "prune", Mock(side_effect=sqlite3.OperationalError("no such table: jobs")))
    try:
        await workflow.run()
        assert workflow.stopping
        assert "Playback executor stopped unexpectedly" in caplog.text
        assert not workflow.player.calls
    finally:
        await cleanup(controller)


async def test_database_wait_is_cancellable(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    monkeypatch.setattr(workflow.store, "prune", Mock(side_effect=sqlite3.OperationalError("database is locked")))
    task = asyncio.create_task(workflow.run())
    try:
        await wait_until(lambda: workflow.database_status["state"] == "waiting")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        assert not workflow.player.calls
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await cleanup(controller)


def test_only_sqlite_busy_and_locked_codes_are_retryable():
    error = sqlite3.OperationalError("busy snapshot")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY_SNAPSHOT
    assert is_database_busy(error)
    error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
    assert not is_database_busy(error)
    assert not is_database_busy(ValueError("database is locked"))


async def test_lock_after_play_dispatch_reconciles_saved_attempt_without_replay(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    report, _ = workflow.store.submit(payload())
    saved = workflow.store.journal_done
    blocked = False

    def busy_after_play(action_id, error=None):
        nonlocal blocked
        if not blocked and any(call[0] == "play" for call in workflow.player.calls):
            blocked = True
            raise sqlite3.OperationalError("database is locked")
        return saved(action_id, error)

    monkeypatch.setattr(workflow.store, "journal_done", busy_after_play)
    task = asyncio.create_task(workflow.run())
    try:
        await wait_until(lambda: workflow.database_status["state"] == "waiting")
        retained = workflow.store.report(report["token"])
        attempt = retained["prime_player"]["attempt_id"]
        assert retained["operation"]["state"] != "failed"
        await wait_until(lambda: workflow.store.report(report["token"])["operation"]["state"] == "playing_verified")
        assert workflow.store.report(report["token"])["prime_player"]["attempt_id"] == attempt
        assert len([call for call in workflow.player.calls if call[0] == "play"]) == 1
        assert not any(call[0] in {"stop", "cancel"} for call in workflow.player.calls)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await cleanup(controller)
