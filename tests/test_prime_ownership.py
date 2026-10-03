import asyncio
from types import SimpleNamespace

import pytest

from controller.device_input import WAKE_PACKET, DeviceInput, Input
from controller.prime_ownership import PrimeOwnership, PrimeOwnershipError


class Bridge(PrimeOwnership):
    def __init__(self):
        super().__init__("unused", "fixture")
        self.calls = []
        self.state = {
            "mode": "automatic",
            "epoch": 0,
            "session_id": "service",
            "handoff_id": None,
            "owner_id": None,
            "acknowledged": True,
            "last_seq": 0,
        }
        self.fail = False

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if self.fail:
            raise PrimeOwnershipError("no acknowledgement")
        if method == "health":
            return {"api_version": 4, "serial": "fixture", "ownership": dict(self.state)}
        if method == "suspend":
            if params["previous"]["epoch"] != self.state["epoch"]:
                raise PrimeOwnershipError("stale")
            self.state.update(
                epoch=self.state["epoch"] + 1,
                mode="manual" if params["value"] else "automatic",
                owner_id=params.get("owner_id"),
                handoff_id="handoff",
            )
            return dict(self.state)
        return {"delivered": True, "seq": params["seq"], "session_id": "service"}


async def test_wake_and_user_keys_go_through_same_service_receipt():
    b = Bridge()
    await b.acquire("manual-session")
    await b.send("manual-session", WAKE_PACKET)
    await b.send("manual-session", Input(seq=1, key="down").encode())
    inputs = [p for method, p in b.calls if method == "manual_input"]
    assert [p["key"] for p in inputs] == ["wake", "down"]
    assert [p["seq"] for p in inputs] == [1, 2]
    assert inputs[0]["receipt"]["handoff_id"] == "handoff"


async def test_failed_handoff_never_sends_input():
    b = Bridge()
    b.fail = True
    with pytest.raises(PrimeOwnershipError):
        await b.acquire("manual-session")
    with pytest.raises(PrimeOwnershipError):
        await b.send("manual-session", WAKE_PACKET)
    assert not any(m == "manual_input" for m, _ in b.calls)


async def test_stale_release_does_not_resume_new_owner():
    b = Bridge()
    await b.acquire("new")
    before = len(b.calls)
    await b.release("old")
    assert b.calls[before:] == [("health", {})]
    assert b.state["mode"] == "manual"


async def test_gateway_restart_releases_only_matching_durable_owner():
    b = Bridge()
    b.state.update(mode="manual", owner_id="gateway:stored", epoch=3, handoff_id="stored")
    await b.release("stored")
    assert b.state["mode"] == "automatic"


async def test_bad_packet_is_not_forwarded():
    b = Bridge()
    await b.acquire("manual-session")
    with pytest.raises(PrimeOwnershipError):
        await b.send("manual-session", b"raw shell")
    assert not any(m == "manual_input" for m, _ in b.calls)


def test_automation_and_manual_gateway_must_use_same_service():
    service = SimpleNamespace(
        settings=SimpleNamespace(
            prime_player_socket="socket",
            screen_adb_serial="fixture",
            executor=SimpleNamespace(prime_socket="other"),
        ),
        executor=object(),
    )
    with pytest.raises(ValueError):
        DeviceInput(service)


async def test_rpc_cancellation_drains_background_write():
    b = PrimeOwnership("unused", "fixture")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(*a, **kw):
        entered.set()
        await release.wait()
        return {}

    b.client.rpc = slow
    task = asyncio.create_task(b.rpc("manual_input"))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await b.close()


def test_websocket_gateway_routes_wake_and_keys_without_direct_adb(tmp_path):
    from fastapi.testclient import TestClient
    from test_manual_control import ORIGIN, take

    from controller.api import create_app
    from controller.config import Settings

    app = create_app(
        Settings(
            database=str(tmp_path / "prime.sqlite"),
            screen_adb_serial="fixture",
            prime_player_socket="unused",
            simulation_delay=0,
        ),
        start_workers=False,
    )
    bridge = Bridge()
    app.state.control.prime = bridge

    def forbidden(_):
        raise AssertionError("direct ADB fallback")

    app.state.control.source_factory = forbidden
    with TestClient(app) as client:
        body = take(client)
        with client.websocket_connect("/api/v1/devices/living-room/control/input", headers=ORIGIN) as ws:
            ws.send_json({"session_id": body["session_id"], "owner_token": body["owner_token"]})
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"seq": 1, "key": "down"})
            assert ws.receive_json() == {"type": "sent", "seq": 1}
    assert [p["key"] for m, p in bridge.calls if m == "manual_input"] == ["wake", "down"]


def test_failed_prime_handoff_is_not_an_acknowledged_take_control(tmp_path):
    from fastapi.testclient import TestClient
    from test_manual_control import PATH, command

    from controller.api import create_app
    from controller.config import Settings

    app = create_app(
        Settings(
            database=str(tmp_path / "prime-fail.sqlite"),
            screen_adb_serial="fixture",
            prime_player_socket="unused",
            simulation_delay=0,
        ),
        start_workers=False,
    )
    bridge = Bridge()
    bridge.fail = True
    app.state.control.prime = bridge
    with TestClient(app) as client:
        result = client.post(PATH, json=command(client))
        assert result.status_code == 503
    assert not any(m == "manual_input" for m, _ in bridge.calls)
