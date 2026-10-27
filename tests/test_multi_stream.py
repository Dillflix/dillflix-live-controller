import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from controller.api import create_app
from controller.config import DeviceSettings, Settings


@pytest.fixture
def fleet(tmp_path):
    settings = Settings(database=str(tmp_path / "fleet.sqlite"), simulation_delay=0,
                        public_auth_mode="guest", admin_auth_mode="trusted-lan",
                        devices=(DeviceSettings("living-room", "Dillflix 1"),
                                 DeviceSettings("second-tv", "Dillflix 2")))
    app = create_app(settings, start_workers=False)
    with TestClient(app) as client:
        client.headers["origin"] = "http://testserver"
        yield client, app.state.controller, app


def read(client, device="living-room"):
    response = client.get("/api/v1/overview", params={"device_id": device})
    assert response.status_code == 200, response.text
    return response.json()


def command(client, device, action, revision=None):
    if revision is None:
        revision = read(client, device)["device"]["revision"]
    return client.post(f"/api/v1/devices/{device}/watch-plan", json={
        "command_id": str(uuid4()), "expected_revision": revision, "action": action,
    })


def tick_all(service):
    for runtime in service.runtimes.values():
        runtime.tick()


def test_display_projects_fleet_with_one_catalog_read(fleet, monkeypatch):
    client, service, _ = fleet
    tick_all(service)
    runtime = service.runtime("second-tv")
    original = runtime.items
    calls = []
    def items(db, device_id):
        calls.append(device_id)
        return original(db, device_id)
    monkeypatch.setattr(runtime, "items", items)
    data = client.get("/api/v1/devices/second-tv/display").json()
    assert calls == ["second-tv"]
    assert data["device_name"] == "Dillflix 2"
    assert [s["name"] for s in data["streams"]] == ["Dillflix 1", "Dillflix 2"]
    assert all(s["playback"]["state"] == "playing" for s in data["streams"])
    assert len({s["playback"]["event"]["content_id"] for s in data["streams"]}) == 2
    assert data["playback"] == data["streams"][1]["playback"]


@pytest.mark.parametrize("device_id,old,new", [
    ("living-room", "Living room", "Dillflix 1"),
    ("living-room", "Stream 1", "Dillflix 1"),
    ("stream-2", "Stream 2", "Dillflix 2"),
    ("custom", "Sports", "Sports"),
])
def test_legacy_names_preserve_device_bindings(device_id, old, new):
    binding = DeviceSettings(device_id, old, "/socket/player", "192.0.2.1:5555")
    assert (binding.id, binding.name, binding.prime_player_socket, binding.adb_serial) == (
        device_id, new, "/socket/player", "192.0.2.1:5555")


def test_two_streams_choose_distinct_live_events_and_remain_stable(fleet):
    client, service, app = fleet
    tick_all(service)
    first = [read(client, key)["device"]["observed"]["content_id"] for key in service.runtimes]
    assert len(set(first)) == 2
    for _ in range(3):
        tick_all(service)
    assert [read(client, key)["device"]["observed"]["content_id"] for key in service.runtimes] == first
    assert len(read(client)["streams"]) == 2
    assert app.state.screens["living-room"] is not app.state.screens["second-tv"]
    assert app.state.controls["living-room"].lock is not app.state.controls["second-tv"].lock


def test_simultaneous_ticks_reserve_before_launch(fleet):
    client, service, _ = fleet
    barrier = threading.Barrier(2)
    def tick(runtime):
        barrier.wait(timeout=5)
        runtime.tick()
    with ThreadPoolExecutor(2) as executor:
        list(executor.map(tick, service.runtimes.values()))
    current = [read(client, key)["device"]["observed"]["content_id"] for key in service.runtimes]
    assert len(set(current)) == 2


