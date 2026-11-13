import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from controller.api import create_app
from controller.catalogue_cache import CatalogueCache
from controller.config import DeviceSettings, Settings
from controller.fixtures import fixtures


NOW = datetime(2026, 11, 5, 6, tzinfo=UTC)


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    class Clock(datetime):
        current = NOW
        @classmethod
        def now(cls, tz=None):
            return cls.current
    monkeypatch.setattr("controller.service.datetime", Clock)
    app = create_app(Settings(database=str(tmp_path / "catalogue.sqlite"), mode="teamarr",
        devices=(DeviceSettings("living-room", "Dillflix 1"), DeviceSettings("second-tv", "Dillflix 2"))),
        start_workers=False)
    with TestClient(app) as client:
        service = app.state.controller
        entries, _ = fixtures()
        entries = copy.deepcopy(entries[:3])
        for entry in entries:
            entry.update(status="live", start_time=(NOW - timedelta(hours=1)).isoformat())
        with service.db.transaction() as db:
            service.replace_catalog(db, entries, "teamarr", prepared=service.prepare_catalog(entries))
        yield service, client, Clock


def read(service, device=None):
    with service.db.snapshot() as db:
        return service.runtime(device).items(db)


def test_unchanged_active_ticks_and_http_views_reuse_projections(catalogue, monkeypatch):
    service, client, _ = catalogue
    # Let initial allocation establish each stream's different pins.
    for _ in range(2):
        for runtime in service.runtimes.values():
            runtime.stage_playback()
    for key in service.runtimes:
        read(service, key)
    for runtime in service.runtimes.values():
        def unexpected(*args, **kwargs):
            raise AssertionError("unchanged catalogue rebuilt")
        monkeypatch.setattr(runtime, "_project_items", unexpected)
    for _ in range(6):
        for runtime in service.runtimes.values():
            assert runtime.stage_playback()
        assert client.get("/api/v1/overview").status_code == 200
        assert client.get("/api/v1/devices/living-room/display").status_code == 200


def test_unrelated_heartbeat_and_returned_mutations_do_not_change_cache(catalogue):
    service, _, _ = catalogue
    original = read(service)
    altered = read(service)
    altered[0]["snapshot"]["title"] = "caller mutation"
    altered[0]["lifecycle"]["state"] = "ended"
    with service.db.transaction() as db:
        service.db.set_meta(db, "worker_heartbeat", 42)
        device = service.db.device(db)
        device["reason"] = "new status"
        service.db.save_device(db, device)
    assert read(service) == original


def test_catalogue_commit_invalidates_and_old_wal_snapshot_stays_consistent(catalogue):
    service, _, _ = catalogue
    with service.db.snapshot() as old:
        before = service.items(old)
        cid = before[0]["content_id"]
        with service.db.transaction() as db:
            db.execute("UPDATE contents SET snapshot=json_set(snapshot,'$.title','new title') WHERE id=?", (cid,))
        assert read(service)[0]["title"] == "new title"
        assert service.items(old)[0]["title"] == before[0]["title"]
    assert read(service)[0]["title"] == "new title"


def test_rolled_back_writer_cannot_publish_a_cache_entry(catalogue):
    service, _, _ = catalogue
    original = read(service)
    cid = original[0]["content_id"]
    with pytest.raises(RuntimeError):
        with service.db.transaction() as db:
            db.execute("UPDATE contents SET snapshot=json_set(snapshot,'$.title','uncommitted') WHERE id=?", (cid,))
            assert service.items(db)[0]["title"] == "uncommitted"
            raise RuntimeError("rollback")
    # A later commit can reuse the generation that was rolled back.
    with service.db.transaction() as db:
        db.execute("UPDATE contents SET snapshot=json_set(snapshot,'$.title','committed') WHERE id=?", (cid,))
    assert read(service)[0]["title"] == "committed"


def test_expiry_and_clock_rollback_refresh_without_a_write(catalogue):
    service, _, clock = catalogue
    initial = read(service)
    expiry = datetime.fromisoformat(initial[0]["lifecycle"]["effective_valid_until"])
    clock.current = expiry
    assert read(service)[0]["lifecycle"]["state"] == "unknown"
    clock.current = NOW
    assert read(service)[0]["lifecycle"]["state"] == "live"


