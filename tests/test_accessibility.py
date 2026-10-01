"""Reference parity and transport/capture races; these tests do not control a TV."""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from controller.config import Settings
from controller.executor.accessibility import (
    MAX_LINE,
    SUPPORTED_VERSION,
    EventLines,
    FocusCollector,
    FocusState,
    associate_capture,
    focus_metadata,
    observation_validity,
    parse_event,
)
from controller.executor.adb import PACKAGE, AdbDevice, Frame
from controller.executor.config import ExecutorConfig
from controller.executor.models import ExecutorError

REFERENCE = json.loads((Path(__file__).parent / "fixtures/prime-accessibility-conformance.json").read_text())


def event(at=101, label="A", kind="TYPE_VIEW_FOCUSED", description="null"):
    return (
        f"EventType: {kind}; EventTime: {at}; PackageName: {PACKAGE}; "
        f"ClassName: button; Text: [{label}]; ContentDescription: {description}"
    )


def wire_event(**kwargs):
    return event(**kwargs) + "; Enabled: true; recordCount: 0"


@pytest.mark.parametrize("case", REFERENCE["cases"], ids=lambda case: case["name"])
def test_archive_conformance(case):
    now = [0]
    state = FocusState(now=lambda: now[0], app_version=case["version"])
    for index, step in enumerate(case["steps"]):
        if step["op"] == "action":
            state.begin_action(step["action"], step.get("device_time"))
        elif step["op"] == "event":
            state.ingest(step["line"])
        elif step["op"] == "time":
            now[0] = step["now"]
        else:
            state.invalidate()
        assert state.snapshot() == step["expected"], (case["name"], index)


def test_parser_properties_and_scope_never_invent_selected_state():
    raw = event(description="[Search Suggestions] A") + (
        "; ItemCount: 5; CurrentItemIndex: 2; Enabled: false; Checked: true; Scrollable: true; "
        "ScrollX: 30; MaxScrollX: 100; ScrollDeltaY: -1"
    )
    parsed = parse_event(raw)
    assert parsed["properties"]["currentItemIndex"] == 2
    assert parse_event("garbage") is None
    assert parse_event(raw.replace("EventTime: 101", "EventTime: NaN")) is None
    assert parse_event(raw.replace("EventTime: 101", "EventTime: Infinity")) is None
    state = FocusState(now=lambda: 0)
    state.begin_action("RIGHT", 100)
    state.ingest(raw)
    metadata = focus_metadata(state.snapshot())
    assert metadata["descriptionContext"] == "Search Suggestions"
    assert metadata["eventProperties"] == {
        "enabled": False,
        "checked": True,
        "scrollable": True,
        "collection": {"itemCount": 5, "currentItemIndex": 2},
        "scroll": {"x": {"offset": 30, "max": 100}},
    }
    assert "selected" not in metadata["eventProperties"]
    snapshot = state.snapshot()
    snapshot["channels"]["input"]["focus"]["text"] = "modified"
    assert state.snapshot()["channels"]["input"]["focus"]["text"] == "A"


def test_utf8_fragments_crlf_and_oversize_records_do_not_create_false_labels():
    lines = []
    decoder = EventLines(lines.append)
    line = event(label="Dynamic tab é")
    raw = (line + "\r\n").encode()
    split = raw.index("é".encode()) + 1
    decoder.feed(raw[:split])
    assert lines == []
    decoder.feed(raw[split:])
    assert lines == [line]
    decoder.feed(b"x" * MAX_LINE)
    decoder.feed(event(label="Not a complete record").encode())
    decoder.feed(b"\n" + event(label="Real next record").encode() + b"\n")
    assert lines == [line, event(label="Real next record")]
    assert decoder.buffer == "" and not decoder.discarding


