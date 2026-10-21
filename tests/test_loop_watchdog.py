import asyncio
import threading

import pytest

from controller.loop_watchdog import LoopWatchdog


@pytest.mark.asyncio
async def test_captures_blocking_stack_before_loop_resumes_and_stops():
    captured = threading.Event()
    records = []

    class Recorder:
        def record(self, source, kind, **data):
            records.append((source, kind, data))
            captured.set()

    watchdog = LoopWatchdog(Recorder(), threshold=0.05, interval=0.01)
    watchdog.start()
    try:
        # Deliberately block this event loop until the independent monitor captures it.
        assert captured.wait(2)
        data = records[0][2]
        owner = next(t for t in data["threads"].values() if t["event_loop"])
        assert any(f["function"] == "test_captures_blocking_stack_before_loop_resumes_and_stops"
                   for f in owner["frames"])
        assert all(set(f) == {"file", "line", "function"} for f in owner["frames"])
        await asyncio.sleep(0.05)
        assert len(records) == 1
    finally:
        await watchdog.close()
    assert not watchdog.thread.is_alive()


@pytest.mark.asyncio
async def test_async_wait_does_not_count_as_blocking():
    class Recorder:
        def record(self, *args, **kwargs):
            pytest.fail("An asynchronous wait must not be reported as a blocked loop")

    watchdog = LoopWatchdog(Recorder(), threshold=0.5, interval=0.01)
    watchdog.start()
    try:
        await asyncio.sleep(0.6)
    finally:
        await watchdog.close()
