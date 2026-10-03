"""Optional manual-input bridge to the Prime Player mutation owner.

No direct ADB fallback is permitted when this bridge is configured.
"""

import asyncio
import http.client
import json
import socket
import struct


class PrimeOwnershipError(RuntimeError):
    pass


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=45)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class PrimeOwnership:
    def __init__(self, path, serial):
        self.path, self.serial = path, serial
        self.receipt = None
        self.session_id = None
        self.seq = 0

    def _request(self, method, **params):
        connection = UnixConnection(self.path)
        try:
            connection.request("POST", "/rpc", json.dumps({"method": method, "params": params}),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(2_200_001)
            if len(raw) > 2_200_000:
                raise PrimeOwnershipError("Prime ownership response exceeds limit")
            value = json.loads(raw)
            if response.status != 200 or "error" in value:
                raise PrimeOwnershipError("Prime ownership request rejected; inspect Prime service health")
            return value["result"]
        finally:
            connection.close()

    async def rpc(self, method, **params):
        task = asyncio.create_task(asyncio.to_thread(self._request, method, **params))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Draining the task is essential: cancelling an await does not stop an HTTP write.
            try:
                await task
            except Exception:
                pass
            raise

    async def acquire(self, session_id):
        health = await self.rpc("health")
        if health.get("api_version", 0) < 4 or health.get("serial") != self.serial:
            raise PrimeOwnershipError("Prime Player API/device does not match the manual gateway")
        receipt = await self.rpc("suspend", value=True, owner_id="gateway:" + session_id,
                                 previous=health["ownership"])
        if receipt.get("acknowledged") is not True or receipt.get("mode") != "manual":
            raise PrimeOwnershipError("Prime cancellation handoff is unconfirmed")
        self.receipt, self.session_id, self.seq = receipt, session_id, receipt["last_seq"]

    async def release(self, session_id):
        # Scope releases by the durable UI session, including after gateway restart.
        health = await self.rpc("health")
        current = health.get("ownership", {})
        if current.get("owner_id") != "gateway:" + session_id:
            return
        receipt = self.receipt if self.session_id == session_id and self.receipt else current
        await self.rpc("suspend", value=False, previous=receipt)
        if self.session_id == session_id:
            self.receipt = None
            self.session_id = None

    async def send(self, session_id, packet):
        if session_id != self.session_id or not self.receipt:
            raise PrimeOwnershipError("manual handoff is not acknowledged")
        keys = {19:"up",20:"down",21:"left",22:"right",23:"select",4:"back",3:"home",
                82:"menu",85:"play_pause",67:"backspace",224:"wake"}
        params = {}
        if len(packet) == 28 and packet[0] == 0:
            down, up = struct.unpack(">BBiii", packet[:14]), struct.unpack(">BBiii", packet[14:])
            if down[:2] != (0, 0) or up[:2] != (0, 1) or down[2:] != up[2:] or down[3:] != (0, 0) or down[2] not in keys:
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
        await self.rpc("manual_input", receipt=self.receipt, seq=self.seq, **params)


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
