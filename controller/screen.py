"""On-demand shared screen capture, deliberately independent of playback state."""

import asyncio
import logging
from contextlib import suppress
from urllib.parse import urlsplit

from fastapi import WebSocket, WebSocketDisconnect

from .screen_capture import CONFIG, KEY_FRAME, AdbCapture, CaptureError

log = logging.getLogger(__name__)
MAX_VIEWERS = 8
MAX_QUEUE_BYTES = 6 * 1024 * 1024


class Viewer:
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=90)
        self.bytes = 0
        self.waiting_key = True
        self.failed = False

    def put(self, message):
        if self.failed:
            return
        size = len(message) if isinstance(message, bytes) else 0
        if self.queue.full() or self.bytes + size > MAX_QUEUE_BYTES:
            self.fail("The screen connection fell behind. Reconnecting to live video.")
            return
        self.bytes += size
        self.queue.put_nowait(message)

    def fail(self, message):
        while not self.queue.empty():
            self.queue.get_nowait()
        self.bytes = 0
        self.failed = True
        self.queue.put_nowait({"type": "error", "message": message})

    async def get(self):
        message = await self.queue.get()
        if isinstance(message, bytes):
            self.bytes -= len(message)
        return message


class ScreenStream:
    def __init__(self, settings, source_factory=AdbCapture):
        self.settings = settings
        self.source_factory = source_factory
        self.lock = asyncio.Lock()
        self.viewers = set()
        self.task = None
        self.metadata = None
        self.state = "idle"
        self.error = None

    @property
    def enabled(self):
        return bool(self.settings.screen_adb_serial)

    def status(self):
        return {
            "enabled": self.enabled,
            "state": self.state if self.enabled else "disabled",
            "viewers": len(self.viewers),
            "error": self.error,
            "read_only": True,
            "audio": False,
            "max_size": self.settings.screen_max_size,
            "max_fps": self.settings.screen_max_fps,
            "bit_rate": self.settings.screen_bit_rate,
            "stream_path": "/api/v1/devices/living-room/screen/stream" if self.enabled else None,
        }

    async def subscribe(self):
        async with self.lock:
            if not self.enabled:
                raise CaptureError("Screen mirroring is not configured on this controller.")
            if len(self.viewers) >= MAX_VIEWERS:
                raise CaptureError(
                    "All eight screen viewer slots are in use. Close another viewer and retry."
                )
            viewer = Viewer()
            self.viewers.add(viewer)
            if self.task is None or self.task.done():
                self.metadata = None
                self.state, self.error = "starting", None
                self.task = asyncio.create_task(self.run())
            elif self.metadata:
                viewer.put(self.metadata)
            return viewer

    async def unsubscribe(self, viewer):
        async with self.lock:
            self.viewers.discard(viewer)
            if not self.viewers:
                await self.stop_task()

    async def stop_task(self):
        if self.task:
            if not self.task.cancelling():
                self.task.cancel()
            # A disappearing HTTP scope must not cancel the capture's cleanup twice.
            await asyncio.shield(asyncio.gather(self.task, return_exceptions=True))
            self.task = None
        self.metadata = None
        self.state = "idle"

    async def stop(self):
        async with self.lock:
            for viewer in self.viewers:
                viewer.fail("Controller is restarting. Reconnecting shortly.")
            self.viewers.clear()
            await self.stop_task()

    async def run(self):
        try:
            async with self.source_factory(self.settings) as source:
                self.metadata = source.metadata
                for viewer in self.viewers:
                    viewer.put(self.metadata)
                config = None
                while True:
                    packet = await source.packet()
                    flags = int.from_bytes(packet[:8], "big")
                    if flags & CONFIG:
                        if config is not None and config != packet:
                            # Rotation or an encoder restart needs fresh decoder state.
                            for viewer in self.viewers:
                                viewer.put(self.metadata)
                        config = packet
                        for viewer in self.viewers:
                            viewer.waiting_key = True
                        continue
                    self.state = "streaming"
                    for viewer in self.viewers:
                        if viewer.waiting_key:
                            if not config or not flags & KEY_FRAME:
                                continue
                            viewer.put(config)
                            viewer.waiting_key = False
                        viewer.put(packet)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = (
                str(error)
                if isinstance(error, CaptureError)
                else ("The device screen stream stopped. Check ADB connectivity and the controller logs.")
            )
            self.state = "error"
            log.warning("Screen capture failed: %s", error)
            for viewer in self.viewers:
                viewer.fail(self.error)


async def serve_screen(websocket: WebSocket, stream: ScreenStream):
    # nginx supplies Host. Browser credentials must not authorize another origin's viewer.
    origin = urlsplit(websocket.headers.get("origin", ""))
    if (
        origin.scheme not in {"http", "https"}
        or origin.netloc.lower() != websocket.headers.get("host", "").lower()
    ):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        viewer = await stream.subscribe()
    except CaptureError as error:
        await websocket.send_json({"type": "error", "message": str(error)})
        await websocket.close(code=1013)
        return

    async def send():
        while True:
            message = await viewer.get()
            async with asyncio.timeout(5):
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_json(message)
                    if message.get("type") == "error":
                        await websocket.close(code=1013)
                        return

    async def receive():
        message = await websocket.receive()
        if message["type"] != "websocket.disconnect":
            # This endpoint is strictly outbound video, not an ADB command proxy.
            await websocket.close(code=1008)

    tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]

    async def cleanup():
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await stream.unsubscribe(viewer)
        with suppress(WebSocketDisconnect, RuntimeError, OSError):
            await websocket.close()

    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            with suppress(WebSocketDisconnect, RuntimeError, OSError, TimeoutError):
                task.result()
    finally:
        # Finish cleanup even if the HTTP server cancels this socket's request scope.
        await asyncio.shield(asyncio.create_task(cleanup()))
