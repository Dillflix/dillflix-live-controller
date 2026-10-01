"""Structured MediaSession observations, distinct from visual/event identity.

The adapter consumes the installed dev.tvprobe.mediasession service used by
capture 05. Callback payloads retain their own meaning; snapshots are composite.
No media position is interpreted as programme time, rendered frames or live lag.
"""

import hashlib
import json
import math
from collections import deque
from datetime import datetime, timedelta

from .accessibility import PRIME

PROBE = "dev.tvprobe.mediasession"
JOURNAL_TAIL_BYTES = 512 * 1024
STATES = {
    0: "none",
    1: "stopped",
    2: "paused",
    3: "playing",
    4: "fast_forwarding",
    5: "rewinding",
    6: "buffering",
    7: "error",
    8: "connecting",
    9: "skipping_previous",
    10: "skipping_next",
    11: "skipping_queue_item",
}
MEDIA_ID = "android.media.metadata.MEDIA_ID"
SOURCE_ID = "com.amazon.alexa.externalmediaplayer.metadata.PLAYBACK_SOURCE_ID"
EXTRA_ID = "com.amazon.media.MEDIA_ID"


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def string(value, limit=1024):
    return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= limit else None


def records(text):
    """Service dumps contain headers; rotating journals can end mid-record."""
    result = []
    for line in text.splitlines():
        if len(line) > 256 * 1024:
            continue
        try:
            item = json.loads(line)
        except (ValueError, TypeError):
            continue
        if (
            isinstance(item, dict)
            and isinstance(item.get("snapshot"), dict)
            and number(item.get("elapsedRealtimeMs")) is not None
            and isinstance(item["snapshot"].get("sessions"), list)
        ):
            result.append(item)
    return result


def session(value):
    metadata = value.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None
    entries = metadata.get("entries") or {}
    description = metadata.get("description") or {}
    extras = value.get("sessionExtras") or {}
    playback = value.get("playbackState") or {}
    if not all(isinstance(x, dict) for x in (metadata, entries, description, extras, playback)):
        return None
    identifiers = {
        MEDIA_ID: string(entries.get(MEDIA_ID)),
        SOURCE_ID: string(entries.get(SOURCE_ID)),
        "description.mediaId": string(description.get("mediaId")),
        EXTRA_ID: string(extras.get(EXTRA_ID)),
    }
    unique = {v for v in identifiers.values() if v}
    raw_state = playback.get("state")
    return {
        "package": string(value.get("package"), 128),
        "session_token": string(value.get("sessionToken"), 256),
        "runtime_media_id": next(iter(unique)) if len(unique) == 1 else None,
        "identity_status": "consistent" if len(unique) == 1 else "conflicting" if unique else "missing",
        "runtime_identifiers": identifiers,
        "display_title": string(entries.get("android.media.metadata.DISPLAY_TITLE")),
        "description_title": string(description.get("title")),
        "app_content_id": string(extras.get("com.amazon.media.CONTENT_ID")),
        "state": raw_state if type(raw_state) is int else None,
        "transport": STATES.get(raw_state, "unknown") if type(raw_state) is int else "unknown",
        "position_ms": number(playback.get("positionMs")),
        "updated_elapsed_ms": number(playback.get("updatedElapsedRealtimeMs")),
        "speed": number(playback.get("speed")),
        "duration_ms": number(entries.get("android.media.metadata.DURATION")),
        "buffered_position_ms": number(playback.get("bufferedPositionMs")),
        "error": string(playback.get("errorMessage")),
    }


def normalize(record):
    snapshot = record["snapshot"]
    values = [session(s) for s in snapshot["sessions"][:100] if isinstance(s, dict)]
    prime = [s for s in values if s and s["package"] == PRIME]
    return {
        "source": "media_probe",
        "source_health": "fresh" if snapshot.get("listenerConnected") is True else "disconnected",
        "snapshot_atomic": False,
        "device_elapsed_ms": record["elapsedRealtimeMs"],
        "sequence": record.get("sequence"),
        "session": prime[0] if len(prime) == 1 else None,
        "session_count": len(prime),
    }


def identity_key(sample):
    s = (sample or {}).get("session")
    if (
        not s
        or sample.get("source_health") != "fresh"
        or s.get("identity_status") != "consistent"
        or not s.get("session_token")
        or not sample.get("boot_id")
        or not sample.get("probe_instance")
    ):
        return None
    return (
        sample["boot_id"],
        sample["probe_instance"],
        s["session_token"],
        s["runtime_media_id"],
        sample.get("identity_revision", 0),
    )


def reported_playing(sample):
    return bool(
        identity_key(sample)
        and sample.get("foreground") == PRIME
        and sample["session"]["transport"] == "playing"
        and sample.get("active_confirmed")
    )


