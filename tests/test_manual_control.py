import asyncio
import json
import struct
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from controller.api import create_app
from controller.config import Settings
from controller.device_input import AdbInput, DeviceInput, Input
from controller.models import ManualControlCommand
from controller.service import Controller

PATH = "/api/v1/devices/living-room/control"
ORIGIN = {"origin": "http://testserver"}
WAKE = struct.pack(">BBiiiBBiii", 0, 0, 224, 0, 0, 0, 1, 224, 0, 0)


class FakeInput:
    def __init__(self):
        self.packets = []
        self.started = 0
        self.stopped = 0
        self.fail = False

    async def __aenter__(self):
        self.started += 1
        return self

    async def send(self, packet):
        self.packets.append(packet)
        if self.fail:
            raise OSError("Uncertain write")

    async def close(self):
        self.stopped += 1


@pytest.fixture
def remote(tmp_path):
    app = create_app(
        Settings(database=str(tmp_path / "control.sqlite"), screen_adb_serial="fixture", simulation_delay=0),
        start_workers=False,
    )
    source = FakeInput()
    app.state.control.source_factory = lambda _: source
    with TestClient(app) as client:
        yield client, app.state.controller, source


def state(client):
    return client.get("/api/v1/devices/living-room/state").json()


def command(client, **kwargs):
    return {
        "command_id": str(uuid.uuid4()),
        "expected_revision": state(client)["revision"],
        "action": "take",
        "session_id": str(uuid.uuid4()),
        "owner_token": uuid.uuid4().hex,
        **kwargs,
    }


def take(client, **kwargs):
    body = command(client, **kwargs)
    result = client.post(PATH, json=body)
    assert result.status_code == 200, result.text
    return body


def attach(ws, body):
    ws.send_json({key: body[key] for key in ("session_id", "owner_token")})
    assert ws.receive_json() == {"type": "ready"}


def test_remote_wakes_before_ready_and_again_on_reconnect(remote):
    client, _, source = remote
    body = take(client)
    for count in (1, 2):
        with client.websocket_connect(PATH + "/input", headers=ORIGIN) as ws:
            attach(ws, body)
            assert source.packets == [WAKE] * count


def test_failed_wake_does_not_advertise_ready_or_replay(remote):
    client, _, source = remote
    body = take(client)
    source.fail = True
    with client.websocket_connect(PATH + "/input", headers=ORIGIN) as ws:
        ws.send_json({key: body[key] for key in ("session_id", "owner_token")})
        assert ws.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert source.packets == [WAKE] and source.stopped == 1
    assert state(client)["manual_control"] is not None
    assert state(client)["automation"] == "paused"


@pytest.mark.parametrize("invalidated", ["expired", "handoff"])
def test_wake_rechecks_authority_after_adb_startup(remote, monkeypatch, invalidated):
    client, service, source = remote
    body = take(client)

    async def invalidate_on_start(self):
        self.started += 1
        with service.db.transaction() as db:
            device = service.db.device(db)
            if invalidated == "expired":
                device["manual_control"]["expires_at"] = (
                    datetime.now(UTC) - timedelta(seconds=1)
                ).isoformat()
            else:
                device["manual_control"]["input_ready"] = False
            service.db.save_device(db, device)
        return self

    monkeypatch.setattr(FakeInput, "__aenter__", invalidate_on_start)
    with client.websocket_connect(PATH + "/input", headers=ORIGIN) as ws:
        ws.send_json({key: body[key] for key in ("session_id", "owner_token")})
        assert ws.receive_json()["type"] == "error"
    assert source.packets == [] and source.stopped == 1


def test_take_fences_playback_keeps_plan_and_uses_real_four_hour_deadline(remote):
    client, service, source = remote
    service.tick()
    before = state(client)
    assert before["observed"]["verified"]
    body = take(client, minutes=240)
    current = state(client)
    assert current["automation"] == "paused" and current["plan"] == before["plan"]
    assert current["observed"] is None and current["desired"] is None
    assert current["intent_version"] > before["intent_version"]
    assert current["manual_control"]["return_mode"] == "active"
    assert (
        14398
        < (
            datetime.fromisoformat(current["manual_control"]["expires_at"]) - datetime.now(UTC)
        ).total_seconds()
        <= 14400
    )
    assert body["owner_token"] not in json.dumps(client.get("/api/v1/overview").json())
    service.tick()  # An old simulator observation must not restore verification.
    assert state(client)["observed"] is None
    assert source.started == 0  # Acquiring ownership alone never contacts ADB.
    assert client.post(PATH, json=body).status_code == 200
    assert state(client)["manual_control"] == current["manual_control"]
    assert client.post(PATH, json={**body, "minutes": 60}).status_code == 409
    assert (
        client.post(
            "/api/v1/devices/living-room/automation",
            json={"command_id": "resume", "expected_revision": current["revision"], "mode": "active"},
        ).status_code
        == 409
    )


