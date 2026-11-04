"""Historical playback evidence must not become scheduling work on every poll."""

from contextlib import contextmanager
import json
import sqlite3

import pytest

from controller.database import Database
from playback_fixtures import payload
from test_prime_workflow import rig


def seed_history(workflow):
    report, _ = workflow.store.submit(payload())
    with workflow.db.transaction() as db:
        original = dict(workflow.store.get_row(db, report["token"]))
        columns = list(original)
        for number in range(1200):
            row = {**original, "token": f"history-{number}", "request_id": f"history-{number}",
                   "intent": -number - 1, "state": "feeds_unavailable",
                   "cancel_requested": number % 2 * 2, "retired_at": 1,
                   "report": json.dumps({"retained_evidence": "x" * 8192})}
            db.execute("INSERT INTO executor_jobs (" + ",".join(columns) + ") VALUES ("
                       + ",".join("?" for _ in columns) + ")", [row[key] for key in columns])
    return report["token"]


def test_polling_does_not_walk_retained_history(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    token = seed_history(workflow)
    snapshot = controller.db.snapshot

    @contextmanager
    def bounded_snapshot():
        with snapshot() as db:
            # Deterministic VM budget, not a machine-dependent time assertion.
            # A scan across the 1,200 archived rows exceeds this budget.
            db.set_progress_handler(lambda: 1, 2000)
            yield db

    monkeypatch.setattr(controller.db, "snapshot", bounded_snapshot)
    cancellations, row = workflow.pending_work()
    assert not cancellations and row["token"] == token
    with controller.db.transaction() as db:
        db.execute("UPDATE executor_jobs SET next_check=99999999999 WHERE token=?", (token,))
    assert workflow.pending_work() == ([], None)
    with controller.db.transaction() as db:
        db.execute("UPDATE executor_jobs SET cancel_requested=1 WHERE token=?", (token,))
    cancellations, row = workflow.pending_work()
    assert [r["token"] for r in cancellations] == [token]
    assert row is None  # Even a deferred cancellation retains precedence.


def test_overview_entitlement_blockers_use_indexed_queue_lookups(tmp_path):
    controller, workflow = rig(tmp_path)
    token = seed_history(workflow)
    with controller.db.transaction() as db:
        db.execute("UPDATE executor_jobs SET state='failed',cancel_requested=2 WHERE token=?", (token,))
    with controller.db.snapshot() as db:
        device = controller.db.device(db)
        db.set_progress_handler(lambda: 1, 2000)
        assert controller.entitlement_blocker(db, device) is None
    with controller.db.transaction() as db:
        db.execute("UPDATE executor_jobs SET state='playing_verified',cancel_requested=0,next_check=0 "
                   "WHERE token=?", (token,))
    with controller.db.snapshot() as db:
        db.set_progress_handler(lambda: 1, 2000)
        assert controller.entitlement_blocker(db, device) == "Playback monitoring has priority"
        assert controller.entitlement_blocker(db, device, device_read=False) is None


def test_schema_20_upgrade_only_adds_indexes(tmp_path):
    controller, workflow = rig(tmp_path)
    token = seed_history(workflow)
    with sqlite3.connect(controller.db.path) as db:
        for name in ("executor_queue", "executor_cancellations", "executor_claims"):
            db.execute("DROP INDEX " + name)
        db.execute("PRAGMA user_version=20")
        before = db.execute("SELECT * FROM executor_jobs ORDER BY token").fetchall()
    upgraded = Database(controller.db.path)
    with upgraded.snapshot() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == Database.SCHEMA_VERSION
        assert [tuple(row) for row in db.execute("SELECT * FROM executor_jobs ORDER BY token")] == before
        assert db.execute("SELECT token FROM executor_jobs WHERE token=?", (token,)).fetchone()
        names = {row[1] for row in db.execute("PRAGMA index_list(executor_jobs)")}
        assert {"executor_queue", "executor_cancellations", "executor_claims"} <= names


def test_contention_reports_holder_stack_without_locals(tmp_path, monkeypatch, caplog):
    controller, _ = rig(tmp_path)
    with controller.db.transaction():
        secret_value = "must-not-appear-in-diagnostic"
        from unittest.mock import Mock
        blocked = Mock()
        blocked.execute.side_effect = [None, sqlite3.OperationalError("database is locked")]
        with monkeypatch.context() as patch:
            patch.setattr("controller.database.sqlite3.connect", Mock(return_value=blocked))
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                with controller.db.connection_transaction():
                    pytest.fail("must not enter a blocked transaction")
        assert "SQLite writer contention" in caplog.text
        assert "test_contention_reports_holder_stack_without_locals" in caplog.text
        assert '"phase":"transaction"' in caplog.text
        assert secret_value not in caplog.text
    assert controller.db.writer_diagnostic()["holder"] == "not_observed"


def test_post_commit_callbacks_do_not_report_a_held_write_lock(tmp_path):
    controller, _ = rig(tmp_path)
    observed = []
    with controller.db.transaction() as db:
        db.defer_after_commit(lambda: observed.append(controller.db.writer_diagnostic()))
    assert observed == [{"holder": "not_observed", "scope": "this_database_instance"}]
