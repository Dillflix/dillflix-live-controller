import asyncio
import json
import socket
import struct
from dataclasses import replace

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect

from controller.api import create_app
from controller.config import Settings
from controller.screen import MAX_QUEUE_BYTES, ScreenStream, Viewer
from controller.screen_capture import CONFIG, KEY_FRAME, MAX_PACKET, AdbCapture, CaptureError, read_packet

PATH = "/api/v1/devices/living-room/screen"
META = {"type": "stream", "protocol": 1, "codec": "h264", "width": 320, "height": 180, "max_fps": 10}


def packet(flags=0, data=b"frame"):
    return struct.pack(">QI", flags, len(data)) + data


class FakeCapture:
    metadata = META

    def __init__(self):
        self.queue = asyncio.Queue()
        self.started = 0
        self.stopped = 0

    async def __aenter__(self):
        self.started += 1
        return self

    async def __aexit__(self, *_):
        self.stopped += 1

    async def packet(self):
        value = await self.queue.get()
        if isinstance(value, Exception):
            raise value
        return value


async def get(viewer):
    return await asyncio.wait_for(viewer.get(), 1)


def test_disabled_screen_has_no_device_effects(rig):
    client, service, _ = rig
    before = service.overview()["device"]
    data = client.get(PATH).json()
    assert data["state"] == "disabled"
    assert data["read_only"] and not data["audio"]
    with client.websocket_connect(PATH + "/stream", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json()["type"] == "error"
    assert client.get("/api/v1/devices/unknown/screen").status_code == 404
    assert service.overview()["device"] == before


@pytest.mark.parametrize("origin", [None, "https://evil.example", "null", "file://testserver"])
def test_foreign_or_absent_origin_cannot_start_capture(rig, origin):
    client, _, _ = rig
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(PATH + "/stream", headers={"origin": origin} if origin else {}):
            pytest.fail("Untrusted socket was accepted")


async def test_one_capture_for_multiple_viewers_and_keyframe_join():
    source = FakeCapture()
    stream = ScreenStream(Settings(screen_adb_serial="tv:5555"), lambda _: source)
    first = await stream.subscribe()
    assert await get(first) == META
    source.queue.put_nowait(packet(CONFIG, b"sps-pps"))
    source.queue.put_nowait(packet(1))  # Cannot decode a delta frame before a keyframe.
    source.queue.put_nowait(packet(KEY_FRAME | 2))
    assert await get(first) == packet(CONFIG, b"sps-pps")
    assert await get(first) == packet(KEY_FRAME | 2)
    second = await stream.subscribe()
    assert await get(second) == META
    source.queue.put_nowait(packet(3))
    assert await get(first) == packet(3)
    assert second.queue.empty()
    source.queue.put_nowait(packet(KEY_FRAME | 4))
    assert await get(second) == packet(CONFIG, b"sps-pps")
    assert await get(second) == packet(KEY_FRAME | 4)
    assert source.started == 1
    await stream.unsubscribe(first)
    assert source.stopped == 0
    await stream.unsubscribe(second)
    assert source.stopped == 1
    assert stream.status()["state"] == "idle"


async def test_resolution_change_reinitializes_decoder_before_new_keyframe():
    source = FakeCapture()
    stream = ScreenStream(Settings(screen_adb_serial="tv"), lambda _: source)
    viewer = await stream.subscribe()
    await get(viewer)
    for item in [
        packet(CONFIG, b"old"),
        packet(KEY_FRAME | 1),
        packet(CONFIG, b"new"),
        packet(2),
        packet(KEY_FRAME | 3),
    ]:
        source.queue.put_nowait(item)
    assert await get(viewer) == packet(CONFIG, b"old")
    assert await get(viewer) == packet(KEY_FRAME | 1)
    assert await get(viewer) == META
    assert await get(viewer) == packet(CONFIG, b"new")
    assert await get(viewer) == packet(KEY_FRAME | 3)
    await stream.stop()


async def test_failed_capture_cleans_up_and_can_reconnect():
    source = FakeCapture()
    stream = ScreenStream(Settings(screen_adb_serial="tv"), lambda _: source)
    viewer = await stream.subscribe()
    await get(viewer)
    source.queue.put_nowait(CaptureError("Authorize ADB on the TV."))
    assert (await get(viewer))["message"] == "Authorize ADB on the TV."
    assert stream.status()["state"] == "error"
    await stream.unsubscribe(viewer)
    again = await stream.subscribe()
    assert await get(again) == META
    await stream.unsubscribe(again)
    assert source.started == source.stopped == 2


def test_slow_viewer_is_disconnected_without_unbounded_buffering():
    viewer = Viewer()
    for _ in range(10):
        viewer.put(b"x" * (MAX_QUEUE_BYTES // 3))
    assert viewer.failed and viewer.bytes == 0
    assert viewer.queue.qsize() == 1
    assert viewer.queue.get_nowait()["type"] == "error"


async def test_viewer_limit():
    source = FakeCapture()
    stream = ScreenStream(Settings(screen_adb_serial="tv"), lambda _: source)
    for _ in range(8):
        await stream.subscribe()
    with pytest.raises(CaptureError, match="eight"):
        await stream.subscribe()
    await stream.stop()


@pytest.mark.parametrize("size,flags", [(0, 0), (MAX_PACKET + 1, 0), (65537, CONFIG)])
async def test_packet_size_is_bounded_before_body_is_read(size, flags):
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack(">QI", flags, size))
    with pytest.raises(CaptureError):
        await read_packet(reader)


async def test_fragmented_packet_retains_timestamp_flags_and_payload():
    reader = asyncio.StreamReader()
    expected = packet(KEY_FRAME | 987654321, b"h264")
    task = asyncio.create_task(read_packet(reader))
    for part in (expected[:3], expected[3:13], expected[13:]):
        reader.feed_data(part)
        await asyncio.sleep(0)
    assert await task == expected


async def test_adb_capture_launch_transport_and_owned_cleanup(fake_adb, monkeypatch):
    monkeypatch.setattr("controller.screen_capture.verified", lambda _: True)
    async with AdbCapture(fake_adb) as capture:
        assert capture.metadata["device_name"] == "Fixture TV"
        assert (await capture.packet())[0] & 0x80
        assert (await capture.packet())[0] & 0x40
    await capture.close()  # A second cleanup must not remove a subsequently reused port.
    calls = [
        json.loads(line)
        for line in (fake_adb.screen_server_path.parent / "adb-calls.jsonl").read_text().splitlines()
    ]
    command = next(c[-1] for c in calls if c[-1].startswith("echo $$"))
    assert "control=false" in command and "audio=false" in command and "power_on=false" in command
    assert "forward" in calls[3] and "tcp:0" in calls[3]
    assert sum("--remove" in call for call in calls) == 1
    assert all("kill-server" not in call and "disconnect" not in call for call in calls)
    assert capture.process.returncode is not None


async def test_bad_server_checksum_does_not_contact_adb(fake_adb):
    with pytest.raises(CaptureError, match="checksum"):
        async with AdbCapture(fake_adb):
            pass
    assert not (fake_adb.screen_server_path.parent / "adb-calls.jsonl").exists()


async def test_missing_adb_produces_actionable_diagnostic(fake_adb, monkeypatch):
    monkeypatch.setattr("controller.screen_capture.verified", lambda _: True)
    with pytest.raises(CaptureError, match="not installed"):
        async with AdbCapture(replace(fake_adb, screen_adb_path="/no-such-adb")):
            pass


def test_websocket_video_only_and_does_not_mutate_playback(fake_adb, tmp_path, monkeypatch):
    monkeypatch.setattr("controller.screen_capture.verified", lambda _: True)
    settings = replace(fake_adb, database=str(tmp_path / "controller.sqlite"))
    app = create_app(settings, start_workers=False)
    with TestClient(app) as client:
        before = client.get("/api/v1/overview").json()["device"]
        with client.websocket_connect(PATH + "/stream", headers={"origin": "http://testserver"}) as ws:
            assert ws.receive_json()["type"] == "stream"
            assert ws.receive_bytes()[0] & 0x80
            assert ws.receive_bytes()[0] & 0x40
            ws.send_json({"keycode": 3})
            with pytest.raises(WebSocketDisconnect):
                while True:
                    ws.receive_bytes()
        assert client.get("/api/v1/overview").json()["device"] == before


async def test_real_uvicorn_websocket_transport_and_disconnect_cleanup(tmp_path):
    source = FakeCapture()
    source.queue.put_nowait(packet(CONFIG, b"config"))
    source.queue.put_nowait(packet(KEY_FRAME, b"keyframe"))
    app = create_app(
        Settings(database=str(tmp_path / "controller.sqlite"), screen_adb_serial="fixture"),
        start_workers=False,
    )
    app.state.screen.source_factory = lambda _: source
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
            async with connect(
                f"ws://127.0.0.1:{port}{PATH}/stream", origin=f"http://127.0.0.1:{port}", proxy=None
            ) as ws:
                assert json.loads(await ws.recv()) == META
                assert await ws.recv() == packet(CONFIG, b"config")
                assert await ws.recv() == packet(KEY_FRAME, b"keyframe")
            while source.stopped != 1:
                await asyncio.sleep(0.01)
            assert not app.state.screen.viewers
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