class MediaTracker:
    """Bounded callback history and session identity revision across polling."""

    def __init__(self):
        self.boot_id, self.latest_elapsed = None, None
        self.probe_instance = None
        self.last_sequence = None
        self.identity_revision, self.last_key = 0, None
        self.seen, self.events = deque(maxlen=2048), deque(maxlen=24)
        self.last_sample = None

    def update(
        self,
        record,
        history,
        *,
        boot_id,
        probe_instance,
        started_at,
        finished_at,
        foreground,
        legacy,
        device_before_ms,
        elapsed_seconds,
        max_age=10,
        history_available=True,
    ):
        sample = normalize(record)
        at = sample["device_elapsed_ms"]
        restarted = (
            self.boot_id != boot_id
            or self.probe_instance != probe_instance
            or (self.latest_elapsed is not None and at < self.latest_elapsed)
        )
        if restarted:
            self.identity_revision += 1
            self.seen.clear()
            self.events.clear()
            self.last_key = self.last_sample = None
            self.last_sequence = None
        self.boot_id = boot_id
        self.probe_instance = probe_instance
        # Compare elapsedRealtime to this device's boot clock, never UTC. The
        # finite command interval bounds how old/future the service dump can be.
        age_ms = (
            max(0, device_before_ms + elapsed_seconds * 1000 - at) if device_before_ms is not None else None
        )
        if (
            device_before_ms is None
            or not device_before_ms - max_age * 1000
            <= at
            <= (device_before_ms + elapsed_seconds * 1000 + 1000)
            or age_ms > max_age * 1000
        ):
            sample["source_health"] = "stale"
        previous_elapsed = None if restarted else self.latest_elapsed
        known = set(self.seen)
        history_gap = False
        for event in sorted(history, key=lambda r: r["elapsedRealtimeMs"]):
            timestamp = event["elapsedRealtimeMs"]
            if timestamp > at or (previous_elapsed is not None and timestamp < previous_elapsed):
                continue
            digest = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
            if digest in known:
                continue
            known.add(digest)
            self.seen.append(digest)
            sequence = event.get("sequence")
            if type(sequence) is int and sequence > 0:
                if self.last_sequence is not None and sequence != self.last_sequence + 1:
                    self.identity_revision += 1
                    history_gap = True
                self.last_sequence = sequence
            reason = event.get("reason")
            if reason in {"connected", "disconnected", "connect_error", "poll_error", "session_destroyed"}:
                self.identity_revision += 1
            observed = normalize(event).get("session")
            if not observed:
                if self.last_key is not None:
                    self.identity_revision += 1
                    self.last_key = None
                continue
            key = (observed["session_token"], observed["runtime_media_id"], observed["identity_status"])
            if self.last_key is not None and self.last_key != key:
                self.identity_revision += 1
            self.last_key = key
            payload = event.get("eventPayload") or {}
            if not isinstance(payload, dict):
                continue
            if reason in {
                "metadata_changed",
                "extras_changed",
                "playback_state_changed",
                "session_destroyed",
            }:
                value = payload.get("value") or {}
                if not isinstance(value, dict):
                    continue
                if payload.get("sessionToken") != observed["session_token"]:
                    continue
                raw_state = value.get("state") if reason == "playback_state_changed" else None
                if type(raw_state) is int and raw_state not in {3, 6, 8}:
                    # A pause/seek/stop between polls withdraws the live-mode
                    # association even if the next snapshot is PLAYING again.
                    self.identity_revision += 1
                self.events.append(
                    {
                        "reason": reason,
                        "device_elapsed_ms": timestamp,
                        "session_token": observed["session_token"],
                        "transport": STATES.get(raw_state, "unknown") if type(raw_state) is int else None,
                        "runtime_media_id": observed["runtime_media_id"],
                        "source": "callback_payload"
                        if reason == "playback_state_changed"
                        else "nearby_snapshot",
                    }
                )
        s = sample["session"]
        key = (s["session_token"], s["runtime_media_id"], s["identity_status"]) if s else None
        if self.last_key is not None and self.last_key != key:
            self.identity_revision += 1
        self.last_key = key
        active = [v for v in legacy if v.get("package") == PRIME and v.get("active")]
        sample.update(
            boot_id=boot_id,
            probe_instance=probe_instance,
            identity_revision=self.identity_revision,
            started_at=started_at,
            observed_at=(
                datetime.fromisoformat(finished_at) - timedelta(milliseconds=age_ms or 0)
            ).isoformat(),
            received_at=finished_at,
            foreground=foreground,
            active_confirmed=bool(s and len(active) == 1 and active[0].get("state") == s["state"]),
            recent_events=list(self.events),
            history_available=history_available,
            history_gap=history_gap,
            position_meaning="application_reported_or_extrapolated_not_programme_time",
            live_edge="unmeasured",
        )
        # Unknown/missing history cannot establish uninterrupted identity through
        # a long outage. Force a fresh visual association when sampling resumes.
        timeline = sorted(
            {
                r["elapsedRealtimeMs"]
                for r in history
                if previous_elapsed is not None and previous_elapsed <= r["elapsedRealtimeMs"] <= at
            }
        )
        covered = bool(
            history_available
            and timeline
            and previous_elapsed is not None
            and max(b - a for a, b in zip([previous_elapsed, *timeline], [*timeline, at])) <= max_age * 1000
        )
        discontinuity = False
        previous = (self.last_sample or {}).get("session")
        if (
            previous
            and s
            and previous["runtime_media_id"] == s["runtime_media_id"]
            and previous_elapsed is not None
        ):
            a, b = previous["position_ms"], s["position_ms"]
            if a is not None and b is not None and min(a, b) >= 0:
                discontinuity = b - a < -2000 or b - a > (at - previous_elapsed) * 3 + 10000
        sample["position_discontinuity"] = discontinuity
        if discontinuity or (
            previous_elapsed is not None and at - previous_elapsed > max_age * 1000 and not covered
        ):
            self.identity_revision += 1
            sample["identity_revision"] = self.identity_revision
        self.latest_elapsed, self.last_sample = at, sample
        return sample
