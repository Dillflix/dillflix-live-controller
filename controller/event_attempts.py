"""Durable per-event playback limits, independent of network/session caches."""

CONCLUSIVE_FAILURES = frozenset({"feeds_unavailable", "feeds_locked", "no_matching_feed"})
DEFAULT_ATTEMPT_LIMIT = 2


def attempt_status(device, content_id):
    record = device.get("event_attempts", {}).get(content_id)
    if not record:
        return None
    limit = device["preferences"].get("event_attempt_limit", DEFAULT_ATTEMPT_LIMIT)
    exhausted = record["attempts"] >= limit
    return {
        **record, "limit": limit, "exhausted": exhausted,
        "message": (
            f"Automatic attempts stopped after {record['attempts']} unsuccessful cycles. "
            "Watch plan retained; use Play now to try again."
        ) if exhausted else f"{record['attempts']} of {limit} unsuccessful playback cycles",
    }


def record_failure(device, content_id, result_id, state, observed_at):
    """Call only for accepted, complete, error-free selection outcomes."""
    if state not in CONCLUSIVE_FAILURES:
        return
    records = device.setdefault("event_attempts", {})
    previous = records.get(content_id, {})
    if previous.get("result_id") == result_id:
        return
    records[content_id] = {
        "attempts": previous.get("attempts", 0) + 1,
        "result_id": result_id, "state": state, "observed_at": observed_at,
    }


def exhausted(device, content_id):
    return bool((attempt_status(device, content_id) or {}).get("exhausted"))
