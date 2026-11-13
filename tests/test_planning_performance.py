"""Regressions for feed churn, large evidence and optimistic preparation."""
import copy
import json
import sqlite3
import time
from datetime import timedelta
from unittest.mock import Mock

import pytest

from controller.database import Database, PreparationChanged, is_database_busy
from test_catalogue_cache import NOW, catalogue, read
from test_entitlements import seed
from test_prime_workflow import rig, cleanup


def snapshots(service):
    with service.db.snapshot() as db:
        return [json.loads(row[0]) for row in db.execute("SELECT snapshot FROM contents ORDER BY id")]


def publish(service, entries):
    prepared = service.prepare_catalog(entries)
    with service.db.transaction() as db:
        service.replace_catalog(db, entries, "teamarr", prepared=prepared)


def test_reimport_same_upstream_observation_keeps_cache_and_preparation(catalogue, monkeypatch):
    service, _, clock = catalogue
    entries = snapshots(service)
    for entry in entries:
        entry["status_received_at"] = NOW.isoformat()
    publish(service, entries)
    original = read(service)
    calls = []
    project = service._project_items
    monkeypatch.setattr(service, "_project_items", lambda *a, **kw: (calls.append(kw["content_ids"]), project(*a, **kw))[1])
    preparations = []

    def prepare(db):
        prepared = service.prepare_planning(db)
        preparations.append(1)
        # Import overlaps every attempt. A no-op refresh must not starve the
        # handoff, even though it commits and updates the import receipt.
        clock.current += timedelta(seconds=1)
        publish(service, entries)
        return prepared

    with service.db.transaction(prepare=prepare, version=service.planning_version) as db:
        assert service.items(db) == original
    assert preparations == [1]
    assert calls == []
    with service.db.snapshot() as db:
        assert {row[0] for row in db.execute("SELECT seen_at FROM contents")} == {clock.current.isoformat()}


@pytest.mark.parametrize("change", ["source_timestamp", "receipt_only", "missing", "title", "new"])
def test_feed_changes_refresh_only_affected_events(catalogue, monkeypatch, change):
    service, _, clock = catalogue
    entries = snapshots(service)
    for entry in entries:
        entry["status_received_at"] = NOW.isoformat()
    if change == "receipt_only":
        entries[0].pop("status_received_at")
    publish(service, entries)
    before = {item["content_id"]: item for item in read(service)}
    changed = entries[0]["id"]
    clock.current += timedelta(seconds=10)
    if change == "source_timestamp":
        entries[0]["status_received_at"] = clock.current.isoformat()
    elif change == "missing":
        entries.pop(0)
    elif change == "title":
        entries[0]["title"] = "Corrected event title"
    elif change == "new":
        entries.append({**copy.deepcopy(entries[0]), "id": "new-event"})
        changed = "new-event"
    publish(service, entries)
    calls = []
    original = service._project_items
    def project(*a, **kw):
        calls.append(kw["content_ids"])
        return original(*a, **kw)
    monkeypatch.setattr(service, "_project_items", project)
    after = {item["content_id"]: item for item in read(service)}
    assert calls == [{changed}]
    assert all(after[cid] == value for cid, value in before.items() if cid != changed)
    if change == "missing":
        assert after[changed]["active"] is False
        assert after[changed]["lifecycle"]["state"] != "ended"
    if change in {"source_timestamp", "receipt_only"}:
        assert after[changed]["lifecycle"]["received_at"] == clock.current.isoformat()


def test_new_revision_does_not_retain_all_obsolete_catalogues(catalogue):
    service, _, _ = catalogue
    with service.db.snapshot() as old:
        before = service.items(old)
        for generation in range(10):
            with service.db.transaction() as db:
                db.execute("UPDATE contents SET snapshot=json_set(snapshot,'$.title',?)", (f"revision-{generation}",))
            current = read(service)
            assert len(service.catalogue_cache.entries) == len(current)
        # Reading an older WAL snapshot still gives the old data, without
        # replacing current entries or growing retained memory again.
        keys = set(service.catalogue_cache.entries)
        assert service.items(old) == before
        assert set(service.catalogue_cache.entries) == keys
        assert read(service) == current
    assert len(service.catalogue_cache.families) == len(current)


def test_repeated_real_input_changes_abort_without_claiming_sqlite_lock_failure(tmp_path, monkeypatch, caplog):
    db = Database(str(tmp_path / "preparation.sqlite"))
    notifications = Mock()
    with db.transaction() as writer:
        db.set_meta(writer, "revision", 1)
    # Deterministically exhaust the preparation deadline without sleeping.
    ticks = iter([0, 11])
    def prepare(reader):
        value = db.meta(reader, "revision")
        with db.transaction() as writer:
            db.set_meta(writer, "revision", value + 1)
        monkeypatch.setattr("controller.database.time.monotonic", lambda: next(ticks, 11))
        return value
    monkeypatch.setattr("controller.database.time.monotonic", lambda: 0)
    with pytest.raises(PreparationChanged) as raised:
        with db.transaction(prepare=prepare, version=lambda c: db.meta(c, "revision")) as writer:
            writer.defer_after_commit(notifications)
            pytest.fail("stale mutation body executed")
    assert not is_database_busy(raised.value)
    assert "SQLite writer contention" not in caplog.text
    notifications.assert_not_called()
    with db.transaction():
        pass


async def test_queue_reads_covering_index_with_large_retained_evidence(tmp_path):
    service, _ = rig(tmp_path)
    seed(service, count=1000)
    try:
        evidence = json.dumps({"reason": "No Prime listing found", "archive": "x" * 65536})
        with service.db.transaction() as db:
            db.execute("UPDATE event_entitlements SET state='retry',evidence=?", (evidence,))
        with service.db.snapshot() as db:
            items = service.items(db)
            statements = []
            db.set_trace_callback(statements.append)
            started = time.perf_counter()
            queue = service.entitlement_queue(db, service.db.device(db), items)
            elapsed = time.perf_counter() - started
            db.set_trace_callback(None)
            query = next(s for s in statements if "v.evidence AS view_evidence" in s)
            plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query)]
            assert any("COVERING INDEX entitlement_summary" in row for row in plan), plan
            assert len(queue["rows"]) + len(queue["history_rows"]) == 1000
            print(f"queue: 1000 entries, 64 MiB retained audit bodies, {elapsed:.4f}s; {plan}")
    finally:
        await cleanup(service)


def test_schema_24_upgrade_preserves_data_and_adds_conditional_fences(catalogue):
    service, _, _ = catalogue
    with sqlite3.connect(service.db.path) as db:
        db.execute("DROP INDEX entitlement_summary")
        # Reconstruct the unconditional v24 triggers on the existing database.
        for name, sql in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'planning_%_update'").fetchall():
            db.execute('DROP TRIGGER "' + name + '"')
            prefix, body = sql.split(" WHEN ", 1)
            db.execute(prefix + " BEGIN" + body.split(" BEGIN", 1)[1])
        before = db.execute("SELECT * FROM contents ORDER BY id").fetchall()
        db.execute("PRAGMA user_version=24")
    upgraded = Database(service.db.path)
    with upgraded.snapshot() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM contents ORDER BY id")] == before
        assert db.execute("PRAGMA user_version").fetchone()[0] == 25
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='entitlement_summary'").fetchone()