def test_scheduled_prime_start_refreshes_without_database_write(catalogue):
    service, _, clock = catalogue
    # Enable projection semantics only; this test performs no player operation.
    original_executor = service.executor
    service.executor = SimpleNamespace()
    try:
        with service.db.transaction() as db:
            cid = db.execute("SELECT id FROM contents ORDER BY id LIMIT 1").fetchone()[0]
            snapshot = json.loads(db.execute("SELECT snapshot FROM contents WHERE id=?", (cid,)).fetchone()[0])
            snapshot.update(status="scheduled", start_time=(NOW + timedelta(seconds=10)).isoformat(),
                            viewing_options=[{"id": "prime", "app": "prime_video"}])
            db.execute("UPDATE contents SET snapshot=? WHERE id=?", (json.dumps(snapshot), cid))
        assert read(service)[0]["playable"] is False
        clock.current = NOW + timedelta(seconds=10)
        assert read(service)[0]["launch_eligibility"] == "scheduled_start_reached"
    finally:
        service.executor = original_executor


def test_entitlement_change_rebuilds_only_the_changed_event(catalogue, monkeypatch):
    service, _, _ = catalogue
    original_executor = service.executor
    service.executor = SimpleNamespace()
    try:
        with service.db.transaction() as db:
            cid = db.execute("SELECT id FROM contents ORDER BY id LIMIT 1").fetchone()[0]
            db.execute("UPDATE contents SET snapshot=json_set(snapshot,'$.viewing_options',json(?)) WHERE id=?",
                       (json.dumps([{"id": "prime", "app": "prime_video"}]), cid))
            db.execute("INSERT INTO event_entitlements(device_id,content_id) VALUES (?,?)", (service.device_id, cid))
        assert read(service)[0]["entitlement"]["status"] == "unknown"
        original = service._project_items
        calls = []
        def scoped(*args, **kwargs):
            calls.append(kwargs["content_ids"])
            return original(*args, **kwargs)
        monkeypatch.setattr(service, "_project_items", scoped)
        with service.db.transaction() as db:
            db.execute("UPDATE event_entitlements SET status='entitled',state='complete' WHERE content_id=?", (cid,))
        assert read(service)[0]["entitlement"]["status"] == "entitled"
        assert calls == [{cid}]
    finally:
        service.executor = original_executor


def test_manual_completion_and_stream_pins_refresh_projection(catalogue):
    service, _, _ = catalogue
    cid = read(service)[0]["content_id"]
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["manual_completions"][cid] = NOW.isoformat()
        device["desired"] = cid
        service.db.save_device(db, device)
    item = read(service)[0]
    assert item["lifecycle"]["state"] == "ended"
    assert item["lifecycle"]["tracked"] is True
    # Completion is shared; the physical stream's desired pin is not.
    assert read(service, "second-tv")[0]["lifecycle"]["tracked"] is False


def test_new_status_observation_invalidates_cached_lifecycle(catalogue):
    service, _, _ = catalogue
    initial = read(service)[0]
    cid = initial["content_id"]
    observation = {"content_id": cid, "state": "ended", "source": "provider",
                   "observed_at": NOW.isoformat(), "received_at": NOW.isoformat(),
                   "valid_until": (NOW + timedelta(seconds=60)).isoformat(),
                   "simulated": False, "timestamp_basis": "provider"}
    with service.db.transaction() as db:
        db.execute("INSERT INTO content_status(content_id,request_id,observation,next_check) VALUES (?,?,?,0)",
                   (cid, "new-status", json.dumps(observation)))
    assert read(service)[0]["lifecycle"]["state"] == "ended"


def test_discovery_preferences_invalidate_active_flags(catalogue):
    service, _, _ = catalogue
    with service.db.transaction() as db:
        cid = db.execute("SELECT id FROM contents ORDER BY id LIMIT 1").fetchone()[0]
        db.execute("UPDATE contents SET snapshot=json_set(snapshot,'$.source','games','$.competition','nhl') WHERE id=?", (cid,))
    assert read(service)[0]["active"] is True
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["preferences"]["discovery_leagues"] = []
        service.db.save_device(db, device)
    assert read(service)[0]["active"] is False


def test_concurrent_builds_coalesce_and_retained_memory_is_bounded():
    cache = CatalogueCache(max_entries=2)
    entered, release = threading.Event(), threading.Event()
    calls = []
    value = [{"content_id": "event", "start_time": NOW.isoformat(), "lifecycle": {}, "snapshot": {"title": "event"}}]
    def build(ids, now):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return copy.deepcopy(value)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(cache.read, {"event": "same"}, lambda: NOW, build)
        assert entered.wait(5)
        second = workers.submit(cache.read, {"event": "same"}, lambda: NOW, build)
        release.set()
        a, b = first.result(), second.result()
    assert calls == [1]
    a[0]["snapshot"]["title"] = "modified"
    assert b[0]["snapshot"]["title"] == "event"
    for key in range(4):
        cache.read({"event": key}, lambda: NOW, lambda ids, at: copy.deepcopy(value))
    assert len(cache.entries) == 2 and cache.bytes <= cache.max_bytes
    tiny = CatalogueCache(max_bytes=1)
    assert tiny.read({"event": "large"}, lambda: NOW, lambda ids, at: copy.deepcopy(value)) == value
    assert not tiny.entries and tiny.bytes == 0


