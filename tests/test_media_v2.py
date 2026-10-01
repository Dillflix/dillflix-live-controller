"""Synthetic v2 contract/fault fixtures; these are not Fire TV captures."""

import copy
import json
from datetime import UTC, datetime

import pytest
from test_real_executor import settings

from controller.executor.adb import AdbDevice
from controller.executor.media import (
    EXTRA_ID,
    MEDIA_ID,
    SOURCE_ID,
    MediaTracker,
    identity_key,
    records,
    reported_playing,
)
from controller.executor.media_v2 import LOSS_COUNTERS, OPERATIONS
from controller.executor.models import RuntimeStatus
from controller.executor.monitoring import status
from controller.executor.probe import check

INSTANCE = "750a8eb1-77a4-4ca2-8263-04d5dc5b0e4c"
OTHER = "0eb6a39f-3870-4af8-bdfc-35d5a42b327a"


def stamp(at):
    return {"elapsedRealtimeMs": at, "wallTimeMs": 1800000000000 + at}


def dump(seq=3, at=10000, *, instance=INSTANCE, epoch=1, sid="s1", playing=3):
    row = dict(
        package="com.amazon.firebat",
        sessionInstanceId=sid,
        sessionToken="42",
        connectionEpoch=epoch,
        readSucceeded=True,
        dataComplete=True,
        readErrors={},
        nestedReadErrors=0,
        serialization={"complete": True, "omissions": 0, "reasons": []},
        acquisitionStart=stamp(at + 1),
        acquisitionEnd=stamp(at + 4),
        metadata={
            "entries": {MEDIA_ID: "event-A", SOURCE_ID: "event-A"},
            "description": {"mediaId": "event-A"},
        },
        sessionExtras={EXTRA_ID: "event-A"},
        playbackState={"state": playing, "positionMs": 1000, "speed": 1},
    )
    health = dict(
        listenerConnected=True,
        activeSessionsListenerRegistered=True,
        callbacksRegistered=True,
        pollExpected=True,
        pollOverdueMs=0,
        lastSuccessfulSessionReadElapsedMs=at + 5,
        lastPollSuccessElapsedMs=at - 100,
        sessionRegistrations=[{"sessionInstanceId": sid, "registrationCallSucceeded": True}],
    )
    health.update({k: dict(failures=0, lastAttemptSucceeded=True, inProgress=False) for k in OPERATIONS})
    interval = {"serviceInstanceId": instance, "firstSequence": 1, "lastSequence": seq}

    def retention(rs):
        return dict(ranges=rs, rangesComplete=True, evictedRanges=0, unattributedRecords=0, invalidRecords=0)

    journal = dict(
        latestProducedSequence=seq,
        latestWrittenSequence=seq,
        initialized=True,
        writerAlive=True,
        writerClosing=False,
        lastWriteSucceeded=True,
        queueDepth=0,
        queueCapacity=64,
        inFlightSequence=0,
        oldestPendingAgeMs=0,
        lossRanges=[],
        retention={"initialized": True, "current": retention([interval]), "previous": retention([])},
    )
    journal.update({k: 0 for k in LOSS_COUNTERS})

    def cp(t):
        return dict(
            serviceInstanceId=instance,
            latestProducedSequence=seq,
            latestWrittenSequence=seq,
            capturedAt=stamp(t),
        )

    return dict(
        schemaVersion=2,
        probeBuild="2.0.0",
        serviceInstanceId=instance,
        connectionEpoch=epoch,
        serviceCreatedAt=stamp(100),
        connectionStartedAt=stamp(200),
        **stamp(at + 6),
        snapshotAtomic=False,
        snapshotIsCached=False,
        dumpTimedOut=False,
        dumpFailed=False,
        dumpRequestedAt=stamp(at - 2),
        dumpResponseAt=stamp(at + 8),
        checkpointBeforeSnapshot=cp(at - 1),
        checkpointAfterSnapshot=cp(at + 7),
        latestProducedSequence=seq,
        latestWrittenSequence=seq,
        collectionHealth=health,
        journalHealth=journal,
        snapshot=dict(
            listenerConnected=True,
            complete=True,
            sessions=[row],
            acquisitionStart=stamp(at),
            acquisitionEnd=stamp(at + 5),
        ),
    )


