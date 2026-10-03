"""Content lifecycle evidence, independent of discovery and playback verification."""

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .database import encode
from .planner import parse_time

STATES = {"scheduled", "live", "ended", "cancelled", "delayed", "suspended", "postponed", "unknown"}
TERMINAL = {"ended", "cancelled"}
log = logging.getLogger(__name__)


class InvalidStatus(ValueError):
    """A safe public validation reason, without arbitrary adapter payload text."""


def normalized_state(value):
    value = "ended" if value == "final" else value
    return value if isinstance(value, str) and value in STATES else "unknown"


def source_observation(snapshot, seen_at, as_of, now, mode, ttl):
    """Only fixtures have a simulated clock; feed reads do not create provider timestamps."""
    if mode == "demo":
        controls = snapshot.get("_simulation", {})
        if controls.get("status_error"):
            raise ConnectionError("Simulated status lookup unavailable")
        if controls.get("status_missing"):
            return None
        state = controls.get("override")
        if not state:
            actual_end = parse_time(controls.get("actual_end_time"))
            state = (
                "scheduled"
                if as_of < parse_time(snapshot["start_time"])
                else "ended"
                if actual_end and as_of >= actual_end
                else "live"
            )
        observed_at, received_at, basis = now.isoformat(), now.isoformat(), "fixture"
        source = "fixture_simulator"
    else:
        state = snapshot.get("status", "unknown")
        observed_at, received_at, basis = None, seen_at, "feed_received"
        if snapshot.get("status_received_at"):
            # An upstream cache hit must not renew an old live observation.
            received_at = min(parse_time(seen_at), parse_time(snapshot["status_received_at"])).isoformat()
        source = "feed_status_simulator"
    return {
        "content_id": snapshot["id"],
        "state": normalized_state(state),
        "observed_at": observed_at,
        "received_at": received_at,
        "valid_until": (parse_time(observed_at or received_at) + timedelta(seconds=ttl)).isoformat(),
        "timestamp_basis": basis,
        "source": source,
        "simulated": True,
    }


class ContentStatusAdapter(Protocol):
    async def lookup(self, request: dict) -> dict | None: ...


class SimulatedContentStatusAdapter:
    def __init__(self, mode, ttl):
        self.mode, self.ttl = mode, ttl

    async def lookup(self, request):
        observation = source_observation(
            request["content_snapshot"],
            request["catalog_seen_at"],
            parse_time(request["as_of"]),
            datetime.now(UTC),
            self.mode,
            self.ttl,
        )
        if observation is None:
            return None
        return {
            "schema_version": 1,
            "request_id": request["request_id"],
            "content_id": request["content_id"],
            "observation": observation,
        }


def observation_time(observation):
    return parse_time(observation.get("observed_at") or observation.get("received_at"))


def observation_expiry(observation, ttl):
    return min(parse_time(observation["valid_until"]), observation_time(observation) + timedelta(seconds=ttl))


def validate_report(request, report, now, ttl):
    if not isinstance(report, dict) or any(
        report.get(key) != request[key] for key in ("schema_version", "request_id", "content_id")
    ):
        raise InvalidStatus("Status response identity does not match its request")
    observation = report.get("observation")
    if not isinstance(observation, dict) or observation.get("content_id") != request["content_id"]:
        raise InvalidStatus("Status observation identity does not match its request")
    if observation.get("state") not in STATES:
        raise InvalidStatus("Status observation has an unsupported lifecycle state")
    if (
        not isinstance(observation.get("source"), str)
        or not observation["source"].strip()
        or type(observation.get("simulated")) is not bool
        or observation.get("timestamp_basis")
        not in {"provider", "fixture", "feed_received", "device_observed"}
    ):
        raise InvalidStatus("Status observation is missing its source or timestamp basis")
    if observation["timestamp_basis"] == "device_observed":
        evidence = observation.get("evidence") or {}
        if (
            observation["simulated"]
            or not evidence.get("evidence_id")
            or evidence.get("method") != "device_observation"
        ):
            raise InvalidStatus("Device status requires real acquired evidence")
        if observation["state"] in TERMINAL and evidence.get("decision") != "confirmed":
            raise InvalidStatus("Device completion requires explicitly confirmed evidence")
    if observation["timestamp_basis"] == "feed_received":
        if observation.get("observed_at") is not None:
            raise InvalidStatus("Feed receipt time must not be claimed as provider observation time")
    elif not observation.get("observed_at"):
        raise InvalidStatus("Status observation requires an observation timestamp")
    observed = observation_time(observation)
    until = parse_time(observation.get("valid_until"))
    if not observed or not until or observed > now + timedelta(seconds=5) or until <= observed:
        raise InvalidStatus("Status observation has invalid timing")
    if observation_expiry(observation, ttl) <= now:
        raise InvalidStatus("Status observation expired before acceptance")
    encode(observation)  # Reject non-JSON/NaN evidence before persistence.
    return observation