def test_failed_build_does_not_publish_or_prevent_another_reader():
    cache = CatalogueCache()
    value = [{"content_id": "event", "start_time": NOW.isoformat(), "lifecycle": {}}]
    def fail(ids, now):
        raise RuntimeError("projection failed")
    with pytest.raises(RuntimeError, match="projection failed"):
        cache.read({"event": "key"}, lambda: NOW, fail)
    assert not cache.entries and cache.bytes == 0
    assert not cache.building
    assert cache.read({"event": "key"}, lambda: NOW, lambda ids, at: value) == value


def test_changed_account_scope_does_not_reuse_entitlements(catalogue):
    service, _, _ = catalogue
    original_executor = service.executor
    service.executor = SimpleNamespace()
    try:
        with service.db.transaction() as db:
            cid = db.execute("SELECT id FROM contents ORDER BY id LIMIT 1").fetchone()[0]
            db.execute("UPDATE contents SET snapshot=json_set(snapshot,'$.viewing_options',json(?)) WHERE id=?",
                       (json.dumps([{"id": "prime", "app": "prime_video"}]), cid))
            db.execute("INSERT INTO event_entitlements(device_id,content_id,status) VALUES (?,?,'entitled')",
                       (service.device_id, cid))
        assert read(service)[0]["entitlement"]["status"] == "entitled"
        with service.db.transaction() as db:
            service.db.set_meta(db, "entitlement_account", "another-account")
        assert read(service)[0]["entitlement"]["status"] == "unknown"
    finally:
        service.executor = original_executor


def test_repeated_status_updates_do_not_reproject_unchanged_events(catalogue, monkeypatch):
    service, _, _ = catalogue
    initial = read(service)
    cid = initial[0]["content_id"]
    original = service._project_items
    batches = []
    def scoped(*args, **kwargs):
        batches.append(kwargs["content_ids"])
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "_project_items", scoped)
    for n in range(8):
        with service.db.transaction() as db:
            db.execute("INSERT INTO content_status(content_id,request_id,next_check) VALUES (?,?,?) "
                       "ON CONFLICT(content_id) DO UPDATE SET next_check=excluded.next_check", (cid, "probe", n))
        assert len(read(service)) == len(initial)
        assert len(read(service, "second-tv")) == len(initial)
    assert batches == [{cid}] * 8


def test_stream_pin_variants_share_all_other_cached_events(catalogue, monkeypatch):
    service, _, _ = catalogue
    initial = read(service)
    b = initial[1]["content_id"]
    with service.db.transaction() as db:
        device = service.db.device(db, "second-tv")
        device["desired"] = b
        service.db.save_device(db, device)
    peer = service.runtime("second-tv")
    original = peer._project_items
    def scoped(*args, **kwargs):
        assert kwargs["content_ids"] == {b}
        return original(*args, **kwargs)
    monkeypatch.setattr(peer, "_project_items", scoped)
    second = read(service, "second-tv")
    assert next(item for item in second if item["content_id"] == b)["lifecycle"]["tracked"]
    assert len(service.catalogue_cache.entries) == len(initial) + 1


def test_only_expired_event_is_reprojected(catalogue, monkeypatch):
    service, _, clock = catalogue
    with service.db.transaction() as db:
        cid = db.execute("SELECT id FROM contents ORDER BY id LIMIT 1").fetchone()[0]
        db.execute("UPDATE contents SET seen_at=? WHERE id=?",
                   ((NOW - timedelta(seconds=service.settings.status_ttl - 10)).isoformat(), cid))
    read(service)
    original = service._project_items
    def scoped(*args, **kwargs):
        assert kwargs["content_ids"] == {cid}
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "_project_items", scoped)
    clock.current = NOW + timedelta(seconds=10)
    assert read(service)[0]["lifecycle"]["stale"]


def test_cold_build_does_not_block_cached_or_unrelated_reads():
    cache = CatalogueCache()
    def items(ids, now):
        return [{"content_id": cid, "start_time": NOW.isoformat(), "lifecycle": {}} for cid in ids]
    cache.read({"a": "a"}, lambda: NOW, items)
    entered, release = threading.Event(), threading.Event()
    def slow(ids, now):
        entered.set()
        assert release.wait(5)
        return items(ids, now)
    with ThreadPoolExecutor(max_workers=3) as workers:
        first = workers.submit(cache.read, {"b": "b"}, lambda: NOW, slow)
        try:
            assert entered.wait(2)
            cached = workers.submit(cache.read, {"a": "a"}, lambda: NOW, items)
            assert cached.result(timeout=1)[0]["content_id"] == "a"
            unrelated = workers.submit(cache.read, {"c": "c"}, lambda: NOW, items)
            assert unrelated.result(timeout=1)[0]["content_id"] == "c"
        finally:
            release.set()
        assert first.result()[0]["content_id"] == "b"