def journal(record, reason="heartbeat", payload=None):
    r = copy.deepcopy(record)
    for key in (
        "dumpRequestedAt",
        "dumpResponseAt",
        "dumpTimedOut",
        "dumpFailed",
        "snapshotIsCached",
        "checkpointBeforeSnapshot",
        "checkpointAfterSnapshot",
    ):
        r.pop(key)
    r.update(
        sequence=record["latestProducedSequence"],
        reason=reason,
        eventPayload=payload,
        callbackReceivedAt=stamp(record["elapsedRealtimeMs"] - 7),
    )
    return r


def sample(tracker, record, history=None, **overrides):
    opts = dict(
        boot_id="boot",
        probe_instance=None,
        started_at=datetime.now(UTC).isoformat(),
        finished_at=datetime.now(UTC).isoformat(),
        foreground="com.amazon.firebat",
        legacy=[{"package": "com.amazon.firebat", "active": True, "state": 3}],
        device_before_ms=record["elapsedRealtimeMs"] - 10,
        elapsed_seconds=0.1,
        max_age=15,
    )
    opts.update(overrides)
    return tracker.update(record, [journal(record)] if history is None else history, **opts)


def test_real_identity_ignores_hash_and_freshness_uses_acquisition_start():
    tracker = MediaTracker()
    first = sample(tracker, dump())
    assert reported_playing(first), first
    changed = dump(at=10100)
    changed["snapshot"]["sessions"][0]["sessionToken"] = "different-diagnostic-hash"
    second = sample(tracker, changed)
    assert identity_key(first) == identity_key(second)
    assert datetime.fromisoformat(second["observed_at"]) < datetime.fromisoformat(second["started_at"])
    RuntimeStatus.model_validate(status(second, None, "target", 15))


@pytest.mark.parametrize("changes", [{"instance": OTHER}, {"epoch": 2}, {"sid": "s2"}])
def test_explicit_identity_change_with_same_token_hash_invalidates_binding(changes):
    tracker = MediaTracker()
    before = sample(tracker, dump())
    after = sample(tracker, dump(at=10200, **changes))
    assert identity_key(before) != identity_key(after)
    assert after["session"]["session_token"] == before["session"]["session_token"]


@pytest.mark.parametrize(
    "path,value",
    [
        (("schemaVersion",), 3),
        (("schemaVersion",), True),
        (("serviceInstanceId",), None),
        (("connectionEpoch",), 0),
        (("snapshotIsCached",), True),
        (("dumpTimedOut",), True),
        (("dumpFailed",), True),
        (("recordTruncated",), True),
        (("snapshot", "complete"), False),
        (("snapshot", "sessions"), None),
        (("collectionHealth", "callbacksRegistered"), False),
        (("collectionHealth", "lastPollSuccessElapsedMs"), 100),
        (("collectionHealth", "sessionReads", "lastAttemptSucceeded"), False),
        (("collectionHealth", "poll", "inProgress"), True),
        (("snapshot", "sessions", 0, "dataComplete"), False),
        (("snapshot", "sessions", 0, "sessionInstanceId"), None),
        (("checkpointAfterSnapshot", "serviceInstanceId"), OTHER),
        (("journalHealth", "lossRanges"), None),
        (("journalHealth", "droppedRecords"), True),
    ],
)
def test_invalid_v2_never_falls_back_to_v1_hash(path, value):
    record = dump()
    target = record
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result = sample(MediaTracker(), record)
    assert identity_key(result) is None, result
    assert result["schema_version"] == 2 and result["problems"]


def test_missing_schema_on_v2_envelope_is_invalid_not_legacy():
    record = dump()
    del record["schemaVersion"]
    assert sample(MediaTracker(), record)["source_error"] == "unsupported_probe_schema"


