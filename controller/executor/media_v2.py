"""Strict consumption of probe v2 dumps and instance-scoped journal intervals.

Current acquisition, collector health, writer health and exported continuity are
separate facts. A new visual association starts at an explicit sequence boundary;
it never retroactively makes older lost observations complete.
"""

from collections import deque
from datetime import datetime, timedelta
from uuid import UUID

from .accessibility import PRIME

LOSS_COUNTERS = (
    "droppedRecords",
    "writeFailures",
    "rotationFailures",
    "oversizedRecords",
    "lossRangeDetailsEvicted",
)
OPERATIONS = ("activeRegistration", "callbackRegistration", "sessionReads", "poll", "cleanup")


def advertised(record):
    return any(k in record for k in ("schemaVersion", "serviceInstanceId", "probeBuild", "collectionHealth"))


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def integer(value, minimum=0):
    require(type(value) is int and value >= minimum, "invalid_integer")
    return value


def stamp(value):
    require(isinstance(value, dict), "missing_acquisition_time")
    return integer(value.get("elapsedRealtimeMs"))


def incomplete(value):
    if isinstance(value, dict):
        if (
            any(k in value for k in ("readError", "decodeError", "_bundleReadError"))
            or value.get("omitted") is True
            or value.get("truncated") is True
        ):
            return True
        return any(incomplete(v) for v in value.values())
    return isinstance(value, list) and any(incomplete(v) for v in value)


def identifier(value):
    require(isinstance(value, str) and 0 < len(value) <= 128, "invalid_identity")
    return value


def uuid(value):
    value = identifier(value)
    require(str(UUID(value)) == value.lower(), "invalid_service_identity")
    return value.lower()


def checkpoint(value, instance):
    require(
        isinstance(value, dict) and value.get("serviceInstanceId") == instance, "invalid_checkpoint_identity"
    )
    produced = integer(value.get("latestProducedSequence"))
    written = integer(value.get("latestWrittenSequence"))
    require(written <= produced, "invalid_checkpoint_order")
    return produced, written, stamp(value.get("capturedAt"))


def ranges(value, *, losses=False):
    require(isinstance(value, list) and len(value) <= 64, "invalid_sequence_ranges")
    result = []
    for item in value:
        require(isinstance(item, dict), "invalid_sequence_range")
        first, last = integer(item.get("firstSequence"), 1), integer(item.get("lastSequence"), 1)
        require(first <= last, "invalid_sequence_range")
        result.append((uuid(item.get("serviceInstanceId")), first, last))
        if losses:
            identifier(item.get("reason"))
    return result


def session_ids(snapshot, epoch):
    require(
        isinstance(snapshot, dict)
        and snapshot.get("complete") is True
        and isinstance(snapshot.get("sessions"), list),
        "snapshot_incomplete",
    )
    require(snapshot.get("listenerConnected") is True, "listener_disconnected")
    start, end = stamp(snapshot.get("acquisitionStart")), stamp(snapshot.get("acquisitionEnd"))
    sessions = snapshot["sessions"]
    require(len(sessions) <= 16, "too_many_sessions")
    ids = set()
    for row in sessions:
        require(isinstance(row, dict), "invalid_session")
        sid = identifier(row.get("sessionInstanceId"))
        require(
            sid not in ids
            and row.get("package") == PRIME
            and integer(row.get("connectionEpoch"), 1) == epoch,
            "invalid_session_identity",
        )
        ids.add(sid)
        require(row.get("readSucceeded") is True and row.get("dataComplete") is True, "session_incomplete")
        require(
            row.get("readErrors") == {} and integer(row.get("nestedReadErrors")) == 0, "session_read_error"
        )
        serialization = row.get("serialization")
        require(
            isinstance(serialization, dict)
            and serialization.get("complete") is True
            and serialization.get("omissions") == 0,
            "session_truncated",
        )
        require(
            start <= stamp(row.get("acquisitionStart")) <= stamp(row.get("acquisitionEnd")) <= end,
            "invalid_session_time",
        )
    return ids


