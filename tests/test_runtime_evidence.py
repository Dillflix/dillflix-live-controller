"""Replay supplied capture-05 values; no physical-device/model accuracy claims."""

import copy
import json
from pathlib import Path

from test_real_executor import settings

from controller.executor.accessibility import FocusState, associate_capture, parse_event
from controller.executor.adb import AdbDevice
from controller.executor.event_records import EventRecords, RecordStream, TransportEvidence
from controller.executor.media import (
    EXTRA_ID,
    MediaTracker,
    identity_key,
    normalize,
    records,
    reported_playing,
)

FIXTURES = Path(__file__).parent / "fixtures"
ACCESSIBILITY = json.loads((FIXTURES / "prime-capture05-accessibility.json").read_text())["records"]
MEDIA = json.loads((FIXTURES / "prime-capture05-media.json").read_text())["records"]


def test_all_103_recovered_records_including_six_multiline_labels_parse_exactly():
    assert len(ACCESSIBILITY) == 103
    assert sum("Watch Live\nEnglish Broadcast" in r["raw"] for r in ACCESSIBILITY) == 6
    for row in ACCESSIBILITY:
        assert parse_event(row["raw"]) == row["event"]


def test_record_framing_retains_both_channels_and_invalidates_capture_through_partial_record():
    focus = FocusState(now=lambda: 0)
    focus.begin_action("OBSERVE", 344528000)
    transport, delivered, truncated = TransportEvidence(), [], []

    def stream(channel):
        records = EventRecords(
            lambda raw: delivered.append((channel, parse_event(raw))),
            pending=lambda active: transport.set(channel, active),
            truncated=truncated.append,
        )
        return RecordStream(records, transport, channel)

    stdout, stderr = stream("stdout"), stream("stderr")
    before = transport.snapshot(focus.snapshot())
    raw = next(row["raw"] for row in ACCESSIBILITY if "Watch Live\n" in row["raw"])
    left, right = raw.split("\n")
    stdout.feed((left + "\n").encode())
    assert not delivered
    assert transport.snapshot(focus.snapshot())["reason"] == "incomplete_event_record"
    stderr.feed((raw.replace("English Broadcast", "Français") + "\r\n").encode())
    assert delivered[0][0] == "stderr"
    assert delivered[0][1]["text"] == "Watch Live\nFrançais"
    for byte in (right + "\n").encode():
        stdout.feed(bytes([byte]))
    assert delivered[1][1]["text"] == "Watch Live\nEnglish Broadcast"
    after = transport.snapshot(focus.snapshot())
    assert not after["transport"]["pendingRecords"]
    assert associate_capture(before, after, after)["captureAssociation"]["status"] == "changed-during-capture"
    assert not truncated


def test_partial_oversized_and_replaced_records_are_rejected_not_shortened_into_labels():
    records, failures, pending = [], [], []
    decoder = EventRecords(records.append, pending=pending.append, truncated=failures.append, limit=900)
    complete = next(row["raw"] for row in ACCESSIBILITY if "Watch Live\n" in row["raw"])
    decoder.push(complete.split("\n")[0])
    decoder.push(complete)
    assert records == [complete]
    assert failures == ["next_event_before_terminator"]
    decoder.push(complete.split("\n")[0])
    decoder.end()
    assert failures[-1] == "stream_ended_before_terminator"
    decoder.push(complete.split("\n")[0] + "x" * 1000)
    assert failures[-1] == "event_size_limit"
    assert pending[-1] is False


def update(tracker, record, **overrides):
    options = dict(
        boot_id="fixture-boot",
        started_at="2026-10-01T00:55:00+00:00",
        finished_at="2026-10-01T00:55:01+00:00",
        foreground="com.amazon.firebat",
        legacy=[
            {
                "package": "com.amazon.firebat",
                "active": True,
                "state": record["snapshot"]["sessions"][0]["playbackState"]["state"],
            }
        ],
        device_before_ms=record["elapsedRealtimeMs"] - 10,
        elapsed_seconds=0.1,
    )
    options.update(overrides)
    return tracker.update(record, [record], **options)


def test_media_snapshot_and_callback_evidence_keep_identity_transport_and_title_separate():
    tracker = MediaTracker()
    observed = []
    for record in MEDIA[:7]:
        sample = update(tracker, record)
        observed.append(sample["session"]["transport"])
        assert sample["session"]["display_title"] is None
        assert sample["session"]["description_title"] == "PrimeVideo"
        assert sample["session"]["app_content_id"] is None
        assert sample["snapshot_atomic"] is False and sample["live_edge"] == "unmeasured"
        assert reported_playing(sample) is (sample["session"]["transport"] == "playing")
    assert observed == ["none", "none", "buffering", "buffering", "playing", "buffering", "playing"]
    assert tracker.events[-2]["transport"] == "buffering"
    assert tracker.events[-1]["transport"] == "playing"
    assert tracker.events[-1]["device_elapsed_ms"] - tracker.events[-2]["device_elapsed_ms"] == 301
    # Repeated journal reads never invent extra callback transitions.
    count = len(tracker.events)
    update(tracker, MEDIA[6])
    assert len(tracker.events) == count


