"""Bulk planning must not repeatedly decode full provider records."""
import json
from datetime import timedelta

from controller.display import display_event, display_view
from controller.planner import choose
from test_catalogue_cache import NOW, catalogue, read
from test_entitlements import seed
from test_planning_performance import publish, snapshots
from test_prime_workflow import rig, cleanup


def compact_reads_only(service, monkeypatch):
    original = service.catalogue_cache.read
    calls = []
    def checked(*args, **kwargs):
        assert kwargs.get("include_snapshot") is False, "background bulk source decode"
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(service.catalogue_cache, "read", checked)
    return calls


def test_multi_stream_ticks_keep_complete_selected_source_without_bulk_decode(catalogue, monkeypatch):
    service, _, _ = catalogue
    entries = snapshots(service)
    for entry in entries:
        entry["source_only"] = {"opaque": entry["id"], "payload": "provider-body-" * 10000}
    publish(service, entries)
    expected = {entry["id"]: entry for entry in entries}
    calls = compact_reads_only(service, monkeypatch)
    for _ in range(4):
        for runtime in service.runtimes.values():
            assert runtime.stage_playback()
    assert calls
    with service.db.snapshot() as db:
        jobs = list(db.execute("SELECT content_id,payload FROM jobs"))
        assert jobs
        for job in jobs:
            assert json.loads(job["payload"])["content_snapshot"] == expected[job["content_id"]]
        for key in service.runtimes:
            device = service.db.device(db, key)
            items = service.runtime(key).planning_items(db)
            assert all("snapshot" not in item for item in items)
            assert choose(device, items, service.now(db), NOW) == choose(
                device, [service.with_source(db, item) for item in items], service.now(db), NOW)


def test_compact_preparation_preserves_time_boundary_revalidation(catalogue):
    service, _, clock = catalogue
    entries = snapshots(service)
    entries[0]["start_time"] = (NOW + timedelta(seconds=1)).isoformat()
    entries[0]["status"] = "scheduled"
    publish(service, entries)
    def prepare(db):
        result = service.prepare_planning(db, include_snapshot=False)
        clock.current += timedelta(seconds=2)
        return result
    with service.db.transaction(prepare=prepare, version=service.planning_version) as db:
        compact = service.planning_items(db)
        complete = service.items(db)
        assert all("snapshot" not in item for item in compact)
        assert compact == [{k: v for k, v in item.items() if k != "snapshot"} for item in complete]
        assert complete[0]["snapshot"]


def test_source_hydration_uses_readers_snapshot(catalogue):
    service, _, _ = catalogue
    with service.db.snapshot() as old:
        item = service.presentation_items(old)[0]
        original = service.with_source(old, item)
        with service.db.transaction() as writer:
            writer.execute("UPDATE contents SET snapshot=json_set(snapshot,'$.title','new title') WHERE id=?",
                           (item["content_id"],))
        assert service.with_source(old, item) == original
    with service.db.snapshot() as current:
        assert service.with_source(current, service.presentation_items(current)[0])["snapshot"]["title"] == "new title"


def test_display_keeps_team_sides_without_decoding_hidden_sources(catalogue, monkeypatch):
    service, _, _ = catalogue
    complete = read(service)
    # These fixtures have no Prime executor; explicitly provide subscription
    # presentation for the display while retaining the real catalogue cache.
    original = service.presentation_items
    def entitled(*args, **kwargs):
        result = original(*args, **kwargs)
        for item in result:
            item["entitlement"] = {"status": "entitled"}
        return result
    monkeypatch.setattr(service, "presentation_items", entitled)
    calls = compact_reads_only(service, monkeypatch)
    sources = []
    hydrate = service.with_source
    monkeypatch.setattr(service, "with_source", lambda db, item: (
        sources.append(item["content_id"]), hydrate(db, item))[1])
    result = display_view(service, service.device_id)
    assert calls and result.events
    with service.db.snapshot() as db:
        device = service.db.device(db)
    expected = {item["content_id"]: display_event(item, device, {}) for item in complete}
    assert all(event == expected[event.content_id] for event in result.events)
    assert set(sources) == {event.content_id for event in result.events}
    assert len(sources) == len(set(sources))


async def test_entitlement_ranking_hydrates_only_selected_record(tmp_path, monkeypatch):
    service, _ = rig(tmp_path)
    seed(service, count=100)
    try:
        calls = compact_reads_only(service, monkeypatch)
        selected = service.prepare_entitlement_check()
        assert calls and selected is not None
        _, item, _ = selected
        with service.db.snapshot() as db:
            assert item["snapshot"] == json.loads(db.execute(
                "SELECT snapshot FROM contents WHERE id=?", (item["content_id"],)).fetchone()[0])
    finally:
        await cleanup(service)