def current_dump(record, max_age):
    """Validate the advertised contract, including original acquisition times."""
    require(
        type(record.get("schemaVersion")) is int and record["schemaVersion"] == 2, "unsupported_probe_schema"
    )
    instance, epoch = uuid(record.get("serviceInstanceId")), integer(record.get("connectionEpoch"), 1)
    identifier(record.get("probeBuild"))
    for key in ("snapshotIsCached", "dumpTimedOut", "dumpFailed", "snapshotAtomic"):
        require(record.get(key) is False, "dump_not_current")
    require(record.get("recordTruncated", False) is False, "dump_truncated")
    snapshot, health, journal = (
        record.get("snapshot"),
        record.get("collectionHealth"),
        record.get("journalHealth"),
    )
    require(all(isinstance(v, dict) for v in (snapshot, health, journal)), "missing_probe_health")
    require(
        snapshot.get("complete") is True and isinstance(snapshot.get("sessions"), list), "snapshot_incomplete"
    )
    created, connected = stamp(record.get("serviceCreatedAt")), stamp(record.get("connectionStartedAt"))
    start, end = stamp(snapshot.get("acquisitionStart")), stamp(snapshot.get("acquisitionEnd"))
    now, requested = stamp(record.get("dumpResponseAt")), stamp(record.get("dumpRequestedAt"))
    observed = integer(record.get("elapsedRealtimeMs"))
    before, after = (
        checkpoint(record.get(k), instance) for k in ("checkpointBeforeSnapshot", "checkpointAfterSnapshot")
    )
    require(created <= connected <= start <= end <= observed <= after[2] <= now, "invalid_acquisition_order")
    require(
        requested <= before[2] <= start and before[0] <= after[0] and before[1] <= after[1],
        "invalid_checkpoint_order",
    )
    require(now - start <= max_age * 1000, "snapshot_stale")
    require(snapshot.get("listenerConnected") is True, "listener_disconnected")
    for key in (
        "listenerConnected",
        "activeSessionsListenerRegistered",
        "callbacksRegistered",
        "pollExpected",
    ):
        require(health.get(key) is True, "collection_" + key)
    for key in ("lastSuccessfulSessionReadElapsedMs", "lastPollSuccessElapsedMs"):
        at = integer(health.get(key))
        require(connected <= at <= now and now - at <= min(max_age, 5) * 1000, "collection_stale")
    require(integer(health.get("pollOverdueMs")) <= 5000, "poll_overdue")
    counters = {}
    for key in OPERATIONS:
        operation = health.get(key)
        require(isinstance(operation, dict), "missing_operation_health")
        counters[key] = integer(operation.get("failures"))
        if key in ("sessionReads", "poll", "activeRegistration"):
            require(
                operation.get("lastAttemptSucceeded") is True and operation.get("inProgress") is False,
                "collection_" + key,
            )
    ids = session_ids(snapshot, epoch)
    registrations = health.get("sessionRegistrations")
    require(isinstance(registrations, list) and len(registrations) == len(ids), "registration_incomplete")
    require(
        all(isinstance(r, dict) and r.get("registrationCallSucceeded") is True for r in registrations),
        "registration_failed",
    )
    require({r.get("sessionInstanceId") for r in registrations} == ids, "registration_identity_mismatch")
    produced, written = (
        integer(journal.get("latestProducedSequence"), 1),
        integer(journal.get("latestWrittenSequence")),
    )
    require(
        produced == after[0] == record.get("latestProducedSequence")
        and after[1] == record.get("latestWrittenSequence")
        and after[1] <= written <= produced,
        "invalid_checkpoint_order",
    )
    for key in LOSS_COUNTERS:
        counters[key] = integer(journal.get(key))
    loss_ranges = ranges(journal.get("lossRanges"), losses=True)
    require(all(i == instance and last <= produced for i, _, last in loss_ranges), "invalid_loss_identity")
    require(
        sum(last - first + 1 for _, first, last in loss_ranges) <= counters["droppedRecords"],
        "invalid_loss_count",
    )
    retention = journal.get("retention")
    require(isinstance(retention, dict) and retention.get("initialized") is True, "retention_unavailable")
    retained = []
    for key in ("current", "previous"):
        file = retention.get(key)
        require(isinstance(file, dict) and type(file.get("rangesComplete")) is bool, "invalid_retention")
        for count in ("evictedRanges", "unattributedRecords", "invalidRecords"):
            integer(file.get(count))
        retained.extend(ranges(file.get("ranges")))
    for key in ("initialized", "writerAlive", "writerClosing", "lastWriteSucceeded"):
        require(type(journal.get(key)) is bool, "invalid_writer_health")
    depth, flight, age = (
        integer(journal.get(k)) for k in ("queueDepth", "inFlightSequence", "oldestPendingAgeMs")
    )
    capacity = integer(journal.get("queueCapacity"), 1)
    require(depth <= capacity <= 64 and flight <= produced, "invalid_writer_queue")
    writer = "healthy"
    if not journal["initialized"] or not journal["writerAlive"] or journal["writerClosing"]:
        writer = "unavailable"
    elif age > 5000:
        writer = "stalled"
    elif written != produced or depth or flight:
        writer = "pending"
    elif not journal["lastWriteSucceeded"]:
        writer = "failed"
    return instance, epoch, start, now, produced, written, counters, writer, retained, loss_ranges


