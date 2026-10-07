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
        self.recovering = False

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if self.fail:
            raise PrimeOwnershipError("no acknowledgement")
        if method == "health":
            return {"api_version": 4, "serial": "fixture", "ownership": dict(self.state)}
        if method == "suspend":
            if any(params["previous"].get(key) != self.state[key] for key in ("session_id", "epoch", "handoff_id")):
                raise PrimeOwnershipError("stale")
            self.state.update(
                epoch=self.state["epoch"] + 1,
                mode="manual" if params["value"] else "blocked" if self.recovering else "automatic",
                owner_id=params.get("owner_id"),
                handoff_id="handoff" if params["value"] else None,
                acknowledged=not self.recovering,
            )
            return dict(self.state, recovery_pending=True) if self.recovering else dict(self.state)
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


async def test_player_restart_release_uses_current_player_receipt():
    b = Bridge()
    await b.acquire("stored")
    b.state.update(session_id="restarted-player", epoch=0, handoff_id="restored")
    current = dict(b.state)
    await b.release("stored")
    assert b.calls[-1] == ("suspend", {"value": False, "previous": current})
    assert b.state["mode"] == "automatic"
    assert b.receipt is None and b.session_id is None


async def test_same_player_release_retains_cached_epoch_fence():
    b = Bridge()
    await b.acquire("stored")
    b.state.update(epoch=8, handoff_id="newer-handoff")
    with pytest.raises(PrimeOwnershipError, match="stale"):
        await b.release("stored")
    assert b.state["mode"] == "manual"


async def test_recovering_player_release_clears_manual_authority_without_claiming_readiness():
    b = Bridge()
    await b.acquire("stored")
    b.recovering = True
    b.state.update(session_id="restarted-player", epoch=0, handoff_id="restored", acknowledged=False)
    await b.release("stored")
    assert b.state["mode"] == "blocked" and b.state["acknowledged"] is False
    assert b.state["owner_id"] is None
    assert b.receipt is None and b.session_id is None
    with pytest.raises(PrimeOwnershipError, match="not acknowledged"):
        await b.send("stored", WAKE_PACKET)
    assert not any(method == "manual_input" for method, _ in b.calls)


@pytest.mark.parametrize("invalid", [
    {"recovery_pending": False}, {"acknowledged": True}, {"mode": "manual"},
    {"owner_id": "gateway:stored"}, {"session_id": "another-player"},
])
async def test_incomplete_recovery_release_does_not_confirm_handoff(invalid):
    b = Bridge()
    await b.acquire("stored")
    b.recovering = True
    original = b.rpc

    async def rpc(method, **params):
        result = await original(method, **params)
        return {**result, **invalid} if method == "suspend" and not params["value"] else result

    b.rpc = rpc
    with pytest.raises(PrimeOwnershipError, match="release is unconfirmed"):
        await b.release("stored")


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


def test_gateway_releases_persisted_manual_hold_while_restarted_player_recovers(tmp_path):
    from fastapi.testclient import TestClient
    from test_manual_control import PATH, command, state, take

    from controller.api import create_app
    from controller.config import Settings

    app = create_app(
        Settings(
            database=str(tmp_path / "prime-recovery.sqlite"),
            screen_adb_serial="fixture",
            prime_player_socket="unused",
            simulation_delay=0,
        ),
        start_workers=False,
    )
    bridge = Bridge()
    app.state.control.prime = bridge
    with TestClient(app) as client:
        body = take(client)
        assert state(client)["automation"] == "paused"
        bridge.recovering = True
        bridge.state.update(session_id="restarted-player", epoch=0, handoff_id="restored", acknowledged=False)
        result = client.post(PATH, json=command(
            client, action="release", session_id=body["session_id"], owner_token=body["owner_token"],
        ))
        assert result.status_code == 200, result.text
        current = state(client)
        assert current["manual_control"] is None
        assert current["automation"] == "active"
        assert current["observed"] is None
        assert bridge.receipt is None and bridge.session_id is None
        assert bridge.state["mode"] == "blocked" and bridge.state["acknowledged"] is False
        assert bridge.state["owner_id"] is None
    assert not any(method == "manual_input" for method, _ in bridge.calls)
