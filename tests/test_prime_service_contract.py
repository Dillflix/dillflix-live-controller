"""Optional cross-repository contract tests against the real API 11 host service.

Run with Prime Player 0.1.0a23 installed (no device extras required). The runtime
boundary is controlled; the service, ownership engine and HTTP transport are real.
"""

import asyncio
import threading

import pytest

pytest.importorskip("dillflix_prime_player")
from dillflix_prime_player.backend import FridaBackend
from dillflix_prime_player.controller import Controller as PrimeController
from dillflix_prime_player.service import LocalService

from controller.device_input import WAKE_PACKET
from controller.executor.models import ExecutorError
from controller.prime_ownership import PrimeOwnership
from controller.prime_player.client import PrimePlayerClient


class Runtime(FridaBackend):
    def __init__(self):
        super().__init__("fixture", "unused", "/tmp/unused")
        self.latest_state = {"ready": True, "page": {}}
        self.entered = threading.Event()
        self.calls = []

    def compatibility(self):
        return {
            "capabilities": {
                c: {"available": True} for c in ("search", "play", "playback_status", "javascript_navigation")
            }
        }

    def command(self, op, **fields):
        self._check()
        self.calls.append((op, fields))
        self.entered.set()
        return {"id": "pending-search"}

    def fence(self, epoch):
        self.calls.append(("fence", epoch))
        return {"epoch": epoch, "acknowledged": True}

    def manual_input(self, **fields):
        self.calls.append(("manual", fields))


@pytest.fixture
async def service(tmp_path):
    runtime = Runtime()
    controller = PrimeController(runtime)
    path = tmp_path / "player.sock"
    server = LocalService(controller, path).start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = PrimePlayerClient(path)
    bridge = PrimeOwnership(path, "fixture")
    try:
        yield controller, runtime, client, bridge
    finally:
        await bridge.close()
        await client.close()
        await asyncio.to_thread(server.server.shutdown)
        thread.join(2)
        server.close()


async def test_real_handoff_interrupts_search_and_fences_stale_envelopes(service):
    controller, runtime, client, bridge = service
    health = await client.health()
    client.require(health, "search", "play", "playback_status", "cancel", "stop")
    previous = client.ownership(health, automatic=True)
    search = asyncio.create_task(client.search("Jets Lions", 60, previous))
    assert await asyncio.to_thread(runtime.entered.wait, 2)
    await bridge.acquire("manual-session")
    with pytest.raises(ExecutorError):
        await search
    assert not controller.lock.locked()
    await bridge.send("manual-session", WAKE_PACKET)
    assert runtime.calls[-1] == ("manual", {"key": "wake", "text": None})
    await bridge.release("manual-session")
    with pytest.raises(ExecutorError) as error:
        await client.search("late search", 60, previous)
    assert error.value.code == "prime_stale_result"
    assert [c[0] for c in runtime.calls].count("search-data") == 1


async def test_real_cancel_preserves_playback_and_old_stop_cannot_touch_new_attempt(service):
    controller, runtime, client, _ = service
    controller.current_attempt_id = "new-attempt"
    health = await client.health()
    result = await client.stop_attempt(health["session_id"], "old-attempt", health["ownership"])
    assert result == {"stopped": False, "reason": "attempt_not_current"}
    assert not runtime.calls
    receipt = await client.cancel_work(health["session_id"], "new-attempt", health["ownership"])
    assert receipt["acknowledged"] and receipt["playback_stopped"] is False
    assert controller.current_attempt_id == "new-attempt"
    assert [c[0] for c in runtime.calls] == ["fence"]


async def test_real_out_of_band_cancel_interrupts_search_without_waiting_for_timeout(service):
    _, runtime, client, _ = service
    health = await client.health()
    search = asyncio.create_task(client.search("Jets Lions", 60, health["ownership"]))
    assert await asyncio.to_thread(runtime.entered.wait, 2)
    async with asyncio.timeout(3):
        receipt = await client.cancel_work(health["session_id"], None, health["ownership"])
        with pytest.raises(ExecutorError):
            await search
    assert receipt["acknowledged"] and receipt["mode"] == "automatic"


@pytest.mark.parametrize("confirmed", [False, True])
async def test_real_stop_reports_page_exit_separately_from_native_confirmation(
    service, monkeypatch, confirmed
):
    controller, runtime, client, _ = service
    aid, title = "a" * 32, "amzn1.dv.gti.live-event"
    controller.current_attempt_id = aid
    controller.attempts[aid] = {
        "attempt_id": aid,
        "state": "playing",
        "resolved_id": title,
        "start": 0,
        "command_id": "original-launch",
    }
    monkeypatch.setattr(
        "dillflix_prime_player.controller.observe_playback",
        lambda *a, **k: {"matches_attempt": True, "evidence": {"checkpoint": {"epoch": 5}}},
    )

    def command(op, **fields):
        runtime.calls.append((op, fields))
        if op == "stop-playback" and confirmed:
            runtime.rows.append(
                {
                    "sequence": 1,
                    "kind": "control_app_event",
                    "data": {
                        "name": "PLAYBACK_STATE_CHANGE",
                        "playbackEpoch": 5,
                        "state": {"page": {"pageId": title}},
                        "params": {"value": {"state": "Stopped"}},
                    },
                }
            )
        return {"id": "stop-command", "result": {"page": {"pageType": "SEARCH"}}}

    runtime.command = command
    health = await client.health()
    cancelled = await client.cancel_work(health["session_id"], aid, health["ownership"])
    result = await client.stop_attempt(health["session_id"], aid, cancelled)
    assert result["stopped"] is (True if confirmed else None)
    assert result["page_exited"] is True
    assert result["attempt_id"] == aid and result["session_id"] == health["session_id"]
    assert [c[0] for c in runtime.calls].count("stop-playback") == 1


async def test_failed_runtime_blocked_ownership_can_still_acknowledge_cancel(service):
    controller, runtime, client, _ = service
    runtime.failure = "polling failed"
    controller.ownership.mode = "blocked"
    with pytest.raises(ExecutorError, match="healthy service"):
        await client.health()
    health = await client.control_health()
    receipt = client.control_ownership(health)
    assert receipt["acknowledged"] is False
    result = await client.cancel_work(health["session_id"], None, receipt)
    assert client.ownership({"session_id": health["session_id"], "ownership": result})
    assert runtime.calls[-1][0] == "fence"
    assert runtime.failure == "polling failed"