def test_overlapping_batches_publish_owned_items_before_waiting():
    cache = CatalogueCache()
    entered, other_built, release = threading.Event(), threading.Event(), threading.Event()
    def first_build(ids, now):
        assert ids == {"a", "b"}
        entered.set()
        assert release.wait(5)
        return [{"content_id": cid, "start_time": NOW.isoformat(), "lifecycle": {}} for cid in ids]
    def other_build(ids, now):
        assert ids == {"c"}
        other_built.set()
        return [{"content_id": "c", "start_time": NOW.isoformat(), "lifecycle": {}}]
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(cache.read, {"a": "a", "b": "b"}, lambda: NOW, first_build)
        try:
            assert entered.wait(2)
            second = workers.submit(cache.read, {"b": "b", "c": "c"}, lambda: NOW, other_build)
            assert other_built.wait(2)
        finally:
            release.set()
        assert [x["content_id"] for x in first.result()] == ["a", "b"]
        assert [x["content_id"] for x in second.result()] == ["b", "c"]
    assert not cache.building


def test_item_versions_upgrade_and_deletion_never_reuse_revision(catalogue):
    service, _, _ = catalogue
    with service.db.transaction() as db:
        cid = db.execute("SELECT id FROM contents ORDER BY id LIMIT 1").fetchone()[0]
        snapshot = db.execute("SELECT * FROM contents WHERE id=?", (cid,)).fetchone()
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'planning_item_%'").fetchall():
            db.execute('DROP TRIGGER "' + row[0] + '"')
        db.execute("DROP TABLE planning_item_versions")
        db.execute("PRAGMA user_version=23")
    service.db.initialize(service.db.path)
    with service.db.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == service.db.SCHEMA_VERSION
        def revision():
            return db.execute("SELECT revision FROM planning_item_versions WHERE content_id=?", (cid,)).fetchone()[0]
        assert revision() == 0
        db.execute("DELETE FROM contents WHERE id=?", (cid,))
        deleted = revision()
        assert deleted > 0
        db.execute("INSERT INTO contents VALUES (?,?,?,?,?)", tuple(snapshot))
        assert revision() > deleted
        before = revision()
        db.execute("INSERT INTO content_status(content_id,request_id) VALUES (?,?)", (cid, "probe"))
        assert revision() > before
        before = revision()
        db.execute("DELETE FROM content_status WHERE content_id=?", (cid,))
        assert revision() > before


def test_failed_shared_build_releases_all_waiters():
    cache = CatalogueCache()
    entered, other_built, release = threading.Event(), threading.Event(), threading.Event()
    def fail(ids, now):
        entered.set()
        assert release.wait(5)
        raise RuntimeError("failed source read")
    def build(ids, now):
        other_built.set()
        return [{"content_id": cid, "start_time": NOW.isoformat(), "lifecycle": {}} for cid in ids]
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(cache.read, {"a": "a"}, lambda: NOW, fail)
        try:
            assert entered.wait(2)
            second = workers.submit(cache.read, {"a": "a", "b": "b"}, lambda: NOW, build)
            assert other_built.wait(2)
        finally:
            release.set()
        for future in (first, second):
            with pytest.raises(RuntimeError, match="failed source read"):
                future.result(timeout=1)
    assert not cache.building
    assert cache.read({"a": "a"}, lambda: NOW, build)[0]["content_id"] == "a"


def test_waiting_reader_does_not_accept_peers_already_expired_view():
    cache = CatalogueCache()
    entered, other_built, release = threading.Event(), threading.Event(), threading.Event()
    expiry = NOW + timedelta(seconds=1)
    def item(cid, now):
        return {"content_id": cid, "start_time": NOW.isoformat(),
                "lifecycle": {"effective_valid_until": expiry.isoformat(),
                              "state": "live" if now < expiry else "unknown"}}
    def first_build(ids, now):
        entered.set()
        assert release.wait(5)
        return [item(cid, now) for cid in ids]
    refreshed = []
    def second_build(ids, now):
        refreshed.append(ids)
        other_built.set()
        return [item(cid, now) for cid in ids]
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(cache.read, {"a": "a"}, lambda: NOW, first_build)
        try:
            assert entered.wait(2)
            second = workers.submit(cache.read, {"a": "a", "b": "b"},
                                    lambda: NOW + timedelta(seconds=2), second_build)
            assert other_built.wait(2)
        finally:
            release.set()
        first.result()
        assert all(value["lifecycle"]["state"] == "unknown" for value in second.result())
    assert refreshed == [{"b"}, {"a"}]
