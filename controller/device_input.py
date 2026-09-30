"""Exclusive, bounded manual input gateway. No shell commands are accepted from clients."""

import asyncio
import json
import logging
import struct
import time
from contextlib import suppress
from urllib.parse import urlsplit

from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from pydantic import Field, ValidationError

from .models import StrictModel
from .screen_capture import AdbCapture, CaptureError
from .screen_install import SERVER_VERSION

log = logging.getLogger(__name__)
KEYS = {
    "up": 19,
    "down": 20,
    "left": 21,
    "right": 22,
    "select": 23,
    "back": 4,
    "home": 3,
    "menu": 82,
    "play_pause": 85,
    "backspace": 67,
}


class Attach(StrictModel):
    session_id: str = Field(min_length=16, max_length=100)
    owner_token: str = Field(min_length=32, max_length=128)


class Input(StrictModel):
    seq: int = Field(strict=True, ge=1)
    key: str | None = None
    text: str | None = Field(default=None, min_length=1, max_length=200)

    def encode(self):
        if self.key in KEYS and self.text is None:
            # One bounded tap: down and up in the same write. Never leave held keys behind.
            return b"".join(struct.pack(">BBiii", 0, action, KEYS[self.key], 0, 0) for action in (0, 1))
        if self.key is None and self.text and all(32 <= ord(c) <= 126 for c in self.text):
            data = self.text.encode("ascii")
            return b"\x01" + struct.pack(">I", len(data)) + data
        raise ValueError("Use a supported remote key or 1–200 printable ASCII text characters.")


class AdbInput(AdbCapture):
    def server_options(self):
        return [
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            # Use the exact same pinned protocol/lifecycle as screen capture.
            SERVER_VERSION,
            f"scid={self.scid}",
            "log_level=warn",
            "tunnel_forward=true",
            "video=false",
            "audio=false",
            "control=true",
            "power_on=false",
            "clipboard_autosync=false",
            "cleanup=true",
        ]

    async def read_metadata(self):
        # In control-only mode the first (control) socket receives device metadata.
        await asyncio.wait_for(self.reader.readexactly(64), 10)

    async def send(self, packet):
        if self.reader.at_eof() or self.writer.is_closing():
            raise CaptureError("Device input disconnected. Reconnect the remote and check the TV.")
        self.writer.write(packet)
        await asyncio.wait_for(self.writer.drain(), 2)


def same_origin(headers):
    origin = urlsplit(headers.get("origin", ""))
    return origin.scheme in {"http", "https"} and origin.netloc.lower() == headers.get("host", "").lower()


async def receive_json(socket, timeout):
    message = await asyncio.wait_for(socket.receive_text(), timeout)
    if len(message) > 4096:
        raise ValueError("Input message is too large.")
    return json.loads(message)