def test_cached_dump_response_cannot_freshen_old_snapshot_or_clock_jump():
    record = dump()
    record["snapshot"]["acquisitionStart"] = stamp(500)
    result = sample(MediaTracker(), record, device_before_ms=40000)
    assert identity_key(result) is None
    record = dump()
    record["wallTimeMs"] += 10**9
    assert reported_playing(sample(MediaTracker(), record))  # UTC is not freshness authority.


def test_empty_sessions_healthy_but_multiple_or_duplicate_sessions_cannot_bind():
    record = dump()
    record["snapshot"]["sessions"] = []
    record["collectionHealth"]["sessionRegistrations"] = []
    result = sample(MediaTracker(), record)
    assert result["continuity_ready"] and not reported_playing(result)
    record = dump()
    row = copy.deepcopy(record["snapshot"]["sessions"][0])
    row["sessionInstanceId"] = "s2"
    record["snapshot"]["sessions"].append(row)
    record["collectionHealth"]["sessionRegistrations"].append(
        {"sessionInstanceId": "s2", "registrationCallSucceeded": True}
    )
    result = sample(MediaTracker(), record)
    assert result["continuity_ready"] and identity_key(result) is None
    row["sessionInstanceId"] = "s1"
    assert sample(MediaTracker(), record)["source_health"] == "invalid"


@pytest.mark.parametrize(
    "field,value",
    [
        ("writerAlive", False),
        ("writerClosing", True),
        ("oldestPendingAgeMs", 6000),
        ("lastWriteSucceeded", False),
    ],
)
def test_writer_health_is_separate_from_fresh_playing_snapshot(field, value):
    record = dump()
    record["journalHealth"][field] = value
    result = sample(MediaTracker(), record)
    assert result["source_health"] == "fresh" and result["collection_health"] == "fresh"
    assert identity_key(result) is None and result["journal_health"] != "healthy"


def test_missing_final_callback_is_detected_before_any_later_record_exists():
    tracker = MediaTracker()
    good = sample(tracker, dump())
    newer = dump(seq=4, at=11000)
    for key in ("journalHealth", "checkpointBeforeSnapshot", "checkpointAfterSnapshot"):
        newer[key]["latestWrittenSequence"] = 3
    newer["latestWrittenSequence"] = 3
    newer["journalHealth"].update(queueDepth=1, oldestPendingAgeMs=20)
    result = sample(tracker, newer, [journal(dump())])
    assert result["latest_produced_sequence"] == 4 and result["latest_written_sequence"] == 3
    assert "journal_pending" in result["problems"] and not reported_playing(result)
    recovered = sample(tracker, dump(seq=4, at=12000))
    assert recovered["continuity_ready"] and identity_key(recovered) != identity_key(good)


def test_loss_counter_survives_written_high_water_and_can_only_start_new_visual_binding():
    tracker = MediaTracker()
    good = sample(tracker, dump())
    record = dump(seq=5, at=11000)
    j = record["journalHealth"]
    j.update(
        droppedRecords=1,
        writeFailures=1,
        lossRanges=[
            dict(serviceInstanceId=INSTANCE, firstSequence=4, lastSequence=4, reason="write_failure")
        ],
    )
    failed = sample(tracker, record)
    assert (
        "observation_loss_or_collection_failure" in failed["problems"]
        and "journal_sequence_gap" in failed["problems"]
    )
    clean = dump(seq=5, at=12000)
    clean["journalHealth"] = copy.deepcopy(record["journalHealth"])
    recovered = sample(tracker, clean)
    assert recovered["continuity_ready"] and recovered["loss_counters"]["droppedRecords"] == 1
    assert identity_key(recovered) != identity_key(good)