class MediaV2Tracker:
    def __init__(self, read_session, states):
        self.read_session, self.states = read_session, states
        self.epoch = self.cursor = self.counters = self.last = None
        self.revision = 0
        self.events = deque(maxlen=24)

    def update(
        self,
        record,
        history,
        *,
        boot_id,
        started_at,
        finished_at,
        foreground,
        legacy,
        device_before_ms,
        elapsed_seconds,
        max_age=10,
        history_available=True,
        acquisition_before=None,
    ):
        sample = dict(
            source="media_probe",
            schema_version=2,
            source_health="invalid",
            source_error=None,
            snapshot_atomic=False,
            session=None,
            session_count=0,
            boot_id=boot_id,
            probe_instance=None,
            started_at=started_at,
            observed_at=finished_at,
            foreground=foreground,
            active_confirmed=False,
            history_available=history_available,
            history_gap=True,
            history_status="unavailable",
            continuity_ready=False,
            collection_health="invalid",
            journal_health="unknown",
            problems=[],
            recent_events=[],
            live_edge="unmeasured",
        )
        try:
            instance, epoch, at, now, produced, written, counters, writer, retained, losses = current_dump(
                record, max_age
            )
            require(
                device_before_ms is not None
                and device_before_ms - max_age * 1000
                <= at
                <= now
                <= device_before_ms + elapsed_seconds * 1000 + 1000,
                "device_clock_mismatch",
            )
            age_ms = max(0, device_before_ms + elapsed_seconds * 1000 - at)
            require(age_ms <= max_age * 1000, "snapshot_stale")
            if acquisition_before is not None:
                require(
                    acquisition_before.get("schemaVersion") == 2
                    and acquisition_before.get("serviceInstanceId") == instance
                    and acquisition_before.get("connectionEpoch") == epoch,
                    "probe_changed_during_acquisition",
                )
            key = (boot_id, instance, epoch)
            same_service = self.epoch is not None and self.epoch[:2] == key[:2]
            if same_service:
                require(
                    produced >= self.cursor and all(counters[k] >= self.counters[k] for k in counters),
                    "probe_checkpoint_regressed",
                )
            baseline = self.epoch != key
            if baseline:
                self.revision += 1
                self.events.clear()
                self.last = None
            start = produced if baseline else self.cursor + 1
            problems = []
            if same_service and counters != self.counters:
                problems.append("observation_loss_or_collection_failure")
            if writer != "healthy":
                problems.append("journal_" + writer)
            if not history_available:
                problems.append("journal_export_unavailable")
            rows = {}
            for row in history:
                if row.get("serviceInstanceId") != instance:
                    continue  # Legacy and previous service records cannot fill this interval.
                seq = row.get("sequence")
                if type(seq) is not int or seq < 1:
                    problems.append("invalid_journal_sequence")
                    continue
                if seq > produced:
                    problems.append("journal_newer_than_snapshot")
                    continue
                if seq < start:
                    continue
                if seq in rows and rows[seq] != row:
                    problems.append("conflicting_journal_duplicate")
                rows[seq] = row
            # Bounded arithmetic/count checks: never allocate a range supplied by the probe.
            if len(rows) != max(0, produced - start + 1):
                problems.append("journal_sequence_gap")
            previous_key = self.session_key((self.last or {}).get("session"))
            for seq, row in sorted(rows.items()):
                try:
                    require(
                        type(row.get("schemaVersion")) is int
                        and row["schemaVersion"] == 2
                        and row.get("connectionEpoch") == epoch,
                        "invalid_journal_identity",
                    )
                    require(row.get("recordTruncated", False) is False, "journal_record_truncated")
                    require(
                        stamp(record["connectionStartedAt"]) <= integer(row.get("elapsedRealtimeMs")) <= now,
                        "invalid_journal_time",
                    )
                    require(
                        any(i == instance and lo <= seq <= hi for i, lo, hi in retained),
                        "journal_not_retained_at_checkpoint",
                    )
                    require(not any(lo <= seq <= hi for _, lo, hi in losses), "journal_reported_lost")
                    current = row.get("snapshot")
                    require(
                        isinstance(current, dict)
                        and current.get("complete") is True
                        and isinstance(current.get("sessions"), list),
                        "journal_snapshot_incomplete",
                    )
                    session_ids(current, epoch)
                    observed = self.one_session(current["sessions"])
                    next_key = self.session_key(observed)
                    if not baseline and next_key != previous_key:
                        self.revision += 1
                    previous_key = next_key
                    self.event(row, observed)
                except (ValueError, TypeError, KeyError, AttributeError) as error:
                    problems.append(str(error) if isinstance(error, ValueError) else "invalid_journal_record")
            current = self.one_session(record["snapshot"]["sessions"])
            if not baseline and previous_key != self.session_key(current):
                self.revision += 1
            # Preserve the original seek/discontinuity boundary without treating
            # an application position as rendered-frame or live-edge evidence.
            previous = (self.last or {}).get("session")
            if previous and current and self.session_key(previous) == self.session_key(current):
                a, b = previous["position_ms"], current["position_ms"]
                if a is not None and b is not None and min(a, b) >= 0:
                    if b - a < -2000 or b - a > (at - self.last["device_elapsed_ms"]) * 3 + 10000:
                        self.revision += 1
            if self.last and at - self.last["device_elapsed_ms"] > max_age * 1000:
                self.revision += 1  # Reacquire visual identity after a long observation outage.
            problems = list(dict.fromkeys(problems))[:24]
            if problems:
                self.revision += 1
            active = [s for s in legacy if s.get("package") == PRIME and s.get("active")]
            sample.update(
                source_health="fresh",
                collection_health="fresh",
                journal_health=writer,
                service_instance_id=instance,
                connection_epoch=epoch,
                probe_build=record["probeBuild"],
                device_elapsed_ms=at,
                observed_at=(
                    datetime.fromisoformat(finished_at) - timedelta(milliseconds=age_ms)
                ).isoformat(),
                session=current,
                session_count=len(record["snapshot"]["sessions"]),
                active_confirmed=bool(
                    current and len(active) == 1 and active[0].get("state") == current["state"]
                ),
                latest_produced_sequence=produced,
                latest_written_sequence=written,
                loss_counters={k: counters[k] for k in LOSS_COUNTERS},
                problems=problems,
                history_gap=bool(problems),
                continuity_ready=not problems,
                history_status="incomplete" if problems else "baseline" if baseline else "covered",
                recent_events=list(self.events),
            )
            self.epoch, self.cursor, self.counters, self.last = key, produced, counters, sample
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            self.revision += 1
            sample["source_error"] = str(error) if isinstance(error, ValueError) else "invalid_probe_schema"
            sample["problems"] = [sample["source_error"]]
        sample["identity_revision"] = self.revision
        return sample

    def one_session(self, rows):
        if len(rows) != 1 or not isinstance(rows[0], dict):
            return None
        result = self.read_session(rows[0])
        if result:
            result["session_instance_id"] = identifier(rows[0].get("sessionInstanceId"))
        return result

    @staticmethod
    def session_key(session):
        return (
            (session["session_instance_id"], session["runtime_media_id"], session["identity_status"])
            if session
            else None
        )

    def event(self, row, observed):
        reason, payload = row.get("reason"), row.get("eventPayload")
        if reason in {
            "connected",
            "disconnected",
            "collector_error",
            "poll_error",
            "session_destroyed",
            "service_destroyed",
            "session_removed",
        }:
            self.revision += 1
        if reason == "session_removed":
            require(
                isinstance(payload, dict)
                and payload.get("historical") is True
                and payload.get("eventCompletionInferred") is False,
                "invalid_removal_evidence",
            )
            historical = payload.get("lastKnownSnapshot")
            removed_at = stamp(payload.get("removalObservedAt"))
            require(
                payload.get("serviceInstanceId") == row.get("serviceInstanceId"), "invalid_removal_identity"
            )
            if isinstance(historical, dict):
                require(
                    historical.get("sessionInstanceId") == payload.get("sessionInstanceId")
                    and stamp(historical.get("acquisitionEnd")) <= removed_at,
                    "invalid_historical_snapshot",
                )
            summary = self.read_session(historical) if isinstance(historical, dict) else None
            self.events.append(
                dict(
                    reason=reason,
                    session_instance_id=identifier(payload.get("sessionInstanceId")),
                    historical=True,
                    event_completion_inferred=False,
                    device_elapsed_ms=removed_at,
                    last_known=summary,
                    last_known_acquisition=historical.get("acquisitionEnd") if summary else None,
                )
            )
        elif reason in {"metadata_changed", "extras_changed", "playback_state_changed"}:
            require(
                isinstance(payload, dict) and payload.get("historical") is False, "invalid_callback_payload"
            )
            require(
                isinstance(payload.get("serialization"), dict)
                and payload["serialization"].get("complete") is True,
                "callback_truncated",
            )
            sid = identifier(payload.get("sessionInstanceId"))
            value = payload.get("value")
            require(value is None or isinstance(value, dict), "invalid_callback_value")
            require(
                value is None
                or not any(k in value for k in ("readError", "decodeError", "_bundleReadError")),
                "callback_read_error",
            )
            require(not incomplete(value), "callback_incomplete")
            received = stamp(row.get("callbackReceivedAt"))
            require(
                stamp(row.get("connectionStartedAt"))
                <= received
                <= stamp(row["snapshot"].get("acquisitionStart")),
                "invalid_callback_time",
            )
            state = value.get("state") if reason == "playback_state_changed" and value else None
            if reason == "playback_state_changed" and (type(state) is not int or state not in {3, 6, 8}):
                self.revision += 1
            same_session = observed is not None and sid == observed["session_instance_id"]
            callback_identity = None
            if reason in {"metadata_changed", "extras_changed"}:
                field = "metadata" if reason == "metadata_changed" else "sessionExtras"
                callback_identity = self.read_session({field: value})
                if (
                    not same_session
                    or callback_identity is None
                    or callback_identity["identity_status"] != "consistent"
                    or callback_identity["runtime_media_id"] != observed["runtime_media_id"]
                ):
                    self.revision += 1
            self.events.append(
                dict(
                    reason=reason,
                    session_instance_id=sid,
                    historical=False,
                    device_elapsed_ms=received,
                    transport=self.states.get(state, "unknown") if type(state) is int else None,
                    source="callback_payload",
                    runtime_media_id=(callback_identity or {}).get("runtime_media_id"),
                    snapshot_runtime_media_id=observed.get("runtime_media_id") if same_session else None,
                )
            )
