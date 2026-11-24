"""Prime-sourced idle nominations. Discovery never dispatches device playback.

The normal fleet coordinator owns all launches, cancellation and preemption.
Every nomination is fenced by user intent, ownership, runtime and fresh evidence.
"""
import asyncio
import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from .database import encode
from .event_attempts import exhausted
from .executor.models import ExecutorError
from .idle_sports_chooser import choose, eligible_candidates, timestamp
from .model_diagnostics import capture_model_calls_async
from .planner import idle_blocked, is_idle

TTL = 60
REFRESH_SECONDS = 20
log = logging.getLogger(__name__)


def candidate_id(candidate):
    identity = [candidate.get("kind"), candidate.get("station_id"),
                candidate.get("airing_id"), candidate.get("content_id")]
    return "prime_idle:" + hashlib.sha256(encode(identity).encode()).hexdigest()[:32]


def catalogue_candidates(result, now):
    """Positive rows from partial catalogues are useful; absence proves nothing."""
    if result.get("complete") is not True:
        return []
    summary = result.get("station_summary") or {}
    candidates = []
    if summary.get("schema") == "live_tv_epg":
        stations = summary.get("stations", [])
        if not isinstance(stations, list) or len(stations) > 200:
            return []
        grouped = {}
        for station in stations:
            if isinstance(station, dict):
                grouped.setdefault(station.get("station_id"), []).append(station)
        for sid, occurrences in grouped.items():
            station = occurrences[0]
            if any(any(s.get(k) != station.get(k) for k in
                       ("name", "entitlement_status", "has_watch_action")) for s in occurrences):
                continue
            airings = [a for s in occurrences for a in s.get("airings", []) if isinstance(a, dict)]
            current = [a for a in airings if timestamp(a.get("starts_at"))
                       and timestamp(a.get("ends_at"))
                       and timestamp(a["starts_at"]) <= now < timestamp(a["ends_at"])]
            # Two different current slots on one station cannot identify what is on.
            if len({encode(a) for a in current}) != 1:
                continue
            for airing in current[:1]:
                if airing.get("station_id") != sid:
                    continue
                candidates.append({**airing, "kind": "linear_program", "station_id": sid,
                    "station_name": station.get("name"), "has_watch_action": station.get("has_watch_action"),
                    "entitlement_status": station.get("entitlement_status"),
                    "observed_at": result.get("observed_at")})
    for event in result.get("events", []):
        if isinstance(event, dict):
            candidates.append({**event, "kind": "event", "observed_at": result.get("observed_at")})
    for candidate in candidates:
        candidate["candidate_id"] = candidate_id(candidate)
    # Fail bounded rather than silently truncating away the current program.
    return eligible_candidates(candidates, now=now)[0]


def expires_at(candidate):
    observed = timestamp(candidate.get("observed_at"))
    if observed is None:
        return 0
    expires = observed + timedelta(seconds=TTL)
    if candidate.get("kind") == "linear_program":
        end = timestamp(candidate.get("ends_at"))
        if not end:
            return 0
        expires = min(expires, end)
    return expires.timestamp()


def content_snapshot(candidate):
    linear = candidate["kind"] == "linear_program"
    # Live EVENT evidence is authoritative even if its estimated end has passed.
    start = candidate.get("starts_at") or candidate["observed_at"]
    return {
        "id": candidate["candidate_id"], "source": "prime_idle", "kind": "broadcast" if linear else "event",
        "title": candidate["title"], "start_time": start,
        "expected_end_time": candidate.get("ends_at"), "end_time_estimated": not linear,
        "status": "live", "sports": [], "competition": "prime_sports",
        "prime_idle": candidate,
        "broadcast": {"coverage_type": "linear_program"} if linear else None,
        "viewing_options": [{"id": "prime_idle_live", "app": "prime_video", "presentation": "live",
            "channel": candidate.get("station_name"), "stream_title": candidate["title"],
            "coverage_type": "linear_program" if linear else "full", "decision": "included"}],
    }