def test_contiguous_ids_required_not_just_maximum_and_foreign_or_legacy_cannot_fill_gap():
    tracker = MediaTracker()
    sample(tracker, dump())
    newer = dump(seq=6, at=13000)
    history = [journal(dump(seq=4, at=11000)), journal(dump(seq=5, at=12000, instance=OTHER)), journal(newer)]
    result = sample(tracker, newer, history)
    assert "journal_sequence_gap" in result["problems"]


def test_duplicate_records_are_ok_conflicts_and_truncated_terminal_are_not():
    record = dump()
    event = journal(record)
    assert sample(MediaTracker(), record, [event, event])["continuity_ready"]
    conflict = copy.deepcopy(event)
    conflict["reason"] = "poll"
    assert "conflicting_journal_duplicate" in sample(MediaTracker(), record, [event, conflict])["problems"]
    event.update(recordTruncated=True, snapshot=None)
    parsed = records(json.dumps(event))
    assert len(parsed) == 1
    assert "journal_record_truncated" in sample(MediaTracker(), record, parsed)["problems"]


def test_pause_callback_then_playing_snapshot_withdraws_prior_binding_and_retains_payload():
    tracker = MediaTracker()
    before = sample(tracker, dump())
    record = dump(seq=4, at=11000)
    payload = dict(
        sessionInstanceId="s1",
        sessionToken="42",
        value={"state": 2},
        historical=False,
        serialization={"complete": True},
    )
    after = sample(tracker, record, [journal(record, "playback_state_changed", payload)])
    assert after["session"]["transport"] == "playing" and after["recent_events"][-1]["transport"] == "paused"
    assert identity_key(after) != identity_key(before)
    payload["value"] = {"metadata": {"nested": {"readError": {"errorType": "failed"}}}}
    bad = sample(MediaTracker(), record, [journal(record, "metadata_changed", payload)])
    assert "callback_incomplete" in bad["problems"]


def test_removal_is_historical_and_never_populates_current_empty_session():
    tracker = MediaTracker()
    previous = dump()
    sample(tracker, previous)
    now = dump(seq=4, at=11000)
    now["snapshot"]["sessions"] = []
    now["collectionHealth"]["sessionRegistrations"] = []
    removed = dict(
        serviceInstanceId=INSTANCE,
        sessionInstanceId="s1",
        historical=True,
        eventCompletionInferred=False,
        removalObservedAt=stamp(10900),
        lastKnownSnapshot=previous["snapshot"]["sessions"][0],
    )
    result = sample(tracker, now, [journal(now, "session_removed", removed)])
    assert result["session"] is None and identity_key(result) is None
    evidence = result["recent_events"][-1]
    assert evidence["historical"] and not evidence["event_completion_inferred"]
    assert evidence["last_known_acquisition"] == stamp(10004)
    assert evidence["last_known"]["runtime_media_id"] == "event-A"


def test_regressed_checkpoint_cannot_reset_an_existing_epoch():
    tracker = MediaTracker()
    sample(tracker, dump(seq=5))
    result = sample(tracker, dump(seq=3, at=11000))
    assert result["source_error"] == "probe_checkpoint_regressed"


@pytest.mark.parametrize("export_recovers", [False, True])
async def test_adb_v2_rechecks_export_once_without_pid_reads_and_checks_final_checkpoint(
    tmp_path, export_recovers
):
    device = AdbDevice(settings(tmp_path))
    first = dump()
    last = dump(seq=4, at=10020)
    calls = []
    returned = iter([first, last, last])
    exports = []

    async def shell(*args):
        calls.append(args)
        if args[0] == "cat":
            return "12345678-1234-1234-1234-123456789abc\n9.99 0\n"
        assert args[:3] == ("dumpsys", "activity", "service")
        return json.dumps(next(returned))

    async def run(*args):
        assert args[:3] == ("exec-out", "run-as", "dev.tvprobe.mediasession")
        exports.append(args)
        return json.dumps(journal(last if export_recovers and len(exports) == 2 else first)).encode()

    device.shell, device.run = shell, run
    result = await device.runtime(
        device_state=("com.amazon.firebat", [{"package": "com.amazon.firebat", "active": True, "state": 3}])
    )
    assert len(calls) == 4 and len(exports) == 2
    assert result["history_gap"] is (not export_recovers)
    assert result["schema_version"] == 2 and result["latest_produced_sequence"] == 4
    assert (identity_key(result) is not None) is export_recovers


