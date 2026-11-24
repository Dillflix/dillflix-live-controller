"""On-demand catalogue checks before committing a playback switch."""

import asyncio
from copy import deepcopy
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ..entitlement_store import identity, record_entitled
from ..executor.models import ExecutorError
from ..model_diagnostics import capture_model_calls_async
from .client import (
    COMPATIBILITY_MESSAGES,
    RECOVERING_ADAPTATION_STATES,
    adaptation_status,
    recovery_message,
    rpc_context,
)
from .match_context import IDENTITY_CONTRACT
from .matching import selection_state
from .search import choose_search
from .linear import catalogue_capability, read_catalogue, is_idle, is_linear, choose_airing, choose_idle_event, timestamp

SELECTION_HANDOFF_SECONDS = 60

CATALOGUE_ERROR_MESSAGES = {
    **COMPATIBILITY_MESSAGES,
    "prime_ownership_unavailable": (
        "Prime automatic control is not acknowledged; catalogue check retries in 60 seconds"
    ),
    "prime_broadcast_selection_unavailable": "Prime broadcast selection requires a configured matching model",
    "prime_broadcast_selection_failed": "Prime broadcast selection model failed; catalogue check retries in 60 seconds",
    "prime_broadcast_selection_invalid": "Prime broadcast selection response was invalid; catalogue check retries in 60 seconds",
    "prime_invalid_broadcasts": (
        "Prime broadcast data could not be parsed; catalogue check retries in 60 seconds"
    ),
}


def key(device_id):
    return "prime_catalogue_probe:" + device_id


def snapshot_identity(snapshot):
    """Native observation age can refresh without changing programme identity."""
    if snapshot.get("source") != "prime_idle":
        return snapshot
    value = deepcopy(snapshot)
    value.get("prime_idle", {}).pop("observed_at", None)
    return value


def renew_idle_evidence(db, database, device, request, results, selected):
    """Persist an exact fresh native recheck, never extend evidence from a timer.

    Callers must first validate their probe/token intent and service ownership.
    Submitted playback requests remain immutable; only catalogue availability
    and the same device nomination receive the provider's new observation time.
    """
    if not is_idle(request):
        return None
    from ..idle_sports import candidate_blocked, catalogue_candidates, expires_at
    now = datetime.now(UTC)
    snapshot = request["content_snapshot"]
    cid = snapshot["id"]
    nomination = device.get("idle_sports") or {}
    if (device["automation"] != "active" or device.get("manual_control") or device.get("input_handoff")
            or not device["preferences"].get("idle_sports_enabled") or nomination.get("content_id") != cid):
        return None
    validated, _ = (choose_airing if is_linear(request) else choose_idle_event)(request, results, now=now)
    if (not validated or validated.get("readiness") != "ready" or not selected
            or any(validated.get(k) != selected.get(k) for k in ("content_id", "viewing_option_id"))):
        return None
    native = next((candidate for candidate in catalogue_candidates({**results, "events": results.get("events") or []}, now)
                   if candidate["candidate_id"] == cid), None)
    expected = snapshot.get("prime_idle") or {}
    if native is None or any(native.get(k) != expected.get(k) for k in (
        "title", "synopsis", "episode_title", "sport", "competition", "genres",
        "language", "audio_language", "languages", "station_name", "starts_at", "ends_at",
    )):
        return None  # Changed programming/preferences evidence needs a new chooser assessment.
    row = db.execute("SELECT snapshot FROM contents WHERE id=? AND source='prime_idle'", (cid,)).fetchone()
    current = json.loads(row[0]) if row else None
    if current is None or snapshot_identity(current) != snapshot_identity(snapshot):
        return None
    renewed = deepcopy(snapshot)
    renewed["prime_idle"]["observed_at"] = results["observed_at"]
    candidate = renewed["prime_idle"]
    if candidate_blocked(db, device, candidate) or expires_at(candidate) <= now.timestamp():
        return None
    prior_observed = timestamp(current["prime_idle"].get("observed_at"))
    if not prior_observed or timestamp(results["observed_at"]) >= prior_observed:
        db.execute("UPDATE contents SET snapshot=?,seen_at=?,active=1 WHERE id=? AND source='prime_idle'",
                   (json.dumps(renewed, separators=(",", ":")), results["observed_at"], cid))
    device["idle_sports"] = {**nomination, "state": "ready", "candidate": candidate,
        "expires_at": expires_at(candidate), "checked_at": now.isoformat(),
        "scope": {k: results.get(k) for k in ("session_id", "generation", "network_context_id")}}
    database.save_device(db, device)
    return renewed


