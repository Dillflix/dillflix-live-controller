"""A visual event association is scoped to one device boot/session/media epoch."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..planner import parse_time
from .media import identity_key, reported_playing


@dataclass
class Binding:
    key: tuple
    option_id: str
    sample: tuple
    live_mode: str

    def current(self, runtime, max_visual_age):
        return bool(
            identity_key(runtime) == self.key
            and 0 <= (datetime.now(UTC) - self.sample[0].captured_at).total_seconds() < max_visual_age
        )


def can_bind(sample, runtime):
    frame, _ = sample
    return bool(
        frame.runtime
        and frame.runtime.get("capture_association") == "unchanged"
        and identity_key(frame.runtime) == identity_key(runtime)
        and reported_playing(runtime)
    )


def status(runtime, binding, content_id, ttl):
    if not runtime:
        return None
    session = runtime.get("session") or {}
    bound = binding is not None and identity_key(runtime) == binding.key
    observed_at = parse_time(runtime["observed_at"])
    return {
        "source": runtime["source"],
        "source_health": runtime["source_health"],
        "observed_at": runtime["observed_at"],
        "valid_until": (observed_at + timedelta(seconds=ttl)).isoformat(),
        "foreground": runtime.get("foreground"),
        "session_token": session.get("session_token"),
        "runtime_media_id": session.get("runtime_media_id"),
        "boot_id": runtime.get("boot_id"),
        "probe_instance": runtime.get("probe_instance"),
        "schema_version": runtime.get("schema_version"),
        "probe_build": runtime.get("probe_build"),
        "service_instance_id": runtime.get("service_instance_id"),
        "connection_epoch": runtime.get("connection_epoch"),
        "session_instance_id": session.get("session_instance_id"),
        "collection_health": runtime.get("collection_health"),
        "journal_health": runtime.get("journal_health"),
        "history_status": runtime.get("history_status"),
        "latest_produced_sequence": runtime.get("latest_produced_sequence"),
        "latest_written_sequence": runtime.get("latest_written_sequence"),
        "loss_counters": runtime.get("loss_counters", {}),
        "problems": runtime.get("problems", []),
        "identity_revision": runtime.get("identity_revision"),
        "transport": session.get("transport", "unknown"),
        "binding": "visually_associated" if bound else "unbound",
        "bound_content_id": content_id if bound else None,
        "last_visual_at": binding.sample[0].captured_at.isoformat() if bound else None,
        "live_mode": binding.live_mode if bound else "unknown",
        "live_edge": "unmeasured",
        "position_ms": session.get("position_ms"),
        "position_meaning": "application_reported_or_extrapolated_not_programme_time",
        "history_available": bool(runtime.get("history_available")),
        "history_gap": bool(runtime.get("history_gap")),
        "recent_events": runtime.get("recent_events", [])[-24:],
    }
