"""Route compatibility and durable recovery of playback evidence."""

import json
import time
from datetime import UTC, datetime, timedelta

from .planner import parse_time

ROUTE_FIELDS = (
    "id",
    "app",
    "channel",
    "stream_title",
    "listing_url",
    "broadcast_id",
    "presentation",
    "coverage_type",
)


def compatible_options(issued, current, selected_id=None):
    """New alternatives/display updates do not invalidate an existing route.

    A withdrawn route or changed playback locator does. Expected ends remain estimates.
    """
    old = {o["id"]: tuple(o.get(key) for key in ROUTE_FIELDS) for o in issued}
    new = {o["id"]: tuple(o.get(key) for key in ROUTE_FIELDS) for o in current}
    if selected_id is not None:
        return selected_id in old and old[selected_id] == new.get(selected_id)
    return bool(old) and all(new.get(key) == value for key, value in old.items())


class PlaybackRecovery:
    def executor_available(self, db):
        return self.db.device(db).get("executor_health", {}).get("state") != "offline"

    def begin_observation_recovery(self, device, now, *, not_before=None, reason=None):
        content_id = device["observed"]["content_id"]
        recovery = device.get("recovery") or {}
        if recovery.get("content_id") != content_id:
            recovery = {"content_id": content_id, "attempts": 0}
        if not recovery.get("since"):
            delay = min(self.settings.playback_recovery_grace * 2 ** min(recovery.get("attempts", 0), 3), 300)
            recovery.update(
                since=now.isoformat(),
                retry_after=max(now + timedelta(seconds=delay), not_before or now).isoformat(),
                stable_since=None,
            )
        elif not_before and recovery.get("retry_after"):
            # Retained recovery state may have been created by an older version.
            recovery["retry_after"] = max(parse_time(recovery["retry_after"]), not_before).isoformat()
        device["recovery"] = recovery
        if reason:
            recovery["reason"] = reason

    def note_verified_playback(self, device, now):
        recovery = device.get("recovery")
        if not recovery:
            return
        if recovery["content_id"] != device["observed"]["content_id"]:
            device["recovery"] = None
            return
        recovery["since"] = None
        recovery["retry_after"] = None
        recovery.pop("reason", None)
        recovery["stable_since"] = recovery.get("stable_since") or now.isoformat()
        if (
            now - parse_time(recovery["stable_since"])
        ).total_seconds() >= self.settings.recovery_stable_seconds:
            device["recovery"] = None

    def refresh_playback_observation(self):
        with self.db.transaction() as db:
            if not self.owns_device(db, "living-room"):
                return
            health = self.db.device(db).get("executor_health") or {}
            probe_due = (
                not health.get("next_probe_at")
                or parse_time(health["next_probe_at"]).timestamp() <= time.time()
            )
        observation, transport_error, monitoring = None, None, {}
        if probe_due:
            try:
                if hasattr(self.playback, "observe_report"):
                    monitoring = self.playback.observe_report("living-room") or {}
                    observation = monitoring.get("observation")
                else:
                    observation = self.playback.observe("living-room")
            except Exception as exc:
                transport_error = f"{type(exc).__name__}: playback service unavailable"
        with self.db.transaction() as db:
            if not self.owns_device(db, "living-room"):
                return
            d = self.db.device(db)
            now = datetime.now(UTC)
            if probe_due:
                previous = d.get("executor_health") or {}
                if transport_error:
                    failures = previous.get("failures", 0) + 1
                    d["executor_health"] = {
                        "state": "offline",
                        "failures": failures,
                        "since": previous.get("since") or now.isoformat(),
                        "last_contact_at": previous.get("last_contact_at"),
                        "next_probe_at": (
                            now + timedelta(seconds=min(5 * 2 ** min(failures - 1, 4), 60))
                        ).isoformat(),
                        "error": transport_error,
                    }
                    if previous.get("state") != "offline":
                        self.db.log(
                            db,
                            self.now(db).isoformat(),
                            "Playback service unavailable",
                            "Navigation paused; probing with backoff. Watch plan retained.",
                            "recovery",
                        )
                else:
                    d["executor_health"] = {
                        "state": "ok",
                        "failures": 0,
                        "since": None,
                        "last_contact_at": now.isoformat(),
                        "next_probe_at": None,
                        "error": None,
                    }
                    if previous.get("state") == "offline":
                        self.db.log(
                            db,
                            self.now(db).isoformat(),
                            "Playback service recovered",
                            "Rechecking live status and current intent before navigation resumes",
                            "recovery",
                        )
            existing = d.get("observed")
            if existing:
                job = db.execute("SELECT * FROM jobs WHERE id=?", (existing.get("request_id"),)).fetchone()
                item = next((i for i in self.items(db) if i["content_id"] == existing["content_id"]), None)
                route_valid = bool(
                    job
                    and item
                    and compatible_options(
                        json.loads(job["payload"])["allowed_viewing_options"],
                        item["viewing_options"],
                        existing.get("viewing_option_id"),
                    )
                )
                if (
                    route_valid
                    and isinstance(observation, dict)
                    and observation.get("request_id") == existing.get("request_id")
                    and not self.observation_error(job, observation)
                    and parse_time(observation["observed_at"]) >= parse_time(existing["observed_at"])
                ):
                    d["observed"] = observation
                    if (
                        d.get("recovery")
                        and parse_time(existing.get("valid_until"))
                        and parse_time(existing["valid_until"]) <= now
                    ):
                        d["recovery"]["stable_since"] = None
                    if d["playback_state"] == "unverified" and d.get("desired") == existing["content_id"]:
                        d["playback_state"] = "verified"
                        self.db.log(
                            db,
                            self.now(db).isoformat(),
                            "Playback observation recovered",
                            "Fresh matching live playback observation received",
                            "verified",
                        )
                    self.note_verified_playback(d, now)
                else:
                    expiry = min(
                        parse_time(existing.get("valid_until")) or datetime.max.replace(tzinfo=UTC),
                        parse_time(existing["observed_at"])
                        + timedelta(seconds=self.settings.playback_evidence_ttl),
                    )
                    withdrawn = bool(
                        isinstance(observation, dict)
                        and observation.get("verified") is False
                        and all(
                            observation.get(k) == existing.get(k)
                            for k in ("device_id", "request_id", "intent_version", "content_id")
                        )
                        and parse_time(observation["observed_at"]) >= parse_time(existing["observed_at"])
                    )
                    if not route_valid or withdrawn or expiry <= now:
                        if not route_valid:
                            detail = "Coverage changed"
                        elif expiry <= now:
                            detail = "Playback evidence expired; waiting briefly for recovery"
                        else:
                            detail = monitoring.get("reason") or "Current playback is unverified; waiting for recovery"
                        if existing.get("verified"):
                            self.db.log(
                                db,
                                self.now(db).isoformat(),
                                "Playback requires revalidation",
                                detail,
                                "recovery",
                            )
                        existing["verified"] = False
                        if d["playback_state"] == "verified":
                            d["playback_state"] = "unverified"
                        temporary = withdrawn and monitoring.get("player_state") in {
                            "paused", "buffering", "unknown"
                        }
                        reason = detail
                        if route_valid and expiry > now:
                            if monitoring.get("player_state") == "paused":
                                reason = "Prime playback is paused; allowing time to resume"
                            elif monitoring.get("player_state") == "buffering":
                                reason = "Prime playback is buffering; allowing time to recover"
                        self.begin_observation_recovery(
                            d,
                            now,
                            not_before=expiry if route_valid and temporary else None,
                            reason=reason,
                        )
            self.db.save_device(db, d)
