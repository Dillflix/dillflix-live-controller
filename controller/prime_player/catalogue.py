"""On-demand catalogue checks before committing a playback switch."""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ..executor.models import ExecutorError
from ..model_diagnostics import capture_model_calls
from .client import (
    COMPATIBILITY_MESSAGES,
    RECOVERING_ADAPTATION_STATES,
    adaptation_status,
    recovery_message,
    rpc_context,
)
from .matching import selection_state
from .search import choose_search

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


def signature(device, item):
    return hashlib.sha256(
        json.dumps(
            [
                device["revision"],
                device["intent_version"],
                item["content_id"],
                item["snapshot"],
                item["viewing_options"],
                device.get("prime_network_scope"),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


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
            device = self.db.device(db)
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
        if self.loop and self.loop.is_running() and not self.stopping:

            def schedule():
                active = getattr(self, "catalogue_task", None)
                if active and not active.done():
                    return  # Next staging tick queues the newest request after this read settles.
                self.catalogue_task = asyncio.create_task(self.observe_catalogue(probe))

            self.loop.call_soon_threadsafe(schedule)
        return {"state": "pending"}

    async def observe_catalogue(self, probe):
        correlation = rpc_context.set({"catalogue_probe_id": probe["id"], "content_id": probe["content_id"]})
        evidence = {**probe, "model_calls": []}
        try:
            async with asyncio.timeout(self.config.prime_search_timeout + self.config.model_timeout + 50):
                session = None

                async def search(query):
                    nonlocal session
                    async with self.input_lock:
                        if not self.catalogue_current(probe):
                            raise ExecutorError("prime_stale_result", "Plan changed before catalogue search")
                        evidence["runtime_capability"] = "search"
                        health = await self.check_session(session, capabilities=("search",))
                        if health.get("api_version", 0) < 11:
                            raise ExecutorError(
                                "prime_api_incompatible", "Prime Player API 11 or newer is required", retryable=False
                            )
                        session = health["session_id"]
                        ownership = self.player.ownership(health, automatic=True)
                        results = await self.player.search(query, self.config.prime_search_timeout, ownership)
                    if results.get("session_id") != session:
                        raise ExecutorError("prime_stale_result", "Catalogue result belongs to another session")
                    self.require_catalogue_generation(health, results)
                    evidence.update(query=query, results=results, session_id=session, received_at=time.time())
                    return results

                async def fetch_broadcasts(results, content_id):
                    async with self.input_lock:
                        if not self.catalogue_current(probe):
                            raise ExecutorError("prime_stale_result", "Plan changed during broadcast selection")
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

                with capture_model_calls(
                    diagnostic, recorder=self.model_captures.record,
                    context={"source": "catalogue", "device_id": probe["device_id"],
                             "content_id": probe["content_id"], "catalogue_probe_id": probe["id"],
                             "intent_version": probe["intent"]},
                ):
                    _, selected, audit = await choose_search(
                        probe["request"], probe["timezone"], self.matcher, search, fetch_broadcasts,
                        lambda audit: evidence.update(audit=audit),
                    )
                state = selection_state(selected, audit)
                evidence.update(state=state, selected=selected, audit=audit)
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
            self.finish_catalogue(probe, evidence)

    def catalogue_current(self, probe):
        with self.db.transaction() as db:
            device = self.db.device(db, probe["device_id"])
            saved = self.db.meta(db, key(device["id"]), {})
            return (
                saved.get("id") == probe["id"]
                and device["revision"] == probe["revision"]
                and device["intent_version"] == probe["intent"]
                and device["automation"] == "active"
                and not device.get("manual_control")
                and not device.get("input_handoff")
            )

    def finish_catalogue(self, probe, evidence):
        with self.db.transaction() as db:
            device = self.db.device(db, probe["device_id"])
            saved = self.db.meta(db, key(device["id"]), {})
            if saved.get("id") != probe["id"]:
                return
            valid = (
                device["revision"] == probe["revision"]
                and device["intent_version"] == probe["intent"]
                and device["automation"] == "active"
                and not device.get("manual_control")
                and not device.get("input_handoff")
            )
            now = datetime.now(UTC)
            state = evidence.get("state", "access_unknown") if valid else "discarded"
            error = evidence.get("error") or (evidence.get("audit") or {}).get("error") or {}
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
                expires=evidence.get("received_at", time.time()) + 60,
                retry_at=time.time() + retry_seconds if valid else 0,
            )
            self.db.set_meta(db, key(device["id"]), evidence)
            if not valid or (evidence.get("error") or {}).get("code") == "prime_device_recovery":
                return
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
                "expires_at": (time.time() + self.network_policy.ttl) if device.get("prime_network_scope") else None,
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
        with self.db.transaction() as db:
            probe = self.db.meta(db, key(request["device_id"]), {})
        if (
            probe.get("state") == "ready"
            and probe.get("launch_intent") == request["intent_version"]
            and probe.get("content_id") == request["content_id"]
            and probe.get("session_id") == session
            and probe.get("results", {}).get("network_context_id") == network_context_id
            and (generation is None or probe.get("results", {}).get("generation") == generation)
            and time.time() < probe.get("expires", 0)
            and probe.get("request", {}).get("content_snapshot") == request["content_snapshot"]
            and probe.get("request", {}).get("allowed_viewing_options") == request["allowed_viewing_options"]
        ):
            return probe
        return None
