"""Interactive requests must stay bounded and independent of catalogue writers."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from uuid import uuid4

from test_manual_control import remote, take


def test_manual_authorization_reads_while_other_writer_is_busy(remote):
    client, service, _ = remote
    body = take(client)
    writer = sqlite3.connect(service.db.path, isolation_level=None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer.execute("BEGIN IMMEDIATE")
        try:
            future = pool.submit(service.manual_authorized, "living-room", body["session_id"], body["owner_token"])
            assert future.result(timeout=2)["session_id"] == body["session_id"]
        finally:
            writer.rollback()
            writer.close()


def test_ready_remote_reconnect_does_not_reserve_writer(remote, monkeypatch):
    client, service, _ = remote
    body = take(client)
    def forbidden(*args, **kwargs):
        raise AssertionError("Ready remote requested database writer")
    monkeypatch.setattr(service.db, "transaction", forbidden)
    service.prepare_manual_input("living-room", body["session_id"], body["owner_token"])
    service.manual_authorized("living-room", body["session_id"], body["owner_token"])


def test_add_prepares_only_requested_event_and_preview_only_plan(rig, monkeypatch):
    client, service, _ = rig
    original = service._items
    calls = []
    def checked(db, device_id=None, **kwargs):
        assert kwargs.get("include_snapshot") is False
        calls.append((device_id, kwargs.get("content_ids")))
        return original(db, device_id, **kwargs)
    monkeypatch.setattr(service, "_items", checked)
    body = {"command_id": str(uuid4()), "expected_revision": 0,
            "action": {"type": "add", "content_id": "demo:jays"}}
    response = client.post("/api/v1/devices/living-room/watch-plan/preview", json=body)
    assert response.status_code == 200, response.text
    assert len(calls) == 1 and calls[0][1] == {"demo:jays", "demo:canadiens"}
    calls.clear()
    response = client.post("/api/v1/devices/living-room/watch-plan", json=body)
    assert response.status_code == 200, response.text
    assert calls and all(ids == {"demo:jays"} for _, ids in calls)
    first = response.json()
    assert client.post("/api/v1/devices/living-room/watch-plan", json=body).json() == first


def test_remove_and_reorder_never_prepare_catalogue(rig, monkeypatch):
    client, service, _ = rig
    state = client.get("/api/v1/devices/living-room/state").json()
    def forbidden(*args, **kwargs):
        raise AssertionError("Plan edit decoded catalogue")
    monkeypatch.setattr(service, "_items", forbidden)
    for action in [{"type": "reorder", "ordered_entry_ids": [p["id"] for p in state["plan"]]},
                   {"type": "remove", "entry_id": state["plan"][0]["id"]}]:
        response = client.post("/api/v1/devices/living-room/watch-plan", json={
            "command_id": str(uuid4()), "expected_revision": state["revision"], "action": action})
        assert response.status_code == 200, response.text
        state = client.get("/api/v1/devices/living-room/state").json()


def test_small_reads_skip_overview_and_reference_data(rig, monkeypatch):
    client, service, _ = rig
    def forbidden(*args, **kwargs):
        raise AssertionError("Focused read invoked broad overview work")
    monkeypatch.setattr(service, "overview", forbidden)
    monkeypatch.setattr(service.db, "teams", forbidden)
    monkeypatch.setattr(service, "entitlement_queue", forbidden)
    for path in ("state", "rules", "activity", "watch-plan", "interaction-state"):
        response = client.get("/api/v1/devices/living-room/" + path)
        assert response.status_code == 200, response.text


def test_events_read_skips_teams_and_queue(rig, monkeypatch):
    from datetime import UTC, datetime
    client, service, _ = rig
    # Demo observations are intentionally timestamped on every projection.
    # Hold their clock steady when comparing the two complete responses.
    observed_at = datetime.now(UTC)
    project = service._project_items
    def fixed_projection(*args, **kwargs):
        return project(*args, **{**kwargs, "at": observed_at})
    monkeypatch.setattr(service, "_project_items", fixed_projection)
    expected = client.get("/api/v1/events").json()
    def forbidden(*args, **kwargs):
        raise AssertionError("Events read unrelated reference data")
    monkeypatch.setattr(service.db, "teams", forbidden)
    monkeypatch.setattr(service, "entitlement_queue", forbidden)
    actual = client.get("/api/v1/events").json()
    assert actual["items"] == expected["items"]


def test_add_work_does_not_scale_with_three_thousand_source_snapshots(rig, monkeypatch):
    import json
    client, service, _ = rig
    with service.db.snapshot() as db:
        entries = [json.loads(row[0]) for row in db.execute("SELECT snapshot FROM contents")]
    template = entries[0]
    entries.extend({**template, "id": f"bulk-{i}", "opaque_source": "provider-record-" * 1000}
                   for i in range(3000 - len(entries)))
    prepared = service.prepare_catalog(entries)
    with service.db.transaction() as db:
        service.replace_catalog(db, entries, "teamarr", prepared=prepared)
    seen = []
    original = service._project_items
    def project(*args, **kwargs):
        seen.append(kwargs.get("content_ids"))
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "_project_items", project)
    revision = client.get("/api/v1/devices/living-room/state").json()["revision"]
    response = client.post("/api/v1/devices/living-room/watch-plan", json={
        "command_id": str(uuid4()), "expected_revision": revision,
        "action": {"type": "add", "content_id": "demo:jays"}})
    assert response.status_code == 200, response.text
    assert seen and all(ids == {"demo:jays"} for ids in seen)
