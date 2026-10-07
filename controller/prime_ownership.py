"""Optional manual-input bridge to the Prime Player mutation owner.

No direct ADB fallback is permitted when this bridge is configured.
"""

import asyncio
import struct

from .executor.models import ExecutorError
from .prime_player.client import PrimePlayerClient


class PrimeOwnershipError(RuntimeError):
    pass


class PrimeOwnership:
    def __init__(self, path, serial):
        self.path, self.serial = path, serial
        self.client = PrimePlayerClient(path)
        self.receipt = None
        self.session_id = None
        self.seq = 0

    async def close(self):
        await self.client.close()

    async def rpc(self, method, **params):
        task = asyncio.create_task(self.client.rpc(method, budget=45, **params))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise
        except ExecutorError as exc:
            raise PrimeOwnershipError(str(exc)) from exc

    async def acquire(self, session_id):
        health = await self.rpc("health")
        if health.get("api_version", 0) < 4 or health.get("serial") != self.serial:
            raise PrimeOwnershipError("Prime Player API/device does not match the manual gateway")
        receipt = await self.rpc(
            "suspend", value=True, owner_id="gateway:" + session_id, previous=health["ownership"]
        )
        if receipt.get("acknowledged") is not True or receipt.get("mode") != "manual":
            raise PrimeOwnershipError("Prime cancellation handoff is unconfirmed")
        self.receipt, self.session_id, self.seq = receipt, session_id, receipt["last_seq"]

    async def release(self, session_id):
        # Scope releases by the durable UI session, including after gateway restart.
        health = await self.rpc("health")
        current = health.get("ownership", {})
        if current.get("owner_id") != "gateway:" + session_id:
            return
        receipt = (
            self.receipt
            if self.session_id == session_id
            and self.receipt
            and self.receipt.get("session_id") == current.get("session_id")
            else current
        )
        released = await self.rpc("suspend", value=False, previous=receipt)
        # A restarted supervisor can durably release the manual hold before its
        # backend is ready. This confirms intent release, not device readiness.
        recovery_pending = (
            released.get("recovery_pending") is True
            and released.get("acknowledged") is False
            and released.get("mode") == "blocked"
            and released.get("owner_id", "") is None
            and bool(current.get("session_id"))
            and released.get("session_id") == current["session_id"]
        )
        if not recovery_pending and (
            released.get("acknowledged") is not True or released.get("mode") != "automatic"
        ):
            raise PrimeOwnershipError("Prime release is unconfirmed")
        if self.session_id == session_id:
            self.receipt = None
            self.session_id = None

    async def send(self, session_id, packet):
        if session_id != self.session_id or not self.receipt:
            raise PrimeOwnershipError("manual handoff is not acknowledged")
        keys = {
            19: "up",
            20: "down",
            21: "left",
            22: "right",
            23: "select",
            4: "back",
            3: "home",
            82: "menu",
            85: "play_pause",
            67: "backspace",
            224: "wake",
        }
        params = {}
        if len(packet) == 28 and packet[0] == 0:
            down, up = struct.unpack(">BBiii", packet[:14]), struct.unpack(">BBiii", packet[14:])
            if (
                down[:2] != (0, 0)
                or up[:2] != (0, 1)
                or down[2:] != up[2:]
                or down[3:] != (0, 0)
                or down[2] not in keys
            ):
                raise PrimeOwnershipError("unsupported key packet")
            params["key"] = keys[down[2]]
        elif len(packet) >= 6 and packet[0] == 1:
            length = struct.unpack(">I", packet[1:5])[0]
            if not 1 <= length <= 200 or len(packet) != length + 5:
                raise PrimeOwnershipError("unsupported text packet")
            params["text"] = packet[5:].decode("ascii")
            if not all(32 <= ord(c) <= 126 for c in params["text"]):
                raise PrimeOwnershipError("unsupported text packet")
        else:
            raise PrimeOwnershipError("unsupported input packet")
        self.seq += 1
        result = await self.rpc("manual_input", receipt=self.receipt, seq=self.seq, **params)
        if (
            result.get("delivered") is not True
            or result.get("seq") != self.seq
            or result.get("session_id") != self.receipt.get("session_id")
        ):
            self.receipt = None
            raise PrimeOwnershipError("Manual delivery is unconfirmed; reconnect before sending again")


class PrimeInput:
    def __init__(self, bridge, session_id):
        self.bridge, self.session_id = bridge, session_id

    async def __aenter__(self):
        if self.bridge.session_id != self.session_id or not self.bridge.receipt:
            raise PrimeOwnershipError("manual handoff is not acknowledged")
        return self

    async def send(self, packet):
        await self.bridge.send(self.session_id, packet)

    async def close(self):
        # Disconnecting the remote does not release the durable manual session.
        pass
