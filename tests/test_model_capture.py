import io
import json
import sqlite3
import time
from copy import deepcopy

import pytest

from controller.model_capture import ModelCaptureStore, export_records, message_seed


def trace(call_id="call-1"):
    return {
        "call_id": call_id,
        "state": "returned",
        "task": "broadcast_selection",
        "context": {"content_id": "event:test:1", "device_id": "living-room"},
        "request": {
            "truncated": False,
            "json": {
                "model": "teacher-model",
                "temperature": 0,
                "messages": [{"role": "system", "content": "Choose a live feed."}],
                "response_format": {"type": "json_object"},
            },
        },
        "response": {
            "truncated": False,
            "json": {
                "model": "resolved-teacher-version",
                "usage": {"total_tokens": 100},
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": '{"match_status":"no_match"}'},
                }],
            },
        },
        "validation": {"state": "passed"},
    }


@pytest.fixture
def capture_store(tmp_path):
    from controller.database import Database

    path = tmp_path / "controller.sqlite3"
    Database(str(path))
    return ModelCaptureStore(path)


def records(store, *, format="raw"):
    output = io.StringIO()
    export_records(store.path, output, format=format)
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_pending_final_and_validation_upsert_one_durable_record(capture_store):
    value = trace()
    pending = {k: v for k, v in value.items() if k not in {"response", "validation"}}
    pending["state"] = "pending"
    assert capture_store.record(pending)
    assert capture_store.record({"call_id": value["call_id"], "state": "returned", "response": value["response"]})
    assert capture_store.record({"call_id": value["call_id"], "validation": value["validation"]})
    restarted = ModelCaptureStore(capture_store.path)
    saved = records(restarted)
    assert len(saved) == 1
    assert saved[0] | value == saved[0]
    assert saved[0]["schema_version"] == 1
    assert restarted.status()["count"] == 1
    assert restarted.status()["last_recorded_at"] == saved[0]["recorded_at"]


def test_capture_does_not_invoke_domain_transaction_hooks(capture_store):
    from controller.database import Database

    db = Database(capture_store.path)

    def reject(_connection):
        raise AssertionError("Capture must not trigger planner hooks")

    db.before_commit = reject
    assert capture_store.record(trace())
    assert records(capture_store)[0]["call_id"] == "call-1"


def test_capture_contention_fails_open_quickly_and_is_visible(capture_store, caplog):
    with sqlite3.connect(capture_store.path) as db:
        db.execute("BEGIN IMMEDIATE")
        value = trace()
        value["private_test_marker"] = "NEVER_LOG_THIS_PAYLOAD"
        started = time.monotonic()
        assert capture_store.record(value) is False
        elapsed = time.monotonic() - started
    assert elapsed < 1
    status = capture_store.status()
    assert status["count"] == 0
    assert status["state"] == "error"
    assert status["failure_count_since_start"] == 1
    assert status["last_error"]["code"] == "OperationalError"
    assert "NEVER_LOG_THIS_PAYLOAD" not in caplog.text
    assert capture_store.record(trace())
    assert capture_store.status()["state"] == "ok"
    assert capture_store.status()["last_error"] is not None


def test_invalid_or_excessive_capture_never_escapes(capture_store):
    assert capture_store.record({"call_id": None}) is False
    assert capture_store.record({"call_id": "large", "data": "x" * (4 * 1024 * 1024)}) is False
    assert capture_store.status()["count"] == 0


def test_export_does_not_create_or_migrate_database(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.Error):
        export_records(missing, io.StringIO())
    assert not missing.exists()
    existing = tmp_path / "old.sqlite3"
    with sqlite3.connect(existing) as db:
        db.execute("PRAGMA user_version=10")
    with pytest.raises(sqlite3.Error):
        export_records(existing, io.StringIO())
    with sqlite3.connect(existing) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 10
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='chooser_captures'").fetchone()


def test_export_reads_one_snapshot_while_new_calls_arrive(capture_store):
    assert capture_store.record(trace("first"))
    assert capture_store.record(trace("second"))

    class Output(io.StringIO):
        def write(self, value):
            if not self.tell():
                assert capture_store.record(trace("third"))
            return super().write(value)

    output = Output()
    assert export_records(capture_store.path, output) == 2
    assert [json.loads(line)["call_id"] for line in output.getvalue().splitlines()] == ["first", "second"]
    assert capture_store.status()["count"] == 3


def test_messages_export_keeps_review_and_model_provenance(capture_store):
    assert capture_store.record(trace())
    assert capture_store.record({"call_id": "interrupted", "state": "pending"})
    seeds = records(capture_store, format="messages")
    assert len(seeds) == 1
    seed = seeds[0]
    assert seed["call_id"] == "call-1"
    assert seed["messages"][-1] == {"role": "assistant", "content": '{"match_status":"no_match"}'}
    assert seed["metadata"]["review_status"] == "unreviewed"
    assert seed["metadata"]["request"]["model"] == "teacher-model"
    assert seed["metadata"]["response"]["model"] == "resolved-teacher-version"
    assert seed["metadata"]["context"]["content_id"] == "event:test:1"
    assert len(records(capture_store)) == 2


@pytest.mark.parametrize("change", [
    {"state": "failed"},
    {"state": "cancelled"},
    {"validation": None},
    {"validation": {"state": "failed"}},
    {"request": {"truncated": True, "json": {}}},
    {"response": {"truncated": True, "json": {}}},
    {"response": {"truncated": False, "text": "not JSON"}},
])
def test_seed_export_excludes_incomplete_or_unvalidated_calls(change):
    assert message_seed({**trace(), **change}) is None


@pytest.mark.parametrize("change", [
    {"finish_reason": "length"},
    {"message": {"role": "assistant", "content": "x" * 32769}},
    {"message": {"role": "tool", "content": "answer"}},
    {"message": {"role": "assistant", "content": None}},
])
def test_seed_export_requires_a_single_bounded_completed_assistant_answer(change):
    value = deepcopy(trace())
    value["response"]["json"]["choices"][0].update(change)
    assert message_seed(value) is None


@pytest.mark.parametrize("field", ["request", "response"])
def test_seed_export_excludes_redaction_changed_examples(field):
    value = trace()
    value[field]["redacted"] = True
    assert message_seed(value) is None


def test_migration_from_schema_ten_preserves_existing_records(rig):
    from controller.database import Database

    _, service, settings = rig
    with service.db.transaction() as db:
        before = list(db.execute("SELECT id,payload FROM devices"))
        db.execute("DROP TABLE chooser_captures")
        db.execute("PRAGMA user_version=10")
    upgraded = Database(settings.database)
    with upgraded.transaction() as db:
        assert [tuple(row) for row in db.execute("SELECT id,payload FROM devices")] == [tuple(row) for row in before]
        assert db.execute("PRAGMA user_version").fetchone()[0] == Database.SCHEMA_VERSION
        assert db.execute("SELECT COUNT(*) FROM chooser_captures").fetchone()[0] == 0


def test_capture_survives_retention_backup_and_restore(rig, tmp_path):
    from controller import ops
    from controller.maintenance import prune

    _, service, settings = rig
    store = ModelCaptureStore(settings.database)
    assert store.record(trace())
    assert prune(service.db, settings, service.owner)["removed"]["chooser_captures"] == 0
    backup = tmp_path / "backup.sqlite3"
    assert ops.backup(settings.database, backup)["counts"]["chooser_captures"] == 1
    restored = tmp_path / "restored.sqlite3"
    assert ops.restore(backup, restored, expected_mode="demo")["counts"]["chooser_captures"] == 1
    assert records(ModelCaptureStore(restored)) == records(store)