def lifecycle_view(observation, check, now, ttl):
    state = observation["state"] if observation else "unknown"
    expired = observation is None or observation_expiry(observation, ttl) <= now
    # Terminal evidence remains a known fact through outages. A newer explicit
    # non-unknown observation can correct it; a missing/error response cannot.
    effective = "unknown" if expired and state not in TERMINAL else state
    return {
        "state": effective,
        "last_known_state": state,
        "stale": expired,
        "observed_at": observation.get("observed_at") if observation else None,
        "received_at": observation.get("received_at") if observation else None,
        "valid_until": observation.get("valid_until") if observation else None,
        "effective_valid_until": observation_expiry(observation, ttl).isoformat() if observation else None,
        "source": observation.get("source") if observation else None,
        "timestamp_basis": observation.get("timestamp_basis") if observation else None,
        "simulated": observation.get("simulated", True) if observation else True,
        "refresh": {
            "state": "error" if check and check["error"] else "starting" if not check else "ok",
            "last_attempt": check["last_attempt"] if check else None,
            "last_success": check["last_success"] if check else None,
            "next_check_at": datetime.fromtimestamp(check["next_check"], UTC).isoformat() if check else None,
            "error": check["error"] if check else None,
        },
    }


class ContentStatusCoordinator:
    @staticmethod
    def pinned_content_ids(db, *, device_id=None, for_lookup=False):
        ids = set()
        for row in db.execute("SELECT id,payload FROM devices"):
            if device_id is not None and row["id"] != device_id:
                continue
            device = json.loads(row["payload"])
            pins = {entry["content_id"] for entry in device["plan"]}
            pins.update((device.get("desired"), (device.get("observed") or {}).get("content_id")))
            if for_lookup:
                pins.difference_update(device.get("manual_completions", {}))
            ids.update(pins)
        return ids - {None}

    def content_lifecycle(self, row, check, now, real, pinned, completed_at=None):
        if completed_at:
            # Device-scoped human intent takes precedence over provider/cache results,
            # including a status lookup already in flight when the user completed it.
            return {
                "state": "ended",
                "last_known_state": "ended",
                "stale": False,
                "observed_at": completed_at,
                "received_at": None,
                "valid_until": None,
                "effective_valid_until": None,
                "source": "manual_completion",
                "timestamp_basis": "manual",
                "simulated": False,
                "tracked": pinned,
                "refresh": {
                    "state": "manual",
                    "last_attempt": None,
                    "last_success": None,
                    "next_check_at": None,
                    "error": None,
                },
            }
        observation = json.loads(check["observation"]) if check and check["observation"] else None
        if (
            observation
            and (
                not pinned
                or (
                    observation.get("timestamp_basis") == "feed_received"
                    and observation.get("source") in {"teamarr_feed", "feed_status_simulator"}
                )
            )
            and observation["state"] not in TERMINAL
            and observation_expiry(observation, self.settings.status_ttl) <= real
        ):
            # Selecting an event must not make an expired copy of the same feed
            # override the current catalog. The fallback retains catalog age;
            # rereading it does not renew evidence or replace terminal facts.
            observation = None
        if observation is None:
            try:
                observation = source_observation(
                    json.loads(row["snapshot"]),
                    row["seen_at"],
                    now,
                    real,
                    self.settings.mode,
                    self.settings.status_ttl,
                )
            except Exception:
                observation = None
            if observation and getattr(self, "executor", None):
                observation.update(source="teamarr_feed", simulated=False)
        return {**lifecycle_view(observation, check, real, self.settings.status_ttl), "tracked": pinned}

    def status_health(self, db, items, device_id="living-room"):
        pins = self.pinned_content_ids(db, device_id=device_id)
        lifecycles = [item["lifecycle"] for item in items if item["content_id"] in pins]
        errors = sum(item["refresh"]["state"] == "error" for item in lifecycles)
        stale = sum(item["stale"] and item["state"] not in TERMINAL for item in lifecycles)
        unknown = sum(item["state"] == "unknown" for item in lifecycles)
        checked = sum(
            item["refresh"]["last_attempt"] is not None or item["source"] == "manual_completion"
            for item in lifecycles
        )
        return {
            "state": "idle"
            if not pins
            else "degraded"
            if errors or stale or unknown
            else "starting"
            if checked < len(pins)
            else "ok",
            "pinned_count": len(pins),
            "checked_count": checked,
            "error_count": errors,
            "stale_count": stale,
            "unknown_count": unknown,
            "last_attempt": max((item["refresh"]["last_attempt"] or "" for item in lifecycles), default="")
            or None,
            "adapter": "executor_and_teamarr"
            if getattr(self, "executor", None)
            else "fixture_simulator"
            if self.settings.mode == "demo"
            else "feed_status_simulator",
            "simulated": not bool(getattr(self, "executor", None)),
        }

    def accept_status_report(self, request, report, error=None):
        now = datetime.now(UTC)
        with self.db.transaction() as db:
            lease = db.execute("SELECT * FROM leases WHERE resource='content-status'").fetchone()
            check = db.execute(
                "SELECT * FROM content_status WHERE content_id=?", (request["content_id"],)
            ).fetchone()
            if (
                not lease
                or lease["owner"] != self.owner
                or lease["expires"] <= time.time()
                or not check
                or check["request_id"] != request["request_id"]
            ):
                return False
            previous = json.loads(check["observation"]) if check["observation"] else None
            try:
                if error:
                    raise InvalidStatus(error)
                if report is None:
                    raise InvalidStatus("Status lookup returned no observation")
                observation = validate_report(request, report, now, self.settings.status_ttl)
                comparable = previous and all(
                    previous.get(key) == observation.get(key) for key in ("source", "timestamp_basis")
                )
                if comparable and observation_time(observation) < observation_time(previous):
                    raise InvalidStatus("Status observation is older than the accepted evidence")
                if (
                    comparable
                    and observation_time(observation) == observation_time(previous)
                    and observation != previous
                ):
                    raise InvalidStatus("Conflicting status observations have the same timestamp")
                if previous and previous["state"] in TERMINAL and observation["state"] == "unknown":
                    observation = previous
                db.execute(
                    "UPDATE content_status SET observation=?,last_success=?,error=NULL,failures=0,next_check=? WHERE content_id=?",
                    (
                        encode(observation),
                        now.isoformat(),
                        time.time() + self.settings.status_interval,
                        request["content_id"],
                    ),
                )
                if check["error"] or (previous and previous["state"] != observation["state"]):
                    self.db.log(
                        db,
                        self.now(db).isoformat(),
                        "Content status updated",
                        f"{request['content_snapshot']['title']}: {observation['state']}",
                        "status",
                    )
                return True
            except (TypeError, ValueError, AttributeError, KeyError) as exc:
                # Never store transport URLs, provider response bodies, or credentials.
                reason = str(exc) if isinstance(exc, InvalidStatus) else "Invalid status observation"
                failures = check["failures"] + 1
                db.execute(
                    "UPDATE content_status SET error=?,failures=?,next_check=? WHERE content_id=?",
                    (
                        reason[:200],
                        failures,
                        time.time() + min(5 * 2 ** min(failures - 1, 4), 60),
                        request["content_id"],
                    ),
                )
                if check["error"] != reason:
                    self.db.log(
                        db,
                        self.now(db).isoformat(),
                        "Content status unavailable",
                        f"{request['content_snapshot']['title']}: {reason}. Watch plan retained.",
                        "status",
                    )
                return False

    async def refresh_status(self, *, force=False):
        with self.db.transaction() as db:
            if not self.db.lease(
                db,
                "content-status",
                self.owner,
                time.time(),
                max(30, 8 * self.settings.status_lookup_timeout + 15),
            ):
                return
            pins = self.pinned_content_ids(db, for_lookup=True)
            requests = []
            # Cap work per pass. Due checks sort before future checks, preventing starvation.
            rows = db.execute(
                "SELECT c.*,s.next_check FROM contents c LEFT JOIN content_status s ON s.content_id=c.id "
                "ORDER BY COALESCE(s.next_check,0),c.id"
            ).fetchall()
            for row in rows:
                if row["id"] not in pins or (not force and (row["next_check"] or 0) > time.time()):
                    continue
                request = {
                    "schema_version": 1,
                    "request_id": str(uuid.uuid4()),
                    "content_id": row["id"],
                    "content_snapshot_schema_version": 1,
                    "content_snapshot": json.loads(row["snapshot"]),
                    "catalog_seen_at": row["seen_at"],
                    "as_of": self.now(db).isoformat(),
                }
                requests.append(request)
                db.execute(
                    "INSERT INTO content_status(content_id,request_id,last_attempt,next_check) VALUES (?,?,?,?) "
                    "ON CONFLICT(content_id) DO UPDATE SET request_id=excluded.request_id,last_attempt=excluded.last_attempt,next_check=excluded.next_check",
                    (
                        row["id"],
                        request["request_id"],
                        datetime.now(UTC).isoformat(),
                        time.time() + self.settings.status_lookup_timeout + self.settings.status_interval,
                    ),
                )
                if len(requests) >= 50:
                    break
        semaphore = asyncio.Semaphore(8)

        async def lookup(request):
            async with semaphore:
                try:
                    report = await asyncio.wait_for(
                        self.status_adapter.lookup(request), self.settings.status_lookup_timeout
                    )
                except Exception as exc:
                    self.accept_status_report(request, None, f"{type(exc).__name__}: status lookup failed")
                else:
                    self.accept_status_report(request, report)

        await asyncio.gather(*(lookup(request) for request in requests))

    async def run_status(self):
        while True:
            try:
                await self.refresh_status()
            except Exception:
                # A failed pass must not stop independent status refresh permanently.
                log.exception("Content status refresh failed; will retry")
            await asyncio.sleep(min(self.settings.status_interval, 1))