def test_take_cancels_pending_and_rejects_late_result(remote):
    client, service, _ = remote
    service.stage_playback()
    with service.db.transaction() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE state='pending'").fetchone())
    report = service.playback.submit(json.loads(job["payload"]))
    take(client)
    assert not service.receive_playback_report(job["id"], report)
    service.tick()
    with service.db.transaction() as db:
        assert db.execute("SELECT state,cancel_sent FROM jobs WHERE id=?", (job["id"],)).fetchone()[:] == (
            "cancelled",
            1,
        )


def test_input_serialized_no_replay_and_no_text_storage(remote):
    client, service, source = remote
    body = take(client)
    with client.websocket_connect(PATH + "/input", headers=ORIGIN) as ws:
        attach(ws, body)
        ws.send_json({"seq": 1, "key": "up"})
        assert ws.receive_json() == {"type": "sent", "seq": 1}
        ws.send_json({"seq": 2, "text": "private search"})
        assert ws.receive_json()["seq"] == 2
        ws.send_json({"seq": 2, "text": "private search"})
        assert ws.receive_json()["type"] == "error"
    assert len(source.packets) == 3
    assert source.packets[0] == WAKE
    assert source.packets[1] == struct.pack(">BBiiiBBiii", 0, 0, 19, 0, 0, 0, 1, 19, 0, 0)
    assert source.stopped == 1
    with service.db.transaction() as db:
        assert "private search" not in "\n".join(db.iterdump())
    assert state(client)["manual_control"] is not None  # Disconnect doesn't resume automation.


def test_second_browser_explicit_takeover_revokes_old_socket_and_credentials(remote):
    client, _, source = remote
    first = take(client)
    with client.websocket_connect(PATH + "/input", headers=ORIGIN) as old:
        attach(old, first)
        assert client.post(PATH, json=command(client)).status_code == 409
        second = take(client, takeover=True, minutes=240)
        with pytest.raises(WebSocketDisconnect):
            old.receive_json()
        assert source.stopped == 1
        with client.websocket_connect(PATH + "/input", headers=ORIGIN) as ws:
            ws.send_json({key: first[key] for key in ("session_id", "owner_token")})
            assert ws.receive_json()["type"] == "error"
        assert (
            client.post(
                PATH,
                json={
                    **command(client),
                    "action": "release",
                    "session_id": first["session_id"],
                    "owner_token": first["owner_token"],
                },
            ).status_code
            == 409
        )
        with client.websocket_connect(PATH + "/input", headers=ORIGIN) as ws:
            attach(ws, second)
            # Retrying the old successful command must not reclaim ownership.
            assert client.post(PATH, json=first).status_code == 200
            ws.send_json({"seq": 1, "key": "select"})
            assert ws.receive_json()["type"] == "sent"
    assert state(client)["manual_control"]["session_id"] == second["session_id"]


@pytest.mark.parametrize("mode", ["active", "paused"])
def test_release_then_reevaluate_without_restoring_old_playback(remote, mode):
    client, service, source = remote
    service.tick()
    body = take(client)
    with client.websocket_connect(PATH + "/input", headers=ORIGIN) as ws:
        attach(ws, body)
        result = client.post(
            PATH,
            json={
                **body,
                "command_id": "end",
                "expected_revision": state(client)["revision"],
                "action": "release",
                "release_mode": mode,
            },
        )
        assert result.status_code == 200
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert source.stopped == 1
    current = state(client)
    assert current["manual_control"] is None and current["automation"] == mode
    assert current["observed"] is None
    service.tick()
    assert bool(state(client)["observed"]) == (mode == "active")


def test_extend_conflicts_secret_validation_and_input_failure(remote):
    client, _, source = remote
    body = take(client, minutes=5)
    extend = {
        **body,
        "command_id": "extend",
        "expected_revision": state(client)["revision"],
        "action": "extend",
        "minutes": 240,
    }
    assert client.post(PATH, json={**extend, "owner_token": "x" * 32}).status_code == 409
    assert client.post(PATH, json=extend).status_code == 200
    deadline = state(client)["manual_control"]["expires_at"]
    assert client.post(PATH, json=extend).status_code == 200
    assert state(client)["manual_control"]["expires_at"] == deadline
    assert client.post(PATH, json={**extend, "command_id": "stale"}).status_code == 409
    with client.websocket_connect(PATH + "/input", headers=ORIGIN) as ws:
        attach(ws, body)
        source.fail = True
        ws.send_json({"seq": 1, "key": "home"})
        assert ws.receive_json()["type"] == "error"
    assert len(source.packets) == 2 and state(client)["automation"] == "paused"


