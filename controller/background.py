"""Keep essential loops alive when an iteration escapes its local error handler."""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)


class BackgroundWorker:
    def __init__(self, name, function):
        self.name, self.function = name, function
        self.details = {"state": "starting", "failures": 0, "next_retry_at": None, "last_error": None}

    def status(self):
        return dict(self.details)

    async def run(self):
        consecutive = 0
        try:
            while True:
                started = time.monotonic()
                self.details = {
                    **self.details,
                    "state": "running",
                    "started_at": datetime.now(UTC).isoformat(),
                    "next_retry_at": None,
                }
                try:
                    await self.function()
                    raise RuntimeError("Background worker returned unexpectedly")
                except Exception as exc:
                    # A healthy run resets backoff. Persistent faults cannot spin.
                    consecutive = 1 if time.monotonic() - started >= 60 else consecutive + 1
                    delay = min(60, 2 ** min(consecutive - 1, 6))
                    now = datetime.now(UTC)
                    self.details = {
                        **self.details,
                        "state": "retrying",
                        "failures": self.details["failures"] + 1,
                        "last_failed_at": now.isoformat(),
                        "next_retry_at": (now + timedelta(seconds=delay)).isoformat(),
                        "last_error": {
                            "type": type(exc).__name__,
                            "sqlite_errorcode": getattr(exc, "sqlite_errorcode", None),
                            "sqlite_errorname": getattr(exc, "sqlite_errorname", None),
                        },
                    }
                    log.exception("Background worker %s failed; restarting in %s seconds", self.name, delay)
                await asyncio.sleep(delay)
        finally:
            # Cancellation (including shutdown while draining database work) is
            # never converted into a retry. Health does not need a writer lock.
            self.details = {**self.details, "state": "stopped", "next_retry_at": None}
