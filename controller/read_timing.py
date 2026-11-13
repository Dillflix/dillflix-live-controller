"""Bounded timing evidence for slow read projections, without response data."""
import logging
import time
from contextlib import contextmanager

log = logging.getLogger(__name__)


@contextmanager
def read_timing(operation, device_id):
    started = previous = time.monotonic()
    cpu_started = cpu_previous = time.thread_time()
    stages = {}
    stage_cpu = {}

    def mark(stage):
        nonlocal previous, cpu_previous
        now = time.monotonic()
        cpu_now = time.thread_time()
        stages[stage] = round(now - previous, 3)
        stage_cpu[stage] = round(cpu_now - cpu_previous, 3)
        previous = now
        cpu_previous = cpu_now

    try:
        yield mark
    finally:
        elapsed = time.monotonic() - started
        if elapsed >= 1:
            log.warning("Slow controller read: operation=%s device=%s seconds=%.3f stages=%s "
                        "cpu_seconds=%.3f stage_cpu=%s",
                        operation, device_id, elapsed, stages,
                        time.thread_time() - cpu_started, stage_cpu)