def test_service_restart_between_bracketing_dumps_and_future_journal_prevent_binding():
    record = dump()
    result = sample(MediaTracker(), record, acquisition_before=dump(instance=OTHER))
    assert result["source_error"] == "probe_changed_during_acquisition"
    result = sample(MediaTracker(), record, [journal(record), journal(dump(seq=4, at=10001))])
    assert "journal_newer_than_snapshot" in result["problems"]


async def test_readiness_exposes_pending_history_and_cannot_call_legacy_v2_ready():
    class Device:
        async def runtime(self):
            return sample(MediaTracker(), dump())

    result = await check(Device())
    assert result["ready"] and result["service_instance_id"] == INSTANCE

    class Legacy:
        async def runtime(self):
            return dict(
                source="media_probe", source_health="fresh", probe_instance="1:2", history_available=True
            )

    result = await check(Legacy())
    assert not result["ready"] and result["upgrade_required"]


def test_metadata_callback_identity_is_separate_from_later_composite_snapshot():
    tracker = MediaTracker()
    before = sample(tracker, dump())
    record = dump(seq=4, at=11000)
    payload = dict(
        sessionInstanceId="s1",
        historical=False,
        serialization={"complete": True},
        value={"entries": {MEDIA_ID: "event-B"}, "description": {"mediaId": "event-B"}},
    )
    after = sample(tracker, record, [journal(record, "metadata_changed", payload)])
    assert identity_key(before) != identity_key(after)
    assert after["session"]["runtime_media_id"] == "event-A"
    assert after["recent_events"][-1]["runtime_media_id"] == "event-B"
    assert after["recent_events"][-1]["snapshot_runtime_media_id"] == "event-A"


async def test_v2_play_monitor_and_loss_recovery_require_fresh_live_visual_evidence(tmp_path):
    from test_hybrid_executor import play, prepared
    from test_real_executor import Device, Vision

    class V2Device(Device):
        def __init__(self):
            super().__init__()
            self.tracker = MediaTracker()
            self.sequence, self.clock, self.loss, self.loss_applied = 3, 10000, False, False

        async def runtime(self):
            self.clock += 100
            if self.loss and not self.loss_applied:
                self.sequence += 2
                self.loss_applied = True
            record = dump(seq=self.sequence, at=self.clock, playing=3 if self.playing else 0)
            if self.loss:
                record["journalHealth"].update(droppedRecords=1, writeFailures=1)
            return sample(self.tracker, record, legacy=(await self.state())[1])

    class NoLivePosition(Vision):
        async def observe(self, frame):
            result = await super().observe(frame)
            if result.player:
                result.player.live_edge = None
            return result

    device = V2Device()
    service, engine, request, row = prepared(tmp_path, device=device, vision=NoLivePosition())
    try:
        await play(engine, request, row)
        original = engine.store.report(row["token"])
        assert original["runtime"]["schema_version"] == 2 and original["observation"]["verified"]
        captures, actions = device.captures, list(device.actions)
        await engine.monitor(row)
        assert device.captures == captures  # Healthy v2 retains cheap monitoring.
        device.loss = True
        await engine.monitor(row)
        report = engine.store.report(row["token"])
        assert device.captures > captures and device.actions == actions
        assert not report["observation"]["verified"]
        assert report["runtime"]["binding"] != "visually_associated"
        assert report["runtime"]["loss_counters"]["droppedRecords"] == 1
        assert report["content_status"]["effective_state"] == "unknown"
        engine.vision = Vision()  # Now independently observe an explicit live playhead.
        await engine.monitor(row)
        assert engine.store.report(row["token"])["observation"]["verified"]
        assert device.actions == actions
    finally:
        await service.stop()
