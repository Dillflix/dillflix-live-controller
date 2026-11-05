"""Bounded timing evidence for slow read projections, without response data."""
import logging
import time
from contextlib import contextmanager

log = logging.getLogger(__name__)


@contextmanager
def read_timing(operation, device_id):
    started = previous = time.monotonic()
    stages = {}

    def mark(stage):
        nonlocal previous
        now = time.monotonic()
        stages[stage] = round(now - previous, 3)
        previous = now

    try:
        yield mark
    finally:
        elapsed = time.monotonic() - started
        if elapsed >= 1:
            log.warning("Slow controller read: operation=%s device=%s seconds=%.3f stages=%s",
                        operation, device_id, elapsed, stages)