def lifecycle(snapshot, now, pinned):
    evidence = snapshot.get("prime_idle") or {}
    expiry = expires_at(evidence)
    fresh = expiry > now.timestamp() and timestamp(evidence.get("observed_at")) <= now
    return {"state": "live" if fresh else "unknown", "last_known_state": "live", "stale": not fresh,
        "observed_at": evidence.get("observed_at"), "received_at": evidence.get("observed_at"),
        "valid_until": datetime.fromtimestamp(expiry, UTC).isoformat(),
        "effective_valid_until": datetime.fromtimestamp(expiry, UTC).isoformat(),
        "source": "prime_live_catalogue", "timestamp_basis": "provider", "simulated": False,
        "tracked": pinned, "refresh": {"state": "current" if fresh else "due", "last_attempt": None,
            "last_success": evidence.get("observed_at"), "next_check_at": None, "error": None}}


def intent_fence(device):
    return [device["revision"], device["intent_version"], device["automation"],
            device.get("manual_control"), device.get("input_handoff"), device.get("prime_network_scope"),
            device.get("preferences"), device.get("rules"), device.get("team_ranks"),
            device.get("plan"), device.get("manual_completions"), device.get("player_recovery")]


def preferences_digest(device, teams):
    return hashlib.sha256(encode({"preferences": device.get("preferences"), "rules": device.get("rules"),
        "team_ranks": device.get("team_ranks"), "teams": teams}).encode()).hexdigest()


def candidate_blocked(db, device, candidate):
    cid = candidate["candidate_id"]
    if cid in device.get("manual_completions", {}) or exhausted(device, cid):
        return True
    if candidate.get("kind") != "event":
        return False  # Station signals never establish programme completion.
    row = db.execute("SELECT observation FROM content_status WHERE content_id=?", (cid,)).fetchone()
    observation = json.loads(row[0]) if row and row[0] else {}
    return bool(observation.get("source") == "prime_player" and observation.get("state") == "ended"
                and (observation.get("evidence") or {}).get("decision") == "confirmed")


