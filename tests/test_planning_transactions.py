"""Concurrency regressions: preparation must not occupy SQLite's writer slot."""
import copy
import sqlite3
from unittest.mock import Mock

import pytest

from controller.database import Database


def test_preparation_allows_another_writer_and_rebuilds_after_its_commit(tmp_path):
    database = Database(str(tmp_path / "db.sqlite"))
    with database.transaction() as db:
        database.set_meta(db, "test_revision", 1)
    versions = []
    notifications = Mock()

    def prepare(snapshot):
        value = database.meta(snapshot, "test_revision")
        versions.append(value)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            snapshot.execute("DELETE FROM metadata")
        if len(versions) == 1:
            # Independent connection commits while expensive preparation is in
            # progress. It would time out under the old BEGIN IMMEDIATE scope.
            with database.transaction() as writer:
                database.set_meta(writer, "test_revision", 2)
        return value

    with database.transaction(prepare=prepare) as writer:
        assert writer.prepared == 2
        writer.defer_after_commit(notifications)
        database.set_meta(writer, "accepted_revision", writer.prepared)
    assert versions == [1, 2]
    notifications.assert_called_once()


def test_preparation_failure_never_runs_write_hooks(tmp_path):
    database = Database(str(tmp_path / "db.sqlite"))
    database.before_commit = Mock()
    def prepare(db):
        raise ValueError("bad input")
    with pytest.raises(ValueError, match="bad input"):
        with database.transaction(prepare=prepare):
            pytest.fail("mutation body ran")
    database.before_commit.assert_not_called()
    with database.transaction():
        pass  # No lingering lock after failed preparation.


def test_coordinator_catalogue_is_constructed_before_writer_claim(rig, monkeypatch):
    _, controller, _ = rig
    original = controller._items
    calls = []
    def items(db, *args, **kwargs):
        calls.append(db.execute("PRAGMA query_only").fetchone()[0])
        # Demonstrate that playback/report writers are not held up by building
        # the catalogue. No changes, so preparation stays valid.
        with controller.db.transaction():
            pass
        return original(db, *args, **kwargs)
    monkeypatch.setattr(controller, "_items", items)
    controller.stage_playback()
    assert calls and all(calls)


def test_prepared_catalogue_reflects_plan_cleanup_inside_transaction(rig):
    _, controller, _ = rig
    with controller.db.transaction(prepare=controller.prepare_planning) as db:
        device = controller.db.device(db)
        item = controller.items(db)[0]
        device["manual_completions"][item["content_id"]] = "2026-01-01T00:00:00+00:00"
        controller.db.save_device(db, device)
        current = controller.items(db, content_ids={item["content_id"]})[0]
        assert current["lifecycle"]["state"] == "ended"


def test_read_only_http_endpoints_do_not_wait_for_writer(rig):
    client, controller, _ = rig
    with controller.db.transaction() as writer:
        writer.execute("UPDATE devices SET revision=revision+1")
        for path in ("/api/v1/teams", "/api/v1/devices/living-room/jobs",
                     "/api/v1/devices/living-room/configuration"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)


def test_scoped_lookup_and_status_work_ignore_unrelated_catalogue(rig):
    _, controller, _ = rig
    with controller.db.transaction() as db:
        selected = controller.items(db)[0]["content_id"]
        # An unrelated retained row must not even be decoded for one-event work.
        db.execute("INSERT INTO contents VALUES ('unrelated','demo',0,'2026-01-01','invalid JSON')")
    with controller.db.snapshot() as db:
        assert controller.items(db, content_ids={selected})[0]["content_id"] == selected
    controller.prepare_status_requests(force=True)


def test_import_preparation_does_not_reread_entire_catalogue(rig, monkeypatch):
    _, controller, _ = rig
    with controller.db.snapshot() as db:
        entries = [copy.deepcopy(item["snapshot"]) for item in controller.items(db)]
    prepared = controller.prepare_catalog(entries)
    monkeypatch.setattr(controller, "prepare_catalog", Mock(side_effect=AssertionError("prepared under writer")))
    with controller.db.transaction() as db:
        controller.replace_catalog(db, entries, "demo", prepared=prepared)
