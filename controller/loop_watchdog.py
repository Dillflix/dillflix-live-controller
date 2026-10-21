"""Capture blocking host code without polling the player or collecting locals."""

import asyncio
import sys
import threading
import time
import traceback


class LoopWatchdog:
    def __init__(self, recorder, *, threshold=1.0, interval=0.25):
        self.recorder = recorder
        self.threshold = threshold
        self.interval = interval
        self.loop = asyncio.get_running_loop()
        self.owner = threading.get_ident()
        self.last_beat = time.monotonic()
        self.last_report = 0.0
        self.stopping = threading.Event()
        self.handle = None
        self.thread = threading.Thread(target=self._watch, name="event-loop-watchdog", daemon=True)

    def start(self):
        self._beat()
        self.thread.start()

    def _beat(self):
        if self.stopping.is_set():
            return
        self.last_beat = time.monotonic()
        self.handle = self.loop.call_later(self.interval, self._beat)

    def _watch(self):
        while not self.stopping.wait(self.interval):
            now = time.monotonic()
            if now - self.last_beat < self.threshold or now - self.last_report < 30:
                continue
            self.last_report = now
            # Capture on this independent thread while the event loop is blocked.
            # Function names/locations only: no locals, source lines or secrets.
            frames = sys._current_frames()
            stacks = {}
            for thread in threading.enumerate():
                frame = frames.get(thread.ident)
                if frame is None or thread.ident == threading.get_ident():
                    continue
                stacks[str(thread.ident)] = {
                    "name": thread.name,
                    "event_loop": thread.ident == self.owner,
                    "frames": [{"file": f.filename, "line": f.lineno, "function": f.name}
                               for f in traceback.extract_stack(frame, limit=24)],
                }
                if len(stacks) >= 32:
                    break
            del frames
            self.recorder.record("runtime", "event_loop_blocked",
                                 delay_seconds=round(now - self.last_beat, 3), threads=stacks)

    async def close(self):
        self.stopping.set()
        if self.handle:
            self.handle.cancel()
        await asyncio.to_thread(self.thread.join, 2)