def signature(device, item):
    return hashlib.sha256(
        json.dumps(
            [
                IDENTITY_CONTRACT,
                device["revision"],
                device["intent_version"],
                item["content_id"],
                snapshot_identity(item["snapshot"]),
                item["viewing_options"],
                device.get("prime_network_scope"),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def assignment_matches(device, content_id):
    """A preflight belongs to the current fleet allocation, when one exists."""
    assignment = device.get("stream_assignment")
    return assignment is None or assignment.get("content_id") == content_id


class CatalogueChecks:
    @staticmethod
    def require_catalogue_generation(health, results):
        context = health.get("network", {}).get("context_id")
        if health.get("network", {}).get("enabled") and (not context or results.get("network_context_id") != context):
            raise ExecutorError("prime_stale_result", "Catalogue belongs to a replaced network context")
        generation = health.get("compatibility", {}).get("generation")
        if generation is not None and results.get("generation") != generation:
            raise ExecutorError("prime_stale_result", "Prime runtime changed after catalogue selection")

    def record_runtime_adaptation(self, health):
        """Wake a blocked catalogue check without adopting or replaying playback."""
        adaptation = adaptation_status(health)
        with self.db.transaction() as db:
            device = self.db.device(db, self.device_id)
            network = health.get("network", {})
            if network.get("enabled"):
                operation = network.get("operation") or {}
                context = network.get("context") or {}
                device["prime_network"] = {
                    "state": network.get("state", "unknown"), "target_id": context.get("target_id"),
                    "phase": operation.get("phase"), "operation_id": operation.get("operation_id"),
                    "requested_target": (operation.get("target") or {}).get("id"),
                    "last_error": operation.get("error") or network.get("last_error"),
                    "checked_at": datetime.now(UTC).isoformat(),
                    "policy_enabled": self.network_policy.enabled,
                    "startup": health.get("startup"),
                }
                scope = {"context_id": network.get("context_id"), "policy_revision": self.network_policy.revision}
                if device.get("prime_network_scope") != scope:
                    device["prime_network_scope"] = scope
                    device["prime_access"] = {}
                    if device.get("observed"):
                        device["observed"]["verified"] = False
                    self.db.save_device(db, device)
            elif device.pop("prime_network_scope", None):
                device.pop("prime_network", None)
                device["prime_access"] = {}
                self.db.save_device(db, device)
            if not adaptation and not device.get("prime_runtime_adaptation"):
                self.db.save_device(db, device)
                return
            if adaptation:
                device["prime_runtime_adaptation"] = {
                    **adaptation,
                    "session_id": health["session_id"],
                    "checked_at": datetime.now(UTC).isoformat(),
                    "next_probe_at": time.time() + min(60, adaptation["retry_after_seconds"])
                    if adaptation["state"] in RECOVERING_ADAPTATION_STATES else None,
                }
            else:
                device.pop("prime_runtime_adaptation", None)
            probe = self.db.meta(db, key(device["id"]), {})
            error = probe.get("error") or (probe.get("audit") or {}).get("error") or {}
            capability = probe.get("runtime_capability", "search")
            available = health.get("compatibility", {}).get("capabilities", {})
            recovery_codes = {"prime_runtime_recovering", "prime_runtime_unsupported"}
            if (
                probe.get("state") == "access_unknown"
                and error.get("code") in recovery_codes
                and available.get(capability, {}).get("available") is True
            ):
                probe.update(retry_at=0, expires=0)
                self.db.set_meta(db, key(device["id"]), probe)
                access = device.get("prime_access", {}).get(probe.get("content_id"), {})
                if access.get("catalogue_probe_id") == probe.get("id"):
                    access["retry_after"] = datetime.now(UTC).isoformat()
            # Catalogue probes are deliberately discarded on controller restart;
            # their access records still identify which capability blocked them.
            for access in device.get("prime_access", {}).values():
                if (
                    access.get("state") == "access_unknown"
                    and access.get("runtime_error_code") in recovery_codes
                    and available.get(access.get("runtime_capability"), {}).get("available") is True
                ):
                    access["retry_after"] = datetime.now(UTC).isoformat()
            self.db.save_device(db, device)

    def prepare_selection(self, db, device, item):
        """Called under the staging transaction; never issues an RPC here."""
        self.store.require_device(device["id"])
        rule = self.network_policy.rule(item["viewing_options"], item["snapshot"])
        if rule and device.get("prime_network_scope", {}).get("policy_revision") != self.network_policy.revision:
            device["prime_network_scope"] = {"context_id": None, "policy_revision": self.network_policy.revision}
            device["prime_access"] = {}
            self.db.save_device(db, device)
        token = signature(device, item)
        probe = self.db.meta(db, key(device["id"]), {})
        now = time.time()
        if probe.get("signature") == token:
            if probe.get("state") in {"ready", "network_acquisition_required"} and now < probe.get("expires", 0):
                probe["launch_intent"] = device["intent_version"] + 1
                self.db.set_meta(db, key(device["id"]), probe)
                return {"state": probe["state"], "probe_id": probe["id"]}
            if probe.get("state") == "pending" and now < probe.get("expires", 0):
                return {"state": "pending"}
            if now < probe.get("retry_at", 0):
                return {"state": "pending"}
        active = getattr(self, "catalogue_task", None)
        if active and not active.done():
            return {"state": "pending"}
        probe = {
            "id": uuid4().hex,
            "signature": token,
            "initial_network_scope": device.get("prime_network_scope"),
            "state": "pending",
            "device_id": device["id"],
            "content_id": item["content_id"],
            "revision": device["revision"],
            "intent": device["intent_version"],
            "requested_at": datetime.now(UTC).isoformat(),
            "expires": now + self.config.prime_search_timeout + self.config.model_timeout + 65,
            "request": {
                "content_snapshot": item["snapshot"],
                "allowed_viewing_options": item["viewing_options"],
            },
            "timezone": device["preferences"]["timezone"],
        }
        self.db.set_meta(db, key(device["id"]), probe)
        assignment = device.get("stream_assignment")
        if assignment is None and is_idle(probe["request"]):
            assignment = device["stream_assignment"] = {"content_id": item["content_id"], "revision": device["revision"]}
        if assignment and assignment.get("content_id") == item["content_id"]:
            assignment["catalogue"] = {"id": probe["id"], "intent": probe["intent"],
                                       "expires": probe["expires"], "identity": identity(item)}
            self.db.save_device(db, device)
        if self.loop and self.loop.is_running() and not self.stopping:

            def schedule():
                active = getattr(self, "catalogue_task", None)
                if active and not active.done():
                    return  # Next staging tick queues the newest request after this read settles.
                self.catalogue_task = asyncio.create_task(self.observe_catalogue(probe))

            # The worker uses WAL snapshots: scheduling before this transaction
            # commits lets it see the previous probe and reject its own work.
            db.defer_after_commit(lambda: self.loop.call_soon_threadsafe(schedule))
        return {"state": "pending"}

    async def observe_catalogue(self, probe):
        correlation = rpc_context.set({"device_id": self.device_id,
                                       "catalogue_probe_id": probe["id"], "content_id": probe["content_id"]})
        evidence = {**probe, "model_calls": []}
        try:
            async with asyncio.timeout(self.config.prime_search_timeout + self.config.model_timeout + 50):
                session = None
                received = {}

                async def require_current(phase):
                    invalidation = await self.database_call(lambda: self.catalogue_invalidation(probe))
                    if invalidation:
                        evidence["invalidation"] = {**invalidation, "phase": phase,
                                                    "at": datetime.now(UTC).isoformat()}
                        raise ExecutorError("prime_stale_result", "Catalogue selection was superseded during " + phase)

                async def search(query):
                    nonlocal session
                    async with self.input_lock:
                        await require_current("search")
                        capability = catalogue_capability(probe["request"])
                        evidence["runtime_capability"] = capability
                        health = await self.check_session(session, capabilities=(capability,))
                        if health.get("api_version", 0) < 11:
                            raise ExecutorError(
                                "prime_api_incompatible", "Prime Player API 11 or newer is required", retryable=False
                            )
                        session = health["session_id"]
                        ownership = self.player.ownership(health, automatic=True)
                        results = await read_catalogue(self.player, probe["request"], query,
                                                       self.config.prime_search_timeout, ownership)
                    if results.get("session_id") != session:
                        raise ExecutorError("prime_stale_result", "Catalogue result belongs to another session")
                    self.require_catalogue_generation(health, results)
                    received[query] = time.time()
                    evidence.update(query=query, results=results, session_id=session, received_at=received[query])
                    return results

                async def fetch_broadcasts(results, content_id):
                    async with self.input_lock:
                        await require_current("broadcast selection")
                        evidence["runtime_capability"] = "broadcasts"
                        current = await self.check_session(session, capabilities=("broadcasts",))
                        self.require_catalogue_generation(current, results)
                        return await self.player.broadcasts(
                            content_id, self.player.ownership(current, automatic=True)
                        )

                def diagnostic(value):
                    evidence["model_calls"] = [
                        call for call in evidence["model_calls"] if call["call_id"] != value["call_id"]
                    ] + [value]

                async with capture_model_calls_async(
                    diagnostic, recorder=self.model_captures.record,
                    context={"source": "catalogue", "device_id": probe["device_id"],
                             "content_id": probe["content_id"], "catalogue_probe_id": probe["id"],
                             "intent_version": probe["intent"]},
                ):
                    results, selected, audit = await choose_search(
                        probe["request"], probe["timezone"], self.matcher, search, fetch_broadcasts,
                        lambda audit: evidence.update(audit=audit),
                    )
                state = selection_state(selected, audit)
                evidence.update(state=state, selected=selected, audit=audit, results=results,
                                query=results["query"], received_at=received[results["query"]])
        except asyncio.CancelledError:
            evidence.update(
                state="access_unknown", error={"code": "cancelled", "message": "Catalogue check cancelled"}
            )
            raise
        except Exception as exc:
            error = (
                exc.detail()
                if isinstance(exc, ExecutorError)
                else {"code": type(exc).__name__, "message": str(exc)[:1000]}
            )
            evidence.update(state="access_unknown", error=error)
        finally:
            rpc_context.reset(correlation)
            await self.database_call(lambda: self.finish_catalogue(probe, evidence))

    def catalogue_current(self, probe):
        return self.catalogue_invalidation(probe) is None

    def catalogue_invalidation(self, probe):
        self.store.require_device(probe["device_id"])
        with self.db.snapshot() as db:
            device = self.db.device(db, probe["device_id"])
            saved = self.db.meta(db, key(device["id"]), {})
            expected = {"probe_id": probe["id"], "revision": probe["revision"], "intent": probe["intent"],
                        "assignment": probe["content_id"], "automation": "active",
                        "manual_control": False, "input_handoff": False}
            current = {"probe_id": saved.get("id"), "revision": device["revision"],
                       "intent": device["intent_version"], "automation": device["automation"],
                       "assignment": (device.get("stream_assignment") or {}).get("content_id", probe["content_id"]),
                       "manual_control": bool(device.get("manual_control")),
                       "input_handoff": bool(device.get("input_handoff"))}
            changed = [field for field, value in expected.items() if current[field] != value]
            return {"changed": changed, "expected": expected, "current": current} if changed else None

    def finish_catalogue(self, probe, evidence):
        self.store.require_device(probe["device_id"])
        with self.db.transaction() as db:
            device = self.db.device(db, probe["device_id"])
            saved = self.db.meta(db, key(device["id"]), {})
            if saved.get("id") != probe["id"]:
                return
            valid = (
                device["revision"] == probe["revision"]
                and device["intent_version"] == probe["intent"]
                and assignment_matches(device, probe["content_id"])
                and device["automation"] == "active"
                and not device.get("manual_control")
                and not device.get("input_handoff")
            )
            result_context = (evidence.get("results") or {}).get("network_context_id")
            current_scope = device.get("prime_network_scope")
            if evidence.get("results") and current_scope and result_context != current_scope.get("context_id"):
                valid = False
            if valid and evidence.get("results"):
                # The initial health read may have discovered a replacement
                # network context. Bind the completed decision to that context,
                # retaining the original signature for incident analysis.
                evidence["initial_signature"] = probe["signature"]
                evidence["signature"] = signature(device, {
                    "content_id": probe["content_id"],
                    "snapshot": probe["request"]["content_snapshot"],
                    "viewing_options": probe["request"]["allowed_viewing_options"],
                })
                evidence["network_scope"] = current_scope
            now = datetime.now(UTC)
            state = evidence.get("state", "access_unknown") if valid else "discarded"
            error = evidence.get("error") or (evidence.get("audit") or {}).get("error") or {}
            # A rejected read stays rejected even if the reservation returns to
            # the original event before this transaction acquires the writer.
            # It says nothing about entitlement and must not impose its backoff.
            superseded = bool(evidence.get("invalidation") or error.get("code") == "prime_stale_result")
            if superseded:
                valid, state = False, "discarded"
            if valid and state == "ready" and not error and is_idle(probe["request"]):
                if renew_idle_evidence(db, self.db, device, probe["request"], evidence.get("results") or {},
                                       evidence.get("selected")) is None:
                    valid, state = False, "discarded"
            rule = self.network_policy.rule(probe["request"]["allowed_viewing_options"], probe["request"]["content_snapshot"])
            fallback = state
            if valid and rule and (
                (not error and fallback in rule["fallback_on"])
                or error.get("code") == "prime_network_recovering"
            ):
                state = "network_acquisition_required"
            adaptation = device.get("prime_runtime_adaptation") or {}
            retry_seconds = (
                adaptation.get("retry_after_seconds", 15)
                if error.get("code") == "prime_runtime_recovering" else 60
            )
            evidence.update(
                state=state,
                finished_at=now.isoformat(),
                # Matching time must not consume the executor's handoff window.
                # This is a selection decision, not a retained player handle:
                # Play resolves the ID again under current ownership and network.
                selected_at=now.timestamp(),
                expires=now.timestamp() + SELECTION_HANDOFF_SECONDS,
                retry_at=time.time() + retry_seconds if valid else 0,
            )
            self.db.set_meta(db, key(device["id"]), evidence)
            assignment = device.get("stream_assignment") or {}
            if (assignment.get("catalogue") or {}).get("id") == probe["id"]:
                if state in {"ready", "network_acquisition_required"}:
                    assignment["catalogue"]["expires"] = evidence["expires"]
                else:
                    assignment.pop("catalogue", None)
                self.db.save_device(db, device)
            if superseded:
                self.db.log(db, now.isoformat(), "Prime catalogue check superseded; rechecking current selection",
                            json.dumps({"probe_id": probe["id"], "content_id": probe["content_id"],
                                        "error": error, "invalidation": evidence.get("invalidation")}),
                            "prime_catalogue", device["id"])
            if not valid or (evidence.get("error") or {}).get("code") == "prime_device_recovery":
                return
            if not error:
                record_entitled(db, device["id"], probe["request"], evidence.get("selected"),
                                source="foreground_catalogue", provenance={
                                    "probe_id": probe["id"], "session_id": evidence.get("session_id"),
                                    "network_context_id": (evidence.get("results") or {}).get("network_context_id"),
                                })
            reasons = {
                "network_acquisition_required": "Acquiring the feed on a configured network; playback will be interrupted",
                "ready": "Prime catalogue reports an entitled live feed",
                "waiting_for_feed": "Prime feed is upcoming; catalogue check retries in 60 seconds",
                "access_unknown": "Prime entitlement or event state is unknown; catalogue check retries in 60 seconds",
                "feeds_unavailable": "Matching Prime feeds are unavailable; watch plan retained",
                "feeds_locked": "No entitled English or unlabeled feed on the inspected matching Prime routes; watch plan retained",
                "no_matching_feed": "No matching live or upcoming Prime feed found; watch plan retained",
            }
            reason = reasons[state]
            from ..event_attempts import CONCLUSIVE_FAILURES, attempt_status, record_failure
            # A fallback-eligible preflight becomes network_acquisition_required
            # above. Count only the completed cycle, not each individual route.
            if not error:
                record_failure(device, probe["content_id"], probe["id"], state, now.isoformat())
            budget = attempt_status(device, probe["content_id"])
            if budget and budget["exhausted"]:
                reason = budget["message"]
            if state == "access_unknown":
                error = evidence.get("error") or (evidence.get("audit") or {}).get("error") or {}
                reason = CATALOGUE_ERROR_MESSAGES.get(error.get("code"), reason)
                if error.get("code") == "prime_runtime_recovering":
                    reason = recovery_message(adaptation)
            broadcast_decision = (evidence.get("audit") or {}).get("broadcast_selection") or {}
            if state == "no_matching_feed" and broadcast_decision.get("match_status") == "no_match":
                reason = "No English or unlabeled Prime broadcast on the permitted route; watch plan retained"
            device.setdefault("prime_access", {})[probe["content_id"]] = {
                "state": state,
                "scope": device.get("prime_network_scope"),
                "expires_at": (time.time() + self.network_policy.ttl)
                if device.get("prime_network_scope") or (not error and state in CONCLUSIVE_FAILURES) else None,
                "reason": reason,
                "observed_at": now.isoformat(),
                "retry_after": (now + timedelta(seconds=retry_seconds)).isoformat()
                if state in {"waiting_for_feed", "access_unknown"}
                else None,
                "options": probe["request"]["allowed_viewing_options"],
                "catalogue_probe_id": probe["id"],
                "runtime_error_code": error.get("code") if error.get("code") in COMPATIBILITY_MESSAGES else None,
                "runtime_capability": evidence.get("runtime_capability") if error.get("code") in COMPATIBILITY_MESSAGES else None,
            }
            self.db.save_device(db, device)
            self.db.log(
                db,
                now.isoformat(),
                reason,
                json.dumps(
                    {
                        "probe_id": probe["id"],
                        "content_id": probe["content_id"],
                        "selected": evidence.get("selected"),
                        "broadcast_selection": (evidence.get("audit") or {}).get("broadcast_selection"),
                        "parent_selections": (evidence.get("audit") or {}).get("parent_selections"),
                        "error": error or None,
                    }
                ),
                "prime_catalogue",
                device["id"],
            )

    def prepared_catalogue(self, request, session, *, generation=None, network_context_id=None):
        self.store.require_device(request["device_id"])
        with self.db.snapshot() as db:
            if not assignment_matches(self.db.device(db, self.device_id), request["content_id"]):
                return None
            probe = self.db.meta(db, key(request["device_id"]), {})
        if (
            probe.get("state") == "ready"
            and (probe.get("selected") or {}).get("identity_contract") == IDENTITY_CONTRACT
            and probe.get("launch_intent") == request["intent_version"]
            and probe.get("content_id") == request["content_id"]
            and probe.get("session_id") == session
            and probe.get("results", {}).get("network_context_id") == network_context_id
            and (generation is None or probe.get("results", {}).get("generation") == generation)
            and time.time() < probe.get("expires", 0)
            and snapshot_identity(probe.get("request", {}).get("content_snapshot") or {}) == snapshot_identity(request["content_snapshot"])
            and probe.get("request", {}).get("allowed_viewing_options") == request["allowed_viewing_options"]
        ):
            return probe
        return None