def test_shared_edit_revision_conflict_and_undo(fleet):
    client, service, _ = fleet
    old = read(client, "second-tv")["device"]["revision"]
    assert command(client, "living-room", {"type": "add", "content_id": "demo:lions"}).status_code == 200
    left, right = read(client), read(client, "second-tv")
    assert left["device"]["plan"] == right["device"]["plan"]
    assert right["device"]["revision"] > old
    assert command(client, "second-tv", {"type": "add", "content_id": "demo:jays"}, old).status_code == 409
    response = client.post("/api/v1/devices/living-room/undo", json={
        "command_id": str(uuid4()), "expected_revision": left["device"]["revision"],
        "history_id": left["undo"]["id"],
    })
    assert response.status_code == 200, response.text
    assert read(client)["device"]["plan"] == read(client, "second-tv")["device"]["plan"]
    assert "demo:lions" not in {p["content_id"] for p in read(client)["device"]["plan"]}


def test_pause_and_runtime_failure_do_not_stop_other_stream(fleet):
    client, service, _ = fleet
    tick_all(service)
    before = read(client, "second-tv")["device"]["observed"]["request_id"]
    a = read(client)["device"]
    response = client.post("/api/v1/devices/living-room/automation", json={
        "command_id": str(uuid4()), "expected_revision": a["revision"], "mode": "paused",
    })
    assert response.status_code == 200, response.text
    tick_all(service)
    b = read(client, "second-tv")["device"]
    assert b["automation"] == "active"
    assert b["observed"]["request_id"] == before
    assert service.playback_lock is not service.runtime("second-tv").playback_lock


@pytest.mark.parametrize("device_id", ["living-room", "second-tv"])
def test_shared_catalogue_does_not_rebuild_other_streams(fleet, monkeypatch, device_id):
    _, service, _ = fleet
    calls = []
    for key, runtime in service.runtimes.items():
        original = runtime.items
        def counted(db, selected=None, *, key=key, original=original):
            calls.append(selected or key)
            return original(db, selected)
        monkeypatch.setattr(runtime, "items", counted)
    with service.db.snapshot() as db:
        assert service.runtime(device_id).planning_items(db)
    assert calls == [device_id]


def test_shared_network_policy_keeps_connection_state_local(fleet):
    client, service, _ = fleet
    with service.db.transaction() as db:
        b = service.db.device(db, "second-tv")
        b["preferences"]["vpn_policy"] = {"enabled": True, "locations": []}
        b["prime_network"] = {"state": "ready", "location": "toronto"}
        service.db.save_device(db, b)
    assert command(client, "living-room", {"type": "add", "content_id": "demo:lions"}).status_code == 200
    assert read(client, "second-tv")["device"]["preferences"]["vpn_policy"] == {"enabled": True, "locations": []}
    assert read(client)["device"]["preferences"]["vpn_policy"] == {"enabled": True, "locations": []}
    assert "prime_network" not in read(client)["device"]


def test_vpn_edit_from_second_stream_shares_revision_and_undo(fleet):
    client, service, _ = fleet
    path = "/api/v1/devices/second-tv/rules"
    body = client.get(path).json()
    old_first = read(client)["device"]["revision"]
    original = body["preferences"].get("vpn_policy")
    policy = {"enabled": True, "locations": [{"target_id": "montreal", "team_cities": ["Montreal"]}]}
    body["preferences"]["vpn_policy"] = policy
    body.update(command_id=str(uuid4()), expected_revision=body.pop("revision"))
    result = client.put(path, json=body)
    assert result.status_code == 200, result.text
    assert client.put(path, json=body).json() == result.json()
    left, right = read(client), read(client, "second-tv")
    assert left["device"]["preferences"]["vpn_policy"] == right["device"]["preferences"]["vpn_policy"]
    assert left["device"]["revision"] > old_first
    response = client.post("/api/v1/devices/second-tv/undo", json={
        "command_id": str(uuid4()), "expected_revision": right["device"]["revision"],
        "history_id": right["undo"]["id"],
    })
    assert response.status_code == 200, response.text
    for device in service.runtimes:
        assert read(client, device)["device"]["preferences"].get("vpn_policy") == original