class DeviceInput:
    def __init__(self, service, source_factory=AdbInput):
        self.service = service
        self.source_factory = source_factory
        self.lock = asyncio.Lock()
        self.connection = None
        self.monitor = None

    def start(self):
        self.monitor = asyncio.create_task(self.watch_expiry())

    def revoke_locked(self):
        task = self.connection[0] if self.connection else None
        self.connection = None
        if task and not task.done() and not task.cancelling():
            task.cancel()
        return task

    async def drain(self, task):
        if task:
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))

    async def stop(self):
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        async with self.lock:
            task = self.revoke_locked()
        await self.drain(task)

    async def expire(self):
        async with self.lock:
            if self.service.manual_expired():
                # Keep automation paused until all manual writes and cleanup end.
                await self.drain(self.revoke_locked())
                self.service.expire_manual()

    async def watch_expiry(self):
        while True:
            try:
                await self.expire()
            except Exception:
                log.exception("Manual control expiry failed; input remains deadline-fenced")
            await asyncio.sleep(0.25)

    async def command(self, device_id, command):
        async with self.lock:
            if command.action == "release" and self.connection:
                _, attached = self.connection
                if (attached.session_id, attached.owner_token) == (command.session_id, command.owner_token):
                    await self.drain(self.revoke_locked())
            result = self.service.manual_command(device_id, command)
            task = None
            if self.connection:
                _, attached = self.connection
                try:
                    self.service.manual_authorized(device_id, attached.session_id, attached.owner_token)
                except HTTPException:
                    task = self.revoke_locked()
            await self.drain(task)
            if command.action == "take":
                # Replaying an old command receipt must not seize a newer session.
                with self.service.db.transaction() as db:
                    session = self.service.db.device(db, device_id).get("manual_control")
                if session and session["session_id"] == command.session_id:
                    try:
                        await self.service.playback_work(
                            self.service.prepare_manual_input,
                            device_id,
                            command.session_id,
                            command.owner_token,
                        )
                    except HTTPException:
                        raise
                    except Exception as error:
                        raise HTTPException(
                            503,
                            "Automation paused; playback cancellation is unconfirmed. "
                            "Reconnect the remote to retry.",
                        ) from error
        return result

    async def serve(self, websocket: WebSocket, device_id):
        if device_id != "living-room" or not same_origin(websocket.headers):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        task = None
        try:
            attach = Attach.model_validate(await receive_json(websocket, 5))
            async with self.lock:
                try:
                    await self.service.playback_work(
                        self.service.prepare_manual_input,
                        device_id,
                        attach.session_id,
                        attach.owner_token,
                    )
                except HTTPException:
                    raise
                except Exception as error:
                    raise HTTPException(503, "Playback cancellation is unconfirmed. Reconnect to retry.") from error
                self.service.manual_authorized(device_id, attach.session_id, attach.owner_token)
                if self.connection:
                    raise HTTPException(
                        409, "This remote is already connected in another tab. Close it first."
                    )
                task = asyncio.create_task(self.run(websocket, device_id, attach))
                self.connection = (task, attach)
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task is None or not task.cancelled():
                raise
        except (HTTPException, ValueError, ValidationError) as error:
            await self.error(
                websocket,
                error.detail if isinstance(error, HTTPException) else "Invalid remote connection request.",
            )
        except (WebSocketDisconnect, OSError, RuntimeError, TimeoutError):
            pass
        finally:

            async def cleanup():
                async with self.lock:
                    if self.connection and self.connection[0] is task:
                        self.revoke_locked()
                await self.drain(task)
                with suppress(WebSocketDisconnect, RuntimeError, OSError):
                    await websocket.close()

            await asyncio.shield(asyncio.create_task(cleanup()))

    @staticmethod
    async def error(websocket, message):
        with suppress(WebSocketDisconnect, RuntimeError, OSError, TimeoutError):
            async with asyncio.timeout(2):
                await websocket.send_json({"type": "error", "message": message})

    @staticmethod
    async def connect_source(websocket, source):
        # A tab can disappear during the bounded ADB startup. Observe that
        # disconnect immediately instead of holding its slot until startup ends.
        startup = asyncio.create_task(source.__aenter__())
        incoming = asyncio.create_task(websocket.receive())
        try:
            done, _ = await asyncio.wait((startup, incoming), return_when=asyncio.FIRST_COMPLETED)
            if incoming in done:
                message = incoming.result()
                if message["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect()
                raise ValueError("Wait for the remote to be ready before sending input.")
            await startup
        finally:
            for task in (startup, incoming):
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(startup, incoming, return_exceptions=True)

    async def run(self, websocket, device_id, attach):
        source = self.source_factory(self.service.settings)
        try:
            await self.connect_source(websocket, source)
            async with asyncio.timeout(2):
                await websocket.send_json({"type": "ready"})
            last_seq, tokens, checked_at = 0, 10.0, time.monotonic()
            while True:
                try:
                    raw = await receive_json(websocket, 1)
                except TimeoutError:
                    self.service.manual_authorized(device_id, attach.session_id, attach.owner_token)
                    continue
                message = Input.model_validate(raw)
                packet = message.encode()
                async with self.lock:
                    self.service.manual_authorized(device_id, attach.session_id, attach.owner_token)
                    if not self.connection or self.connection[0] is not asyncio.current_task():
                        raise HTTPException(409, "Input ownership changed.")
                    if message.seq <= last_seq:
                        raise ValueError("Input sequence was already used. Commands are never replayed.")
                    now = time.monotonic()
                    tokens = min(10.0, tokens + (now - checked_at) * 8)
                    checked_at = now
                    if tokens < 1:
                        raise ValueError("Input rate exceeded. Reconnect and try more slowly.")
                    tokens -= 1
                    last_seq = message.seq
                    await source.send(packet)
                async with asyncio.timeout(2):
                    # This acknowledges transport delivery, not the visible effect on the TV.
                    await websocket.send_json({"type": "sent", "seq": message.seq})
        except asyncio.CancelledError:
            raise
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception as error:
            message = (
                error.detail
                if isinstance(error, HTTPException)
                else str(error)
                if isinstance(error, (CaptureError, ValueError)) and not isinstance(error, ValidationError)
                else "Device input stopped. Reconnect the remote; check the screen before sending again."
            )
            await self.error(websocket, message)
        finally:
            # Cancellation cannot interrupt owned process cleanup, including partially started sources.
            cleanup = asyncio.create_task(source.close())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
