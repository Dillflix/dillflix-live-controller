"""HTTP and websocket dispatch must stay bound to the selected physical stream."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from controller.api import create_app
from controller.config import DeviceSettings, Settings
from controller.executor.config import ExecutorConfig
from playback_fixtures import TOKEN, HEADERS, payload


@pytest.fixture
def fleet(tmp_path):
    settings = Settings(
        database=str(tmp_path / "fleet.sqlite"), simulation_delay=0,
        public_auth_mode="guest", admin_auth_mode="trusted-lan",
        executor=ExecutorConfig(api_token=TOKEN),
        devices=(DeviceSettings("living-room", "Living room", adb_serial="first:5555"),
                 DeviceSettings("den", "Den", adb_serial="second:5555")),
    )
    app = create_app(settings, start_workers=False)
    with TestClient(app) as client:
        yield client, app


def test_selected_overviews_and_screen_paths_preserve_default(fleet):
    client, app = fleet
    assert client.get("/api/v1/overview").json()["device"]["id"] == "living-room"
    selected = client.get("/api/v1/overview?device_id=den").json()
    assert selected["device"]["name"] == "Den" and selected["shared_plan"]
    assert [item["id"] for item in client.get("/api/v1/devices").json()["items"]] == ["living-room", "den"]
    for device_id, serial in (("living-room", "first:5555"), ("den", "second:5555")):
        result = client.get(f"/api/v1/devices/{device_id}/screen").json()
        assert result["stream_path"] == f"/api/v1/devices/{device_id}/screen/stream"
        assert app.state.screens[device_id].settings.screen_adb_serial == serial
        assert app.state.controls[device_id].device_id == device_id
        assert client.get(f"/api/v1/devices/{device_id}/display").json()["device_id"] == device_id
    assert app.state.screen is app.state.screens["living-room"]
    assert app.state.control is app.state.controls["living-room"]
    assert app.state.controls["den"].lock is not app.state.control.lock


def test_health_detects_failed_worker_on_second_stream(fleet, monkeypatch):
    client, app = fleet
    root = app.state.controller
    monkeypatch.setattr(root, "background_status", lambda: {"playback": {"state": "running"}})
    monkeypatch.setattr(root.runtime("den"), "background_status", lambda: {"playback": {"state": "retrying"}})
    health = client.get("/api/health").json()
    assert health["ok"] is False
    assert health["background_workers"]["playback"]["state"] == "running"
    assert health["devices"]["den"]["background_workers"]["playback"]["state"] == "retrying"
    assert client.get("/healthz").json() == {"ok": True}  # Process liveness is deliberately separate.


def test_legacy_registry_label_is_stream_one_on_admin_and_public_surfaces(fleet):
    client, _ = fleet
    for path in ("/api/v1/overview", "/api/public/v1/overview"):
        view = client.get(path).json()
        assert view["device"]["id"] == "living-room"
        assert view["device"]["name"] == "Dillflix 1"
        assert next(stream for stream in view["streams"] if stream["id"] == "living-room")["name"] == "Dillflix 1"


def test_selection_preview_respects_other_stream_claim_without_mutation(fleet):
    client, app = fleet
    root = app.state.controller
    headers = {"origin": "http://testserver"}
    initial = client.post("/api/v1/devices/den/simulate", headers=headers).json()["decision"]["content_id"]
    assert initial
    with root.db.transaction() as db:
        first = root.db.device(db, "living-room")
        first["observed"] = {"content_id": initial}
        first["playback_state"] = "verified"
        root.db.save_device(db, first)
    with root.db.snapshot() as db:
        before = [root.db.device(db, key) for key in root.runtimes]
    result = client.post("/api/v1/devices/den/simulate", headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["decision"]["content_id"] != initial
    with root.db.snapshot() as db:
        assert [root.db.device(db, key) for key in root.runtimes] == before


@pytest.mark.parametrize("path", ["/api/v1/overview?device_id=missing", "/api/v1/events?device_id=missing",
    "/api/v1/devices/missing/screen", "/api/v1/devices/missing/display", "/api/v1/devices/missing/diagnostics",
    "/api/public/v1/overview?device_id=missing"])
def test_unknown_device_never_falls_back_to_default(fleet, path):
    client, _ = fleet
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("suffix", ["screen/stream", "control/input"])
def test_unknown_device_websocket_never_attaches(fleet, suffix):
    client, _ = fleet
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(f"/api/v1/devices/missing/{suffix}", headers={"origin": "http://testserver"}):
            pytest.fail("Unknown device socket accepted")
    assert error.value.code == 1008


def test_public_streams_are_allowlisted_and_commands_use_selected_revision(fleet):
    client, app = fleet
    root = app.state.controller
    with root.db.transaction() as db:
        device = root.db.device(db, "den")
        device["public_access"] = {"play_now": True, "add_to_plan": True}
        root.db.save_device(db, device)
    body = client.get("/api/public/v1/overview?device_id=den").json()
    assert body["device"] == {"id": "den", "name": "Den"}
    assert body["shared_plan"]
    allowed = {"id", "name", "playback_state", "automation", "status", "verified", "current_event", "target_event"}
    assert all(set(stream) <= allowed for stream in body["streams"])
    assert "second:5555" not in json.dumps(body)
    response = client.post("/api/public/v1/watch-plan?device_id=den", headers={"origin": "http://testserver"}, json={
        "command_id": uuid4().hex, "expected_revision": body["revision"], "content_id": "demo:lions", "action": "add",
    })
    assert response.status_code == 200, response.text
    with root.db.snapshot() as db:
        plans = [root.db.device(db, key)["plan"] for key in root.runtimes]
    assert plans[0] == plans[1]
    entry = next(item for item in plans[1] if item["content_id"] == "demo:lions")
    assert entry["actor"]["type"] == "user"


def test_two_manual_remotes_deliver_only_to_their_own_devices(fleet):
    client, app = fleet

    class Input:
        def __init__(self):
            self.packets = []

        async def __aenter__(self):
            return self

        async def send(self, packet):
            self.packets.append(packet)

        async def close(self):
            pass

    sources, credentials = {}, {}
    for device_id in app.state.controls:
        source = sources[device_id] = Input()
        app.state.controls[device_id].source_factory = lambda _, source=source: source
        credentials[device_id] = {"session_id": uuid4().hex, "owner_token": uuid4().hex}
        revision = client.get(f"/api/v1/devices/{device_id}/state").json()["revision"]
        response = client.post(f"/api/v1/devices/{device_id}/control", headers={"origin": "http://testserver"}, json={
            **credentials[device_id], "command_id": uuid4().hex, "expected_revision": revision,
            "action": "take", "minutes": 15, "takeover": False, "release_mode": "active",
        })
        assert response.status_code == 200, response.text
    # A valid owner token for one stream is not authority over another stream.
    with client.websocket_connect("/api/v1/devices/den/control/input", headers={"origin": "http://testserver"}) as wrong:
        wrong.send_json(credentials["living-room"])
        assert wrong.receive_json()["type"] == "error"
    assert not any(source.packets for source in sources.values())
    with client.websocket_connect("/api/v1/devices/living-room/control/input", headers={"origin": "http://testserver"}) as first:
        first.send_json(credentials["living-room"])
        assert first.receive_json()["type"] == "ready"
        with client.websocket_connect("/api/v1/devices/den/control/input", headers={"origin": "http://testserver"}) as second:
            second.send_json(credentials["den"])
            assert second.receive_json()["type"] == "ready"
            second.send_json({"seq": 1, "key": "right"})
            assert second.receive_json()["type"] == "sent"
            assert len(sources["living-room"].packets) == 1  # Its own wake only.
            assert len(sources["den"].packets) == 2
        first.send_json({"seq": 1, "key": "left"})
        assert first.receive_json()["type"] == "sent"
        assert len(sources["living-room"].packets) == 2


def test_public_viewer_cannot_access_either_stream_admin_routes(tmp_path):
    settings = Settings(database=str(tmp_path / "auth.sqlite"), proxy_secret="test-proxy-secret",
                        devices=(DeviceSettings("living-room", "Living room"), DeviceSettings("den", "Den")))
    app = create_app(settings, start_workers=False)
    headers = {"x-dillflix-proxy-key": "test-proxy-secret", "x-dillflix-user": "viewer", "x-dillflix-role": "user",
               "origin": "http://testserver"}
    with TestClient(app) as client:
        assert client.get("/api/v1/devices", headers=headers).status_code == 403
        for device_id in ("living-room", "den"):
            assert client.get(f"/api/public/v1/overview?device_id={device_id}", headers=headers).status_code == 200
            assert client.get(f"/api/v1/overview?device_id={device_id}", headers=headers).status_code == 403
            assert client.get(f"/api/v1/devices/{device_id}/diagnostics", headers=headers).status_code == 403
            assert client.post(f"/api/v1/devices/{device_id}/control", headers=headers, json={}).status_code == 403
            for suffix in ("screen/stream", "control/input"):
                with pytest.raises(WebSocketDisconnect) as error:
                    with client.websocket_connect(f"/api/v1/devices/{device_id}/{suffix}", headers=headers):
                        pytest.fail("Public viewer opened an administrator socket")
                assert error.value.code == 1008


def test_vpn_diagnostics_and_executor_route_to_bound_runtime(fleet):
    client, app = fleet
    root = app.state.controller
    calls = []

    class Player:
        def __init__(self, device_id):
            self.device_id = device_id

        async def vpn_locations(self):
            calls.append((self.device_id, "vpn"))
            return {"locations": [{"id": self.device_id}]}

        async def rpc(self, method, **kwargs):
            calls.append((self.device_id, method))
            return {"device": self.device_id}

    class Executor:
        worker = None
        database_status = None

        def __init__(self, device_id):
            self.device_id = device_id
            self.player = Player(device_id)
            self.model_captures = SimpleNamespace(status=lambda: {})
            self.store = SimpleNamespace(report=self.report)

        def submit(self, command):
            calls.append((self.device_id, "submit"))
            return {"token": "test-den-token", "device_id": self.device_id}, True

        def report(self, token):
            calls.append((self.device_id, "report"))
            return {"token": token, "device_id": self.device_id}

        async def cancel(self, command):
            calls.append((self.device_id, "cancel"))
            return {"device_id": self.device_id, "input_quiescent": True}

    for runtime in root.runtimes.values():
        runtime.executor = Executor(runtime.device_id)
    try:
        assert client.get("/api/v1/devices/den/vpn-locations").json()["locations"] == [{"id": "den"}]
        diagnostics = client.get("/api/v1/devices/den/diagnostics").json()
        assert diagnostics["prime_player"]["health"] == {"device": "den"}
        request = {**payload(), "device_id": "den"}
        response = client.post("/v1/playbacks", headers={**HEADERS, "origin": "http://testserver"}, json=request)
        assert response.status_code == 202, response.text
        with root.db.transaction() as db:
            db.execute("INSERT INTO executor_jobs(token,request_id,device_id,intent,content_id,request_hash,report,state) "
                       "VALUES (?,?,?,?,?,?,?,?)", ("test-den-token", uuid4().hex, "den", 1000, "fixture-game", "hash", "{}", "pending"))
        assert client.get("/v1/playbacks/test-den-token", headers=HEADERS).json()["device_id"] == "den"
        assert client.post("/v1/playbacks/cancel", headers={**HEADERS, "origin": "http://testserver"},
                           json={"device_id": "den", "token": "test-den-token"}).json()["device_id"] == "den"
        assert calls and all(device == "den" for device, _ in calls)
    finally:
        for runtime in root.runtimes.values():
            runtime.executor = None