def test_legacy_vpn_policy_migrates_once_and_new_stream_inherits(tmp_path):
    from dataclasses import replace
    settings = Settings(database=str(tmp_path / "migration.sqlite"), simulation_delay=0,
                        devices=(DeviceSettings("living-room", "Dillflix 1"),
                                 DeviceSettings("second-tv", "Dillflix 2")))
    first = {"enabled": True, "locations": [{"target_id": "toronto", "team_cities": ["Toronto"]}]}
    second = {"enabled": False, "locations": []}
    app = create_app(settings, start_workers=False)
    with TestClient(app):
        service = app.state.controller
        service.db.before_commit = None
        with service.db.transaction() as db:
            for key, policy in (("living-room", first), ("second-tv", second)):
                device = service.db.device(db, key)
                device["preferences"]["vpn_policy"] = policy
                device["prime_network"] = {"local": key}
                service.db.save_device(db, device)
            canonical = service.db.meta(db, "fleet_plan")
            canonical["preferences"].pop("vpn_policy", None)
            service.db.set_meta(db, "fleet_plan", canonical)
            db.execute("DELETE FROM metadata WHERE key='shared_vpn_policy_migrated'")
    settings = replace(settings, devices=(*settings.devices, DeviceSettings("third-tv", "Stream 3")))
    for _ in range(2):
        app = create_app(settings, start_workers=False)
        with TestClient(app):
            service = app.state.controller
            with service.db.snapshot() as db:
                for key in service.runtimes:
                    assert service.db.device(db, key)["preferences"]["vpn_policy"] == first
                assert service.db.device(db, "second-tv")["prime_network"] == {"local": "second-tv"}
                assert service.db.meta(db, "legacy_device_vpn_policies") == {"living-room": first, "second-tv": second}


def test_selected_display_and_public_status_are_scoped(fleet):
    client, service, _ = fleet
    tick_all(service)
    a = client.get("/api/v1/devices/living-room/display").json()
    b = client.get("/api/v1/devices/second-tv/display").json()
    assert a["playback"]["event"]["content_id"] != b["playback"]["event"]["content_id"]
    public = client.get("/api/public/v1/overview?device_id=second-tv").json()
    assert public["device"]["id"] == "second-tv"
    assert public["shared_plan"] is True
    assert "prime_player_socket" not in str(public)
    assert "adb_serial" not in str(public)
    assert client.get("/api/v1/overview?device_id=missing").status_code == 404


def test_duplicate_bindings_are_rejected():
    with pytest.raises(ValueError, match="unique adb_serial"):
        Settings(devices=(DeviceSettings("one", "One", adb_serial="same"),
                          DeviceSettings("two", "Two", adb_serial="same")))
    with pytest.raises(ValueError, match="unique prime_player_socket"):
        Settings(devices=(DeviceSettings("one", "One", "/run/player.sock"),
                          DeviceSettings("two", "Two", "/run/player.sock")))


@pytest.mark.parametrize("old_name,expected", [("Living room", "Dillflix 1"), ("Main coverage", "Main coverage")])
def test_single_stream_upgrade_renames_only_legacy_label_without_changing_identity(tmp_path, old_name, expected):
    settings = Settings(database=str(tmp_path / "labels.sqlite"))
    app = create_app(settings, start_workers=False)
    with TestClient(app):
        service = app.state.controller
        with service.db.transaction() as db:
            device = service.db.device(db, "living-room")
            device["name"] = old_name
            service.db.save_device(db, device)
            before = device.copy()
    with TestClient(create_app(settings, start_workers=False)) as client:
        after = read(client)["device"]
        assert after["id"] == "living-room" and after["name"] == expected
        for field in ("plan", "revision", "intent_version"):
            assert after[field] == before[field]
    assert DeviceSettings("living-room", "Main coverage").name == "Main coverage"