class IdleSports:
    def expire_idle_rows(self):
        """Withdraw expired guide availability without asserting completion."""
        now = datetime.now(UTC)
        with self.db.transaction() as db:
            protected = set()
            for row in db.execute("SELECT payload FROM devices"):
                nomination = json.loads(row[0]).get("idle_sports") or {}
                if nomination.get("state") == "ready" and nomination.get("expires_at", 0) > now.timestamp():
                    protected.add(nomination.get("content_id"))
            rows = db.execute(
                "SELECT id,snapshot FROM contents WHERE source='prime_idle' AND active=1 AND "
                "(COALESCE(julianday(seen_at),0)<=julianday(?) OR "
                "(json_extract(snapshot,'$.prime_idle.kind')='linear_program' AND "
                "COALESCE(julianday(json_extract(snapshot,'$.prime_idle.ends_at')),0)<=julianday(?))) "
                "ORDER BY seen_at,id LIMIT 200",
                ((now - timedelta(seconds=TTL)).isoformat(), now.isoformat()),
            ).fetchall()
            for row in rows:
                if row["id"] not in protected and expires_at(json.loads(row["snapshot"]).get("prime_idle") or {}) <= now.timestamp():
                    db.execute("UPDATE contents SET active=0 WHERE id=? AND source='prime_idle'", (row["id"],))

    def idle_context_in(self, db, *, read_device=True, check_retry=False):
        """A read-only fleet selection shares exactly the launch priority policy."""
        from .fleet import select_for_stream, allocation_inputs
        device = self.db.device(db, self.device_id)
        if not device["preferences"].get("idle_sports_enabled", False):
            return None
        if device["automation"] != "active" or self.entitlement_blocker(db, device, device_read=read_device):
            return None
        now = datetime.now(UTC)
        previous = device.get("idle_sports") or {}
        if check_retry and previous.get("next_check_at", 0) > now.timestamp():
            return None
        items = self.planning_items(db)
        normal = select_for_stream(self, db, device, [i for i in items if not is_idle(i)],
                                   self.now(db), now, reserve=False)
        if idle_blocked(normal):
            return None
        devices, claims = allocation_inputs(self, db, device)
        peers = []
        for peer, ids in claims.items():
            if peer == self.device_id:
                continue
            for cid in ids:
                row = db.execute("SELECT snapshot FROM contents WHERE id=?", (cid,)).fetchone()
                if row:
                    snapshot = json.loads(row[0])
                    peers.append(snapshot.get("prime_idle") or {"candidate_id": cid, "title": snapshot["title"]})
                # A scheduled Teamarr event and its Prime idle EVENT have
                # different controller IDs. Retain the peer's explicit native
                # mapping as well, so code can exclude duplicate playback.
                for job in db.execute(
                    "SELECT report FROM executor_jobs WHERE device_id=? AND content_id=? "
                    "AND touched_device=1 AND retired_at IS NULL AND cancel_requested!=2 AND state!='completed'",
                    (peer, cid),
                ):
                    selected = (json.loads(job[0]).get("prime_player") or {}).get("selected") or {}
                    if selected.get("content_id"):
                        peers.append({"content_id": selected["content_id"],
                                      "station_id": (selected.get("linear_airing") or {}).get("station_id")})
            other = devices[peer]
            nomination = other.get("idle_sports") or {}
            if (other["automation"] == "active" and not other.get("manual_control")
                    and not other.get("input_handoff") and other["preferences"].get("idle_sports_enabled")
                    and nomination.get("state") == "ready" and nomination.get("expires_at", 0) > now.timestamp()
                    and nomination.get("candidate")):
                peers.append(nomination["candidate"])
        keys = {key for ordered in device.get("team_ranks", {}).values() for key in ordered}
        keys.update(rule["team_id"] for rule in device.get("rules", []) if rule.get("team_id"))
        # Supply names only for referenced preferences, without a full directory
        # export. Omitted/unknown keys remain opaque rather than invented names.
        teams = []
        for key in sorted(keys)[:200]:
            row = db.execute("SELECT payload FROM team_directory WHERE key=?", (key,)).fetchone()
            if row:
                team = json.loads(row[0])
                teams.append({k: team[k] for k in ("key", "league", "full_name", "name", "short_name", "abbreviation") if team.get(k)})
        return {"device": device, "fence": intent_fence(device), "peers": peers, "teams": teams,
                "preferences_digest": preferences_digest(device, teams), "current": previous.get("candidate")}

    def idle_context(self, *, read_device=True):
        with self.db.snapshot() as db:
            return self.idle_context_in(db, read_device=read_device, check_retry=True)

    def idle_progress(self, context, phase):
        with self.db.transaction() as db:
            device = self.db.device(db, self.device_id)
            if intent_fence(device) != context["fence"]:
                return False
            previous = device.get("idle_sports") or {}
            device["idle_sports"] = {**previous, "phase": phase,
                "state": "ready" if previous.get("expires_at", 0) > time.time() else "checking",
                "reason": "Checking Prime sports programming"}
            self.db.save_device(db, device)
            return True

    def idle_filter_candidates(self, context, candidates):
        with self.db.snapshot() as db:
            device = self.db.device(db, self.device_id)
            if intent_fence(device) != context["fence"]:
                raise ExecutorError("prime_stale_result", "Idle sports intent changed")
            return [candidate for candidate in candidates if not candidate_blocked(db, device, candidate)]

    def idle_publish(self, context, selected, audit, scope):
        """Recheck priority inside the writer before making a nomination visible."""
        with self.db.transaction() as db:
            # Peer claims can change while the model runs. The fleet dispatcher
            # also enforces exclusivity; here we avoid publishing an invalid choice.
            fresh = self.idle_context_in(db, read_device=False)
            if fresh is None or fresh["fence"] != context["fence"]:
                return False
            device = fresh["device"]
            real = datetime.now(UTC)
            if selected and (candidate_blocked(db, device, selected)
                             or not eligible_candidates([selected], now=real, peers=fresh["peers"])[0]):
                return False
            previous = device.get("idle_sports") or {}
            if selected:
                snapshot = content_snapshot(selected)
                db.execute("INSERT INTO contents VALUES (?,?,1,?,?) ON CONFLICT(id) DO UPDATE SET "
                           "active=1,seen_at=excluded.seen_at,snapshot=excluded.snapshot,source=excluded.source "
                           "WHERE julianday(excluded.seen_at)>=COALESCE(julianday(contents.seen_at),0)",
                           (snapshot["id"], "prime_idle", selected["observed_at"], encode(snapshot)))
                same = previous.get("content_id") == snapshot["id"]
                device["idle_sports"] = {"state": "ready", "content_id": snapshot["id"], "candidate": selected,
                    "expires_at": expires_at(selected), "selected_at": previous.get("selected_at", real.timestamp())
                    if same else real.timestamp(), "reason": audit.get("reason", "Sports selected from Prime"),
                    "scope": scope, "checked_at": real.isoformat(), "phase": "idle", "failures": 0,
                    "model_checked_at": previous.get("model_checked_at", 0) if audit.get("method") == "renewal" else real.timestamp(),
                    "preferences_digest": context["preferences_digest"],
                    "next_check_at": real.timestamp() + REFRESH_SECONDS}
            elif audit.get("match_status") == "uncertain" and previous.get("expires_at", 0) > real.timestamp():
                # A partial/uncertain read withdraws no existing evidence and
                # renews none. The old nomination keeps its original deadline.
                device["idle_sports"] = {**previous, "phase": "idle", "checked_at": real.isoformat(),
                    "reason": audit.get("reason", "Waiting for a clearer Prime sports listing"),
                    "next_check_at": real.timestamp() + REFRESH_SECONDS}
            else:
                device["idle_sports"] = {"state": "unavailable", "reason": audit.get("reason", "No confirmed sports available"),
                                        "checked_at": real.isoformat(), "expires_at": 0, "phase": "idle",
                                        "failures": 0, "next_check_at": real.timestamp() + REFRESH_SECONDS}
            # Start with the initial page, Sports view and first continuation.
            # If that yields no selection, widen subsequent reads. Keep the
            # wider scan for a choice discovered there so renewal can find it.
            device["idle_sports"]["catalogue_max_pages"] = 8 if (
                audit.get("catalogue_max_pages") == 8 or
                not selected and (audit.get("coverage") or {}).get("exhausted") is not True
            ) else 3
            self.db.save_device(db, device)
            self.db.set_meta(db, "idle_sports_audit:" + self.device_id, audit)
            return True

    def idle_failure(self, context, exc):
        with self.db.transaction() as db:
            device = self.db.device(db, self.device_id)
            if intent_fence(device) != context["fence"]:
                return
            previous = device.get("idle_sports") or {}
            failures = previous.get("failures", 0) + 1
            code = getattr(exc, "code", type(exc).__name__)
            invalidated = code in {"prime_stale_result", "prime_device_mismatch", "prime_ownership_unavailable"}
            reason = {
                "prime_linear_catalog_unavailable": "Prime Player needs Live TV catalogue support",
                "prime_linear_catalog_timeout": "Prime sports catalogue timed out; retrying automatically",
                "idle_sports_selection_unavailable": "Configure a chooser model to use idle sports",
                "prime_ownership_unavailable": "Waiting for automatic control of Prime Video",
            }.get(code, "Prime sports selection is temporarily unavailable")
            # Failed reads do not renew prior evidence. Its original deadline holds.
            device["idle_sports"] = {**previous, "last_error": type(exc).__name__, "error_code": code,
                "reason": reason,
                "state": "ready" if not invalidated and previous.get("expires_at", 0) > time.time() else "unavailable",
                "expires_at": 0 if invalidated else previous.get("expires_at", 0),
                "phase": "waiting", "failures": failures, "checked_at": datetime.now(UTC).isoformat(),
                "next_check_at": time.time() + min(300, REFRESH_SECONDS * 2 ** min(failures - 1, 4))}
            self.db.save_device(db, device)
            audit = self.db.meta(db, "idle_sports_audit:" + self.device_id, {})
            error = {"code": code}
            validation_reason = getattr(exc, "validation_reason", None)
            if validation_reason:
                error["validation_reason"] = validation_reason
            self.db.set_meta(db, "idle_sports_audit:" + self.device_id, {
                **audit, "match_status": "uncertain", "reason": reason,
                "error": error, "checked_at": datetime.now(UTC).isoformat()})

    async def refresh_idle_sports(self):
        await self.playback_work(self.expire_idle_rows)
        context = await self.playback_work(self.idle_context)
        if context is None:
            return False
        workflow = self.executor
        try:
            if not await self.playback_work(self.idle_progress, context, "catalogue"):
                return False
            async with workflow.input_lock:
                fresh = await self.playback_work(lambda: self.idle_context(read_device=False))
                if fresh is None or fresh["fence"] != context["fence"]:
                    return False
                health = await workflow.player.health()
                if health.get("serial") != self.settings.screen_adb_serial:
                    raise ExecutorError("prime_device_mismatch", "Prime Player device differs")
                workflow.player.require(health, "linear_catalog")
                ownership = workflow.player.ownership(health, automatic=True)
                max_pages = 8 if (context["device"].get("idle_sports") or {}).get("catalogue_max_pages") == 8 else 3
                result = await workflow.player.rpc("linear_catalog", timeout=45, budget=60,
                    facet="sports", max_pages=max_pages, ownership=ownership)
                after = await workflow.player.health()
                if (after["session_id"] != health["session_id"] or result.get("session_id") != after["session_id"]
                        or workflow.player.ownership(after, automatic=True) != ownership):
                    raise ExecutorError("prime_stale_result", "Prime ownership changed during idle discovery")
                workflow.require_catalogue_generation(after, result)
            candidates = await self.playback_work(self.idle_filter_candidates, context,
                catalogue_candidates(result, datetime.now(UTC)))
            scope = {k: result.get(k) for k in ("session_id", "generation", "network_context_id")}
            current = context["current"]
            previous = context["device"].get("idle_sports") or {}
            # Renew unchanged, already-classified programming without paying for
            # a model call every twenty seconds. Reassess interests after dwell.
            existing = next((c for c in candidates if current and c["candidate_id"] == current["candidate_id"]), None)
            unchanged = existing and all(existing.get(k) == current.get(k) for k in
                ("title", "synopsis", "episode_title", "starts_at", "ends_at", "genres", "sport", "competition",
                 "station_name", "language", "audio_language", "languages"))
            dwell = max(60, context["device"]["preferences"].get("minimum_viewing_seconds", 300))
            if (unchanged and previous.get("scope") == scope
                    and previous.get("preferences_digest") == context["preferences_digest"]
                    and time.time() - previous.get("model_checked_at", 0) < dwell):
                selected, audit = existing, {"reason": "Continuing the selected sports program", "method": "renewal"}
            else:
                preferences = {k: context["device"].get(k) for k in ("rules", "team_ranks")}
                preferences["teams"] = context["teams"]
                if not await self.playback_work(self.idle_progress, context, "choosing"):
                    return False
                async with capture_model_calls_async(lambda _: None, recorder=workflow.model_captures.record,
                    context={"device_id": self.device_id, "task": "idle_sports_selection"}):
                    device = context["device"]
                    watching_current = current and (device.get("observed") or {}).get("content_id") == current["candidate_id"]
                    started, switched = timestamp(device.get("started_at")), timestamp(device.get("last_switch_at"))
                    now = datetime.now(UTC)
                    allow_switch = not watching_current or (
                        (not started or (now - started).total_seconds() >= device["preferences"].get("minimum_viewing_seconds", 300))
                        and (not switched or (now - switched).total_seconds() >= device["preferences"].get("switch_cooldown_seconds", 30))
                    )
                    selected, audit = await choose(candidates, matcher=workflow.matcher, preferences=preferences,
                        current_candidate_id=current.get("candidate_id") if current else None, peers=context["peers"],
                        allow_switch=bool(allow_switch))
            audit = {**audit, "coverage": result.get("catalogue_coverage"), "candidate_count": len(candidates),
                     "catalogue_max_pages": max_pages}
            # Model latency cannot reuse a replaced service/network/ownership scope.
            async with workflow.input_lock:
                fresh = await self.playback_work(lambda: self.idle_context(read_device=False))
                if fresh is None or fresh["fence"] != context["fence"]:
                    return False
                after = await workflow.player.health()
                if after["session_id"] != health["session_id"] or workflow.player.ownership(after, automatic=True) != ownership:
                    raise ExecutorError("prime_stale_result", "Prime ownership changed during idle selection")
                workflow.require_catalogue_generation(after, result)
            return await self.playback_work(self.idle_publish, context, selected, audit, scope)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.playback_work(self.idle_failure, context, exc)
            return False

    async def run_idle_sports(self):
        while True:
            try:
                await self.refresh_idle_sports()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed database/context read has no nomination to update,
                # but must not permanently kill the background worker.
                log.exception("Idle sports refresh failed; retrying")
            await asyncio.sleep(REFRESH_SECONDS)