@pytest.mark.parametrize("change", ["during_capture", "during_preparation", "expiry", "none"])
def test_capture_association_and_finalization(change):
    now = [0]
    state = FocusState(now=lambda: now[0])
    state.begin_action("OBSERVE", 100)
    state.ingest(event())
    before = state.snapshot()
    if change == "during_capture":
        state.ingest(event(102, "B"))
    after = state.snapshot()
    if change == "during_preparation":
        state.ingest(event(102, "B"))
    if change == "expiry":
        now[0] = 60001
    captured = associate_capture(before, after, state.snapshot())
    assert captured["usable"] is (change == "none")
    assert captured["focusProjection"]["usableCurrentEvidence"] is captured["usable"]
    validity = observation_validity(captured, state.snapshot())
    assert validity["screenContinuity"] == "unproven"
    if change in {"during_capture", "during_preparation"}:
        assert validity["status"] == "changed"
        assert captured["reason"] == "focus_changed_during_capture"
    elif change == "none":
        assert validity["status"] == "unchanged"
        state.ingest(event(103, "External remote changed focus"))
        assert observation_validity(captured, state.snapshot())["status"] == "changed"
    else:
        assert captured["reason"] == "expired"


class Process:
    def __init__(self):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.returncode = None
        self.exited = asyncio.Event()
        self.killed = 0

    def kill(self):
        self.killed += 1
        self.exit()

    def exit(self):
        self.returncode = 0
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.exited.set()

    async def wait(self):
        await self.exited.wait()
        return self.returncode


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.002)


async def test_stream_failure_withdraws_evidence_and_never_respawns_on_capture_calls():
    children, versions = [], []

    async def spawn(*args, **kwargs):
        child = Process()
        children.append(child)
        return child

    async def version():
        future = asyncio.get_running_loop().create_future()
        versions.append(future)
        return await future

    collector = FocusCollector(["fixture"], version, spawn=spawn)
    starting = asyncio.create_task(collector.start())
    try:
        await until(lambda: versions)
        versions[0].set_result(SUPPORTED_VERSION)
        await starting
        collector.begin_action("RIGHT", 100)
        children[0].stderr.feed_data((wire_event() + "\n").encode())
        await until(lambda: collector.snapshot()["usable"])
        captured = collector.snapshot()["validity"]
        children[0].exit()
        await until(lambda: collector.status == "failed")
        assert collector.snapshot()["appVersion"] is None
        assert collector.snapshot()["usable"] is False
        assert collector.snapshot()["validity"]["generation"] > captured["generation"]
        for _ in range(5):
            await collector.start()
        assert len(children) == 1 and len(versions) == 1
        assert collector.failure["code"] == "accessibility_listener_exited"
        assert collector.snapshot()["appVersionStatus"] == "unavailable"
        await collector.stop()
        assert children[0].killed == 0 and collector.runner is None
        await asyncio.sleep(0.025)
        assert len(children) == 1
    finally:
        starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
        await collector.stop()


async def test_cancel_during_spawn_reaps_process_before_stop_returns():
    spawned, release = asyncio.Event(), asyncio.Event()
    child = Process()

    async def spawn(*args, **kwargs):
        spawned.set()
        await release.wait()
        return child

    async def version():
        return SUPPORTED_VERSION

    collector = FocusCollector(["fixture"], version, spawn=spawn)
    starting = asyncio.create_task(collector.start())
    await spawned.wait()
    stopping = asyncio.create_task(collector.stop())
    await asyncio.sleep(0.01)
    assert not stopping.done()
    stopping.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    await starting
    assert child.killed == 1 and child.exited.is_set() and collector.runner is None


async def test_focus_wait_coalesces_and_missing_event_is_bounded_unknown():
    state = FocusState(coalesce_ms=30)
    collector = FocusCollector([], lambda: None, state=state)
    action = collector.begin_action("RIGHT", 100)
    state.ingest(event())
    before = time.monotonic()
    assert (await collector.wait_for_focus(action, timeout_ms=100))["usable"]
    assert time.monotonic() - before >= 0.025
    action = collector.begin_action("DOWN", 110)
    result = await collector.wait_for_focus(action, timeout_ms=20)
    assert result["usable"] is False and result["reason"] == "no_labeled_focus_after_action"
    waiting = asyncio.create_task(collector.wait_for_focus(action))
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting


async def test_stop_boundary_does_not_start_reader_or_query_clock(monkeypatch):
    target = device()
    target.accessibility.begin_action("OBSERVE", 100)
    target.accessibility.state.ingest(event())

    async def forbidden(*args):
        raise AssertionError("Stop must not start native observation or read the device clock")

    monkeypatch.setattr(target.accessibility, "start", forbidden)
    monkeypatch.setattr(target, "shell", forbidden)
    assert await target.prepare_input("STOP_PRIME") is None
    assert not target.accessibility.snapshot()["usable"]


async def test_real_reader_subprocess_utf8_and_close(tmp_path):
    program = tmp_path / "events.py"
    program.write_text(
        "import sys,time\n"
        f"raw={(wire_event(label='Dynamic tab é') + chr(10)).encode()!r}\n"
        "time.sleep(.05)\n"
        "for byte in raw: sys.stdout.buffer.write(bytes([byte])); sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n"
    )

    async def version():
        return SUPPORTED_VERSION

    collector = FocusCollector([sys.executable, str(program)], version)
    try:
        await collector.start()
        process = collector.process
        collector.begin_action("OBSERVE", 100)
        await until(lambda: collector.snapshot()["usable"])
        assert collector.snapshot()["focus"]["text"] == "Dynamic tab é"
    finally:
        await collector.stop()
    assert process.returncode is not None and collector.status == "stopped"


def device():
    return AdbDevice(
        Settings(screen_adb_serial="fixture", executor=ExecutorConfig(adb_timeout=0.1, media_probe=False))
    )


async def test_adb_clock_cutoff_failed_clock_and_version_lookup(monkeypatch):
    target = device()

    async def start():
        pass

    async def shell(*args):
        if args == ("dumpsys", "package", PACKAGE):
            return " versionCode=321009610 minSdk=21\n versionName=PVFTV-321.0096-L\n"
        # Invalidation precedes even the clock request.
        assert target.accessibility.snapshot()["usable"] is False
        return "1.2341 3.00"

    monkeypatch.setattr(target.accessibility, "start", start)
    monkeypatch.setattr(target, "shell", shell)
    assert await target.app_version() == SUPPORTED_VERSION
    action = await target.prepare_input("RIGHT")
    assert action["deviceTime"] == 1245
    target.accessibility.state.ingest(event(1245, "Old"))
    assert not target.accessibility.snapshot()["usable"]
    target.accessibility.state.ingest(event(1246, "New"))
    assert target.accessibility.snapshot()["usable"]

    async def failed(*args):
        raise ExecutorError("adb_timeout", "fixture")

    monkeypatch.setattr(target, "shell", failed)
    action = await target.prepare_input("DOWN")
    assert "deviceTime" not in action
    target.accessibility.state.ingest(event(900000, "Unassociated"))
    assert not target.accessibility.snapshot()["usable"]


@pytest.mark.parametrize("when", ["screenshot", "final_state", "none"])
async def test_adb_capture_binds_native_evidence_through_finalization(monkeypatch, when):
    from datetime import UTC, datetime

    import controller.executor.adb as adb_module

    target = device()
    state = target.accessibility.state
    state.begin_action("OBSERVE", 100)
    state.ingest(event())
    reads = 0

    async def start():
        pass

    async def status():
        nonlocal reads
        reads += 1
        if when == "final_state" and reads == 2:
            state.ingest(event(102, "B"))
        return PACKAGE, []

    async def run(*args):
        if when == "screenshot":
            state.ingest(event(102, "B"))
        return b"png fixture"

    monkeypatch.setattr(target.accessibility, "start", start)
    monkeypatch.setattr(target, "state", status)
    monkeypatch.setattr(target, "run", run)
    monkeypatch.setattr(adb_module, "prepare_image", lambda _: b"jpeg fixture")
    frame = await target.capture()
    assert isinstance(frame, Frame) and frame.captured_at <= datetime.now(UTC)
    assert frame.native_focus["usable"] is (when == "none")
    if when != "none":
        with pytest.raises(ExecutorError) as exc:
            target.validate_frame(frame)
        assert exc.value.code == "stale_navigation_focus"
    else:
        assert target.validate_frame(frame)["status"] == "unchanged"