def test_adding_second_stream_preserves_existing_plan_and_receipts(tmp_path):
    path = str(tmp_path / "upgrade.sqlite")
    app = create_app(Settings(database=path), start_workers=False)
    with TestClient(app) as client:
        assert command(client, "living-room", {"type": "add", "content_id": "demo:lions"}).status_code == 200
        original = read(client)["device"]["plan"]
    # Registration order must not make a fresh stream the missing primary row.
    settings = Settings(database=path, devices=(DeviceSettings("second-tv", "Dillflix 2"),
                                                DeviceSettings("living-room", "Dillflix 1")))
    with TestClient(create_app(settings, start_workers=False)) as client:
        assert read(client)["device"]["plan"] == original
        assert read(client, "second-tv")["device"]["plan"] == original


def test_plan_preview_counts_available_streams_without_mutating_assignments(fleet):
    from controller.fleet import planning_preview

    _, service, _ = fleet
    with service.db.transaction() as db:
        device = service.db.device(db, "living-room")
        now = service.now(db)
        device["plan"] = [service.entry(key) for key in ("a", "b", "c")]
        items = [{"content_id": key, "start_time": now.isoformat(),
                  "expected_end_time": (now + timedelta(hours=1)).isoformat(),
                  "lifecycle": {"state": "live"}} for key in ("a", "b", "c")]
        preview = planning_preview(service, db, device, items, now)
        first = [s for s in preview["segments"] if s["start"] == now.isoformat()]
        assert {s["content_id"] for s in first} == {"a", "b"}
        assert {s["device_id"] for s in first} == {"living-room", "second-tv"}
        assert preview["conflicts"] == [["a", "c"], ["b", "c"]]
        assert all("stream_assignment" not in service.db.device(db, key) for key in service.runtimes)
        second = service.db.device(db, "second-tv")
        second["automation"] = "paused"
        service.db.save_device(db, second)
        preview = planning_preview(service, db, device, items, now)
        assert {s["device_id"] for s in preview["segments"]} == {"living-room"}
        assert preview["conflicts"] == [["a", "b"], ["a", "c"]]


def test_second_stream_diagnostics_show_shared_entitlement_worker(fleet):
    from controller.prime_player.diagnostics import collect

    _, service, _ = fleet
    with service.db.transaction() as db:
        service.db.set_meta(db, "entitlement_worker", {"device_id": "living-room", "reason": "shared"})
        service.db.set_meta(db, "entitlement_worker:second-tv", {"reason": "secondary"})
        service.db.set_meta(db, "entitlement_dispatch_after", 123)
    exported = collect(service.settings.database, "second-tv")
    assert {row["device_id"] for row in exported["stream_allocations"]} == {"living-room", "second-tv"}
    assert all("preferences" not in row for row in exported["stream_allocations"])
    assert exported["entitlement_worker"]["entitlement_worker"] == {"device_id": "living-room", "reason": "shared"}
    assert exported["entitlement_worker"]["entitlement_dispatch_after"] == 123
    assert "entitlement_worker:second-tv" not in exported["entitlement_worker"]


async def test_secondary_close_failure_still_drains_primary_and_releases_leases(tmp_path):
    from controller.service import Controller

    service = Controller(Settings(database=str(tmp_path / "shutdown.sqlite"),
        devices=(DeviceSettings("living-room", "One"), DeviceSettings("second-tv", "Two"))))
    closed = []
    async def close_primary():
        closed.append("primary")
    async def close_secondary():
        closed.append("secondary")
        raise RuntimeError("close failed")
    service.executor = SimpleNamespace(close=close_primary)
    service.runtime("second-tv").executor = SimpleNamespace(close=close_secondary)
    with service.db.transaction() as db:
        for runtime in service.runtimes.values():
            assert service.db.lease(db, "device:" + runtime.device_id, runtime.owner, 1)
    await service.stop()
    assert set(closed) == {"primary", "secondary"}
    with service.db.snapshot() as db:
        assert db.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
