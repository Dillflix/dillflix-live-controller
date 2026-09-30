"""The real executor must provide these same cancellation/ordering guarantees."""

import asyncio
import copy
import json
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from controller.config import Settings
from controller.device_input import Attach, DeviceInput
from controller.models import ManualControlCommand
from controller.playback import SimulatedPlaybackAdapter
from controller.service import Controller


def take_command(service):
    return ManualControlCommand(
        command_id=str(uuid4()), expected_revision=service.overview()["device"]["revision"],
        action="take", session_id=uuid4().hex, owner_token=uuid4().hex,
    )


async def started(event):
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.005)


def test_cancel_stops_active_playback_and_device_fence_survives_restart(rig):
    _, service, _ = rig
    service.tick()
    with service.db.transaction() as db:
        job = db.execute("SELECT * FROM jobs").fetchone()
        payload = json.loads(job["payload"])
    service.playback.cancel(job["id"])
    assert service.playback.inspect(job["id"])["state"] == "cancelled"
    assert service.playback.observe("living-room") is None
    service.playback.cancel(job["id"])  # Idempotent after active playback stops.

    fence = payload["intent_version"] + 2
    service.playback.cancel_device("living-room", fence)
    restarted = SimulatedPlaybackAdapter(service.db)
    late = copy.deepcopy(payload)
    late.update(request_id=str(uuid4()), intent_version=fence)
    assert restarted.submit(late)["state"] == "cancelled"
    newer = {**late, "request_id": str(uuid4()), "intent_version": fence + 1}
    restarted.submit(newer)
    assert restarted.inspect(newer["request_id"])["state"] == "playing_verified"
    restarted.cancel_device("living-room", fence)
    assert restarted.observe("living-room")["request_id"] == newer["request_id"]


async def test_slow_playback_keeps_event_loop_responsive_and_shutdown_drains(tmp_path, monkeypatch):
    service = Controller(Settings(database=str(tmp_path / "worker.sqlite"), simulation_delay=0))
    entered, release = threading.Event(), threading.Event()
    original = service.playback.observe

    def slow_observe(device_id):
        entered.set()
        assert release.wait(3)
        return original(device_id)

    monkeypatch.setattr(service.playback, "observe", slow_observe)
    worker = asyncio.create_task(service.run_worker())
    service.tasks = [worker]
    stopping = None
    try:
        await started(entered)
        # This runs while the adapter call is blocked, on the same event loop as
        # screen/input sockets and feed/status tasks in production.
        assert service.overview()["device"]["automation"] == "active"
        stopping = asyncio.create_task(service.stop())
        await asyncio.sleep(0.02)
        assert not stopping.done()
        with service.db.transaction() as db:
            assert db.execute("SELECT 1 FROM leases WHERE owner=?", (service.owner,)).fetchone()
        release.set()
        await asyncio.wait_for(stopping, 2)
        assert worker.cancelled()
    finally:
        release.set()
        if stopping:
            await stopping
        else:
            await service.stop()


async def test_inflight_submit_drains_before_manual_input_can_start(tmp_path, monkeypatch):
    service = Controller(Settings(
        database=str(tmp_path / "take.sqlite"), simulation_delay=0, screen_adb_serial="fixture"
    ))
    manager = DeviceInput(service)
    entered, release = threading.Event(), threading.Event()
    submit = service.playback.submit

    def slow_submit(payload):
        entered.set()
        assert release.wait(3)
        return submit(payload)

    monkeypatch.setattr(service.playback, "submit", slow_submit)
    tick = asyncio.create_task(service.playback_work(service.tick))
    takeover = None
    try:
        await started(entered)
        command = take_command(service)
        takeover = asyncio.create_task(manager.command("living-room", command))
        async with asyncio.timeout(2):
            while not service.overview()["device"].get("manual_control"):
                await asyncio.sleep(0.005)
        with pytest.raises(HTTPException, match="Waiting for playback cancellation"):
            service.manual_authorized("living-room", command.session_id, command.owner_token)
        assert not takeover.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(tick, takeover), 2)
        assert service.manual_authorized("living-room", command.session_id, command.owner_token)["input_ready"]
        assert service.playback.observe("living-room") is None
    finally:
        release.set()
        await asyncio.gather(tick, *([takeover] if takeover else []), return_exceptions=True)
        await manager.stop()
        await service.stop()


async def test_failed_barrier_survives_restart_and_release_cannot_bypass_it(tmp_path, monkeypatch):
    settings = Settings(database=str(tmp_path / "retry.sqlite"), screen_adb_serial="fixture", simulation_delay=0)
    service = Controller(settings)
    manager = DeviceInput(service)

    def unavailable(*_):
        raise ConnectionError("uncertain cancellation")

    monkeypatch.setattr(service.playback, "cancel_device", unavailable)
    command = take_command(service)
    with pytest.raises(HTTPException) as error:
        await manager.command("living-room", command)
    assert error.value.status_code == 503
    with pytest.raises(HTTPException):
        service.manual_authorized("living-room", command.session_id, command.owner_token)
    await manager.command("living-room", command.model_copy(update={
        "action": "release", "command_id": str(uuid4()), "expected_revision": 1, "release_mode": "active"
    }))
    service.stage_playback()
    with service.db.transaction() as db:
        assert not db.execute("SELECT 1 FROM jobs").fetchone()
    await manager.stop()
    await service.stop()
    restarted = Controller(settings)
    try:
        assert restarted.overview()["device"]["input_handoff"]
        restarted.tick()
        assert restarted.overview()["device"]["input_handoff"] is None
        assert restarted.overview()["device"]["observed"]["verified"]
    finally:
        await restarted.stop()


@pytest.mark.parametrize("action", ["release", "expiry"])
async def test_manual_transport_cleanup_precedes_automation_resume(tmp_path, action):
    service = Controller(Settings(database=str(tmp_path / "drain.sqlite"), screen_adb_serial="fixture"))
    manager = DeviceInput(service)
    command = take_command(service)
    cleanup_started, cleanup_release = asyncio.Event(), asyncio.Event()

    async def input_transport():
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await cleanup_release.wait()

    await manager.command("living-room", command)
    transport = asyncio.create_task(input_transport())
    await asyncio.sleep(0)
    manager.connection = (transport, Attach(session_id=command.session_id, owner_token=command.owner_token))
    if action == "expiry":
        with service.db.transaction() as db:
            device = service.db.device(db)
            device["manual_control"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            service.db.save_device(db, device)
        ending = asyncio.create_task(manager.expire())
    else:
        ending = asyncio.create_task(manager.command("living-room", command.model_copy(update={
            "action": "release", "command_id": str(uuid4()), "expected_revision": 1, "release_mode": "active"
        })))
    try:
        await asyncio.wait_for(cleanup_started.wait(), 1)
        service.tick()
        device = service.overview()["device"]
        assert device["automation"] == "paused" and device["manual_control"]
        assert device["desired"] is None
        cleanup_release.set()
        await asyncio.wait_for(ending, 1)
        assert service.overview()["device"]["automation"] == "active"
    finally:
        cleanup_release.set()
        await asyncio.gather(ending, return_exceptions=True)
        await manager.stop()
        await service.stop()
