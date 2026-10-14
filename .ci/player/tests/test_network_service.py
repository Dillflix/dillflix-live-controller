"""The actual Unix API/SDK must preserve service ownership across Prime restart."""
import asyncio
import threading
import time
from unittest.mock import Mock

import pytest

from dillflix_prime_player.client import PrimePlayer
from dillflix_prime_player.service import LocalService
from dillflix_prime_player.recovery import RuntimeSupervisor
from test_network_device import Bridge, NetworkConfig, NetworkDevice
from test_recovery import BASE, Backend


def test_transition_and_manual_input_through_real_socket(tmp_path):
    backends = []
    def factory(profile):
        backend = Backend(profile, len(backends) + 1)
        backends.append(backend)
        return backend
    store = Mock()
    store.load_active.return_value = None
    runtime = RuntimeSupervisor(BASE, tmp_path, factory, store=store, serial="fixture")
    inputs = []
    config = NetworkConfig({"enabled": True, "token_file": str(tmp_path / "token"), "profiles": [
        {"id": "calgary", "profile_uuid": "a", "country": "CA", "province": "AB", "city": "Calgary",
         "server": "configured", "routing_confirmed": True}]})
    device = NetworkDevice(runtime, tmp_path, config=config, bridge=Bridge(), manual_input=lambda **p: inputs.append(p))
    for _ in range(5):
        device.tick()
    server = LocalService(device, tmp_path / "player.sock").start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def check():
        client = PrimePlayer(tmp_path / "player.sock")
        old = await client._request("health")
        operation = await client.vpn_connect("calgary", operation_id="socket-test", deadline_at=time.time() + 300,
                                             interrupt_playback=True, qualify=True)
        assert operation["state"] == "pending"
        for _ in range(7):
            device.tick()
        current = await client._request("health")
        assert current["session_id"] != old["session_id"]
        assert backends[0].closed and len(backends) == 2
        assert runtime.restart_times == []  # Planned changes are not crash retries.
        assert current["ownership"]["service_instance_id"] == old["ownership"]["service_instance_id"]
        assert (await client.vpn_status(operation_id="socket-test"))["state"] == "ready"
        manual = await client._request("suspend", value=True, owner_id="remote", previous=current["ownership"])
        assert manual["mode"] == "manual" and manual["acknowledged"]
        await client._request("manual_input", receipt=manual, seq=1, key="home")
        assert inputs == [{"key": "home", "text": None}]
        assert device.observation["profile_uuid"] == "a"  # No automatic disconnect.
    try:
        asyncio.run(check())
    finally:
        server.server.shutdown()
        thread.join(3)
        server.close()