def test_conflicting_ids_multiple_prime_sessions_disconnected_and_stale_probe_do_not_bind():
    record = copy.deepcopy(MEDIA[4])
    record["snapshot"]["sessions"][0]["sessionExtras"][EXTRA_ID] = "a-different-runtime-id"
    assert normalize(record)["session"]["identity_status"] == "conflicting"
    assert identity_key(update(MediaTracker(), record)) is None
    record["snapshot"]["sessions"].append(copy.deepcopy(record["snapshot"]["sessions"][0]))
    assert normalize(record)["session"] is None
    record = copy.deepcopy(MEDIA[4])
    record["snapshot"]["listenerConnected"] = False
    assert identity_key(update(MediaTracker(), record)) is None
    assert (
        identity_key(update(MediaTracker(), MEDIA[4], device_before_ms=MEDIA[4]["elapsedRealtimeMs"] + 60000))
        is None
    )
    assert records("ACTIVITY MANAGER SERVICES\n" + json.dumps(MEDIA[4]) + '\n{"partial":') == [MEDIA[4]]


def test_runtime_id_switch_and_back_cannot_reuse_the_old_identity_binding():
    tracker = MediaTracker()
    original = update(tracker, MEDIA[4])
    changed = copy.deepcopy(MEDIA[4])
    changed["elapsedRealtimeMs"] += 100
    changed["snapshot"]["sessions"][0]["sessionToken"] = "new-session"
    assert identity_key(update(tracker, changed)) != identity_key(original)
    returned = copy.deepcopy(MEDIA[4])
    returned["elapsedRealtimeMs"] += 200
    assert identity_key(update(tracker, returned)) != identity_key(original)
    assert identity_key(update(tracker, returned, boot_id="new-boot")) != identity_key(original)


def test_brief_pause_callback_between_playing_polls_withdraws_live_mode_history():
    tracker = MediaTracker()
    first = update(tracker, MEDIA[4])
    paused = copy.deepcopy(MEDIA[4])
    paused["elapsedRealtimeMs"] += 100
    paused["eventPayload"]["value"]["state"] = 2
    # Composite snapshot can already be playing again; the callback still matters.
    assert paused["snapshot"]["sessions"][0]["playbackState"]["state"] == 3
    after = update(tracker, paused)
    assert identity_key(after) != identity_key(first)
    assert after["recent_events"][-1]["transport"] == "paused"
    assert reported_playing(after)


def test_malformed_metadata_is_unknown_and_slow_probe_read_cannot_renew_freshness():
    invalid = copy.deepcopy(MEDIA[4])
    invalid["snapshot"]["sessions"][0]["metadata"] = ["malformed"]
    assert normalize(invalid)["session"] is None
    sample = update(MediaTracker(), MEDIA[4], elapsed_seconds=30)
    assert sample["source_health"] == "stale"
    assert not reported_playing(sample)


async def test_probe_adapter_reads_rotating_history_and_does_not_hide_a_callback_after_the_dump(tmp_path):
    device = AdbDevice(settings(tmp_path))
    dumped = copy.deepcopy(MEDIA[4])
    callback = copy.deepcopy(dumped)
    callback["elapsedRealtimeMs"] += 50
    callback["wallTimeMs"] += 50
    callback["eventPayload"]["value"]["state"] = 2
    callback["snapshot"]["sessions"][0]["playbackState"]["state"] = 2
    commands = []

    async def shell(*args):
        if args[0] == "cat":
            return "12345678-1234-1234-1234-123456789abc\n" + str(dumped["elapsedRealtimeMs"] / 1000) + " 0\n"
        assert args == ("dumpsys", "activity", "service", "dev.tvprobe.mediasession/.ProbeService")
        return "SERVICE HEADER\n" + json.dumps(dumped)

    async def run(*args):
        commands.append(args)
        return (json.dumps(dumped) + "\n" + json.dumps(callback)).encode()

    device.shell, device.run = shell, run
    sample = await device.runtime(
        device_state=("com.amazon.firebat", [{"package": "com.amazon.firebat", "active": True, "state": 3}])
    )
    assert sample["session"]["transport"] == "paused"
    assert sample["history_available"]
    assert not sample["active_confirmed"]  # Earlier dumpsys state disagrees; no atomicity claim.
    assert commands[0][:3] == ("exec-out", "run-as", "dev.tvprobe.mediasession")
    assert "events.previous.jsonl" in commands[0][-1] and "events.jsonl" in commands[0][-1]