@pytest.mark.parametrize(
    "packet",
    [
        {"seq": 1, "shell": "reboot"},
        {"seq": 1, "key": "power"},
        {"seq": 1, "text": "é"},
        {"seq": 1, "text": "x" * 201},
        {"seq": 1, "key": "home", "text": "x"},
        {"seq": True, "key": "up"},
    ],
)
def test_invalid_input_never_reaches_device(remote, packet):
    client, _, source = remote
    body = take(client)
    with client.websocket_connect(PATH + "/input", headers=ORIGIN) as ws:
        attach(ws, body)
        ws.send_json(packet)
        assert ws.receive_json()["type"] == "error"
    assert source.packets == [WAKE]


def test_origin_and_session_token_required(remote):
    client, _, source = remote
    body = take(client)
    assert (
        client.post(
            PATH, json=command(client, takeover=True), headers={"origin": "https://evil.example"}
        ).status_code
        == 403
    )
    for headers in ({}, {"origin": "https://evil.example"}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(PATH + "/input", headers=headers):
                pytest.fail("Foreign socket accepted")
    with client.websocket_connect(PATH + "/input", headers=ORIGIN) as ws:
        ws.send_json({"session_id": body["session_id"], "owner_token": "x" * 32})
        assert ws.receive_json()["type"] == "error"
    assert source.started == 0


def test_one_input_socket_per_owner_and_rate_bound(remote):
    client, _, source = remote
    body = take(client)
    with client.websocket_connect(PATH + "/input", headers=ORIGIN) as ws:
        attach(ws, body)
        with client.websocket_connect(PATH + "/input", headers=ORIGIN) as other:
            other.send_json({key: body[key] for key in ("session_id", "owner_token")})
            assert other.receive_json()["type"] == "error"
        for i in range(1, 50):
            ws.send_json({"seq": i, "key": "down"})
            if ws.receive_json()["type"] == "error":
                break
        else:
            pytest.fail("Unbounded input rate")
    assert len(source.packets) < 50


@pytest.mark.parametrize("previous", ["active", "paused"])
async def test_restart_retains_deadline_and_expiry_restores_previous_mode(tmp_path, previous):
    settings = Settings(database=str(tmp_path / "restart.sqlite"), screen_adb_serial="fixture")
    service = Controller(settings)
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["automation"] = previous
        service.db.save_device(db, device)
    body = ManualControlCommand(
        command_id="start",
        expected_revision=0,
        action="take",
        session_id=uuid.uuid4().hex,
        owner_token=uuid.uuid4().hex,
        minutes=240,
    )
    service.manual_command("living-room", body)
    before = service.overview()["device"]
    await service.stop()
    service = Controller(settings)
    manager = DeviceInput(service)
    try:
        assert service.overview()["device"]["manual_control"] == before["manual_control"]
        assert service.overview()["device"]["automation"] == "paused"
        # No browser connection is needed for expiry; demo time is irrelevant.
        with service.db.transaction() as db:
            d = service.db.device(db)
            d["manual_control"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            service.db.save_device(db, d)
        await manager.expire()
        after = service.overview()["device"]
        assert after["manual_control"] is None
        assert after["automation"] == previous and after["plan"] == before["plan"]
        assert after["revision"] == before["revision"] + 1
    finally:
        await manager.stop()
        await service.stop()


async def test_pinned_scrcpy_control_socket_binary_transport_and_cleanup(fake_adb, monkeypatch):
    monkeypatch.setattr("controller.screen_capture.verified", lambda _: True)
    async with AdbInput(fake_adb) as source:
        packet = Input(seq=1, key="select").encode() + Input(seq=2, text="NFL").encode()
        await source.send(packet)
        async with asyncio.timeout(2):
            target = fake_adb.screen_server_path.parent / "input.bin"
            while not target.exists() or target.read_bytes() != packet:
                await asyncio.sleep(0.01)
    calls = [
        json.loads(line)
        for line in (fake_adb.screen_server_path.parent / "adb-calls.jsonl").read_text().splitlines()
    ]
    options = next(c[-1] for c in calls if c[-1].startswith("echo $$"))
    assert "video=false" in options and "control=true" in options and "clipboard_autosync=false" in options
    assert any("--remove" in c for c in calls)
    assert source.process.returncode is not None


def test_duration_bounds_and_disabled_control(rig):
    client, _, _ = rig
    assert client.post(PATH, json=command(client)).status_code == 409
    for minutes in (0, 1441):
        assert client.post(PATH, json=command(client, minutes=minutes)).status_code == 422


def test_play_now_cannot_bypass_manual_ownership_but_plan_edits_continue(remote):
    client, service, _ = remote
    take(client)
    revision = state(client)["revision"]
    body = {
        "command_id": "play-now",
        "expected_revision": revision,
        "action": {"type": "play_now", "content_id": "demo:redzone"},
    }
    assert client.post("/api/v1/devices/living-room/watch-plan", json=body).status_code == 409
    body["action"]["type"] = "add"
    assert client.post("/api/v1/devices/living-room/watch-plan", json=body).status_code == 200
    service.tick()
    assert state(client)["automation"] == "paused" and state(client)["observed"] is None
    assert any(p["content_id"] == "demo:redzone" for p in state(client)["plan"])


def test_active_socket_is_revoked_at_deadline(remote):
    client, service, source = remote
    body = take(client)
    with client.websocket_connect(PATH + "/input", headers=ORIGIN) as ws:
        attach(ws, body)
        with service.db.transaction() as db:
            d = service.db.device(db)
            d["manual_control"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            service.db.save_device(db, d)
        with pytest.raises(WebSocketDisconnect):
            while True:
                ws.receive_json()
    assert source.stopped == 1 and source.packets == [WAKE]
    assert state(client)["manual_control"] is None


async def test_database_restore_clears_manual_owner_and_cannot_resume_at_old_deadline(remote, tmp_path):
    from controller import ops

    client, service, _ = remote
    take(client, minutes=240)
    backup, target = tmp_path / "backup.sqlite", tmp_path / "restored.sqlite"
    ops.backup(service.settings.database, backup)
    ops.restore(backup, target)
    restored = Controller(replace(service.settings, database=str(target)))
    try:
        d = restored.overview()["device"]
        assert d["manual_control"] is None and d["automation"] == "paused"
        with restored.db.transaction() as db:
            assert restored.db.meta(db, restored.owner_key(d["id"])) is None
        assert not restored.expire_manual()
    finally:
        await restored.stop()


async def test_real_http_and_websocket_input_then_release(tmp_path):
    import socket

    import httpx
    import uvicorn
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    source = FakeInput()
    app = create_app(
        Settings(database=str(tmp_path / "http.sqlite"), screen_adb_serial="fixture"), start_workers=False
    )
    app.state.control.source_factory = lambda _: source
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    base = f"http://127.0.0.1:{port}"
    try:
        async with asyncio.timeout(8):
            while not server.started:
                await asyncio.sleep(0.01)
            async with httpx.AsyncClient(base_url=base, trust_env=False) as client:
                body = dict(
                    command_id="take",
                    expected_revision=0,
                    action="take",
                    session_id=uuid.uuid4().hex,
                    owner_token=uuid.uuid4().hex,
                    minutes=240,
                )
                result = await client.post(PATH, json=body, headers={"origin": base})
                assert result.status_code == 200
                async with connect(f"ws://127.0.0.1:{port}{PATH}/input", origin=base, proxy=None) as ws:
                    await ws.send(json.dumps({k: body[k] for k in ("session_id", "owner_token")}))
                    assert json.loads(await ws.recv())["type"] == "ready"
                    await ws.send(json.dumps({"seq": 1, "key": "back"}))
                    assert json.loads(await ws.recv()) == {"type": "sent", "seq": 1}
                    result = await client.post(
                        PATH,
                        json={**body, "command_id": "release", "expected_revision": 1, "action": "release"},
                        headers={"origin": base},
                    )
                    assert result.status_code == 200
                    with pytest.raises(ConnectionClosed):
                        await ws.recv()
                assert source.stopped == 1 and source.packets == [WAKE, Input(seq=1, key="back").encode()]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)


async def test_disconnect_during_adb_startup_cleans_up_without_waiting_for_timeout(tmp_path):
    from fastapi import WebSocket
    from starlette.websockets import WebSocketState

    service = Controller(Settings(database=str(tmp_path / "startup.sqlite"), screen_adb_serial="fixture"))
    manager = DeviceInput(service)
    source = FakeInput()
    started, disconnected = asyncio.Event(), asyncio.Event()

    async def slow_enter():
        source.started += 1
        started.set()
        await asyncio.Event().wait()

    # Special methods are resolved on the type, so use a dedicated test source.
    class SlowInput(FakeInput):
        async def __aenter__(self):
            return await slow_enter()

        async def close(self):
            source.stopped += 1

    manager.source_factory = lambda _: SlowInput()

    async def receive():
        await disconnected.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(_):
        pass

    websocket = WebSocket({"type": "websocket", "path": "/", "headers": []}, receive, send)
    websocket.client_state = WebSocketState.CONNECTED
    from controller.device_input import Attach

    attached = Attach(session_id="s" * 32, owner_token="t" * 32)
    task = asyncio.create_task(manager.run(websocket, "living-room", attached))
    try:
        await asyncio.wait_for(started.wait(), 1)
        disconnected.set()
        await asyncio.wait_for(task, 1)
        assert source.started == source.stopped == 1
        assert source.packets == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.stop()
