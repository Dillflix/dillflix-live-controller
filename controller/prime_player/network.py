"""Durable bounded network acquisition, using the normal feed chooser."""
import asyncio
import sqlite3
import time
from uuid import uuid4

from ..executor.models import ExecutorError
from ..model_diagnostics import capture_model_calls_async
from ..network_policy import NetworkPolicy
from ..planner import parse_time
from .matching import selection_state
from .search import choose_search

NETWORK_ERRORS = {
    "network_permission_required": "VPN setup requires Android and OpenVPN API consent",
    "network_profile_missing": "The configured OpenVPN profile is missing from the device",
    "network_auth_failed": "OpenVPN authentication failed; check provisioned service credentials",
    "network_profile_unqualified": "The configured VPN profile requires device qualification",
    "network_target_unknown": "The configured network target is absent from Prime Player",
    "network_transition_budget": "Network transition rate limit reached; allow the device to settle",
    "network_deadline_expired": "Network acquisition deadline expired",
}


class NetworkAcquisition:
    @property
    def network_policy(self):
        # The executor currently serves living-room. Read without a nested write
        # transaction: catalogue callers may already hold BEGIN IMMEDIATE.
        db = sqlite3.connect(self.db.path)
        db.row_factory = sqlite3.Row
        try:
            value = self.db.device(db)["preferences"].get("vpn_policy")
        finally:
            db.close()
        return NetworkPolicy(value if value is not None else self.config.prime_network_policy)

    def navigation_budget(self, options, default, snapshot=None):
        return max(default, self.network_policy.timeout) if self.network_policy.rule(options, snapshot) else default

    def _network_allowed(self, token, acquisition):
        with self.db.snapshot() as db:
            self.store.allowed(db, token, navigation=True)
        if acquisition["policy_revision"] != self.network_policy.revision:
            raise ExecutorError("prime_network_policy_changed", "Network policy changed; reevaluate the watch plan")
        if time.time() >= acquisition["deadline_at"]:
            raise ExecutorError("prime_network_deadline", "Network acquisition deadline expired")

    def _save_network(self, token, acquisition):
        self._network_allowed(token, acquisition)
        self.save_workflow(token, network_acquisition=acquisition)

    async def network_transition(self, token, acquisition, target):
        """One operation ID and exact request survive transport/process failures."""
        op = acquisition.get("operation")
        if not op:
            if acquisition["transitions"] >= (await self.database_call(lambda: self.network_policy)).maximum:
                raise ExecutorError("prime_network_exhausted", "Configured network transition limit reached")
            op = {"operation_id": uuid4().hex, "target_id": target,
                  "deadline_at": acquisition["deadline_at"], "interrupt_playback": True}
            acquisition.update(operation=op, transitions=acquisition["transitions"] + 1)
            await self.database_call(self._save_network, token, acquisition)  # Must precede the first RPC.
        while True:
            await self.database_call(self._network_allowed, token, acquisition)
            health = await self.check_session(control=True)
            self.player.require(health, "vpn_status", "vpn_connect", "vpn_cancel")
            if health.get("api_version", 0) < 12 or not health.get("network", {}).get("enabled"):
                raise ExecutorError("prime_network_disabled", "Managed networking is not enabled in Prime Player")
            receipt = self.player.control_ownership(health)
            status = await self.player.vpn_status(op["operation_id"])
            await self.database_call(self._network_allowed, token, acquisition)
            if status["state"] == "not_found":
                if receipt.get("mode") != "automatic" or not receipt.get("acknowledged"):
                    raise ExecutorError("prime_ownership_unavailable", "Waiting for automatic device ownership")
                def invalidate_observation():
                    with self.db.transaction() as db:
                        row = self.store.allowed(db, token, navigation=True)
                        device = self.db.device(db, row["device_id"])
                        if device.get("observed"):
                            device["observed"]["verified"] = False
                        self.db.save_device(db, device)
                await self.database_call(invalidate_observation)
                try:
                    await self.mutation(token, "PRIME_NETWORK_TRANSITION",
                                        lambda: self.player.vpn_connect(op, receipt))
                except ExecutorError as exc:
                    if exc.code not in {"prime_transport_unknown", "prime_operation_unknown"}:
                        raise
                # Inspect this ID, including after uncertain delivery. Never
                # allocate a new request ID merely because the response was lost.
            elif status["state"] == "ready":
                latest = await self.check_session(control=True)
                network = latest.get("network", {})
                if (latest["session_id"] != status.get("session_id") or network.get("context_id") != status.get("context_id")
                        or network.get("state") != "ready"):
                    raise ExecutorError("prime_network_context_changed", "Completed transition no longer owns the network context")
                acquisition.update(operation=None, last_operation=op, context_id=status["context_id"], target_id=target)
                await self.database_call(self._save_network, token, acquisition)
                return latest
            elif status["state"] in {"failed", "cancelled"}:
                message = NETWORK_ERRORS.get(status.get("error"), "Prime network transition failed; inspect its operation status")
                if status.get("failed_phase") == "starting_prime":
                    message = "Network connected, but Prime initialization did not complete"
                raise ExecutorError("prime_network_transition_failed", message)
            await self.database_call(self.store.phase, token, "network_" + status.get("phase", "pending"))
            await asyncio.sleep(2)

    async def acquire_network_catalogue(self, token, request):
        rule = (await self.database_call(lambda: self.network_policy)).rule(request["allowed_viewing_options"], request["content_snapshot"])
        if not rule:
            return None
        workflow = (await self.database_call(self.store.report, token)).get("prime_player") or {}
        acquisition = workflow.get("network_acquisition")
        if not acquisition:
            acquisition = {"policy_revision": (await self.database_call(lambda: self.network_policy)).revision, "tried": [], "transitions": 0,
                           "deadline_at": min(parse_time(request["deadline_at"]).timestamp(), time.time() + (await self.database_call(lambda: self.network_policy)).timeout),
                           "operation": None, "policy_matches": rule.get("matched_policies", []), "attempts": []}
            await self.database_call(self._save_network, token, acquisition)
        def read_timezone():
            with self.db.snapshot() as db:
                timezone = self.db.device(db, request["device_id"])["preferences"]["timezone"]
            return timezone
        timezone = await self.database_call(read_timezone)
        while True:
            await self.database_call(self._network_allowed, token, acquisition)
            health = await self.check_session(control=True)
            network = health.get("network", {})
            if not network.get("enabled"):
                raise ExecutorError("prime_network_disabled", "Configured network acquisition requires enabled Prime Player networking")
            if acquisition.get("operation"):
                health = await self.network_transition(token, acquisition, acquisition["operation"]["target_id"])
                network = health["network"]
            if network.get("state") != "ready":
                # Startup/adoption already belongs to the player. Do not replace it.
                await self.database_call(self.store.phase, token, "network_recovering")
                await asyncio.sleep(2)
                continue
            target = network["context"]["target_id"]
            if target in acquisition["tried"]:
                previous = acquisition.get("last_state")
                choices = [t for t in rule["targets"] if t not in acquisition["tried"]]
                if "matched_policies" in rule and previous in rule["fallback_on"]:
                    inventory = await self.player.vpn_locations()
                    await self.database_call(self._network_allowed, token, acquisition)
                    available = {p["id"] for p in inventory.get("locations", [])
                                 if p.get("qualified") is True and p.get("routing_confirmed") is True}
                    skipped = [t for t in choices if t != "direct" and t not in available]
                    acquisition["unavailable_targets"] = skipped
                    choices = [t for t in choices if t == "direct" or t in available]
                    await self.database_call(self._save_network, token, acquisition)
                if previous not in rule["fallback_on"] or not choices:
                    await self.database_call(self.complete_search, token, previous or "access_unknown", error=acquisition.get("last_error"))
                    return False
                await self.network_transition(token, acquisition, choices[0])
                continue
            session, context = health["session_id"], network["context_id"]
            await self.database_call(self.save_workflow, token, session_id=session, network_context_id=context,
                               ownership=self.player.ownership(health, automatic=True))
            prepared = (await self.database_call(self.prepared_catalogue, request, session, generation=health.get("compatibility", {}).get("generation"), network_context_id=context))
            if prepared:
                return prepared
            evidence = {"id": "network-" + uuid4().hex, "network_context_id": context}
            received = {}

            async def current(capability):
                await self.database_call(self._network_allowed, token, acquisition)
                value = await self.check_session(session, capabilities=(capability,))
                await self.database_call(self._network_allowed, token, acquisition)
                if value.get("network", {}).get("context_id") != context:
                    raise ExecutorError("prime_network_context_changed", "Network changed during feed acquisition")
                return value

            async def search(query):
                value = await current("search")
                await self.database_call(self.store.phase, token, "searching")
                results = await self.mutation(token, "PRIME_SEARCH", lambda: self.player.search(
                    query, self.config.prime_search_timeout, self.player.ownership(value, automatic=True)))
                self.require_catalogue_generation(value, results)
                if results.get("session_id") != session:
                    raise ExecutorError("prime_stale_result", "Search belongs to a replaced session")
                received[query] = time.time()
                evidence.update(query=query, results=results, received_at=received[query])
                await self.database_call(self.store.phase, token, "matching")
                return results

            async def broadcasts(results, content_id):
                value = await current("broadcasts")
                self.require_catalogue_generation(value, results)
                await self.database_call(self.store.phase, token, "broadcasts")
                result = await self.mutation(token, "PRIME_BROADCASTS", lambda: self.player.broadcasts(
                    content_id, self.player.ownership(value, automatic=True)))
                await self.database_call(self.store.phase, token, "matching")
                return result

            async with capture_model_calls_async(lambda e: self.save_model_call(token, e), recorder=self.model_captures.record,
                                     context={"source": "network_acquisition", "token": token, "content_id": request["content_id"],
                                              "network_context_id": context, "target_id": target}):
                results, selected, audit = await choose_search(request, timezone, self.matcher, search, broadcasts,
                                                             lambda a: self.database_call(self.save_workflow, token, selection=a))
            await current("play")
            state = selection_state(selected, audit)
            evidence.update(results=results, selected=selected, audit=audit, selected_at=time.time(),
                            query=results["query"], received_at=received[results["query"]])
            await self.database_call(self.save_workflow, token, selection=audit, catalogue=results)
            reason = state
            acquisition.setdefault("attempts", []).append({"target_id": target, "context_id": context, "state": reason, "audit": audit})
            if state == "ready":
                await self.database_call(self._save_network, token, acquisition)
                return evidence
            acquisition["tried"].append(target)
            acquisition.update(last_state=state, last_error=audit.get("error"))
            await self.database_call(self._save_network, token, acquisition)

    async def interrupt_network(self, token, workflow):
        operation = (workflow.get("network_acquisition") or {}).get("operation")
        if not operation:
            return False
        health = await self.check_session(control=True)
        receipt = self.player.control_ownership(health)
        result = await self.player.vpn_cancel(operation["operation_id"], receipt)
        if not result.get("acknowledged"):
            raise ExecutorError("prime_network_settling", "Waiting for network transition cleanup before releasing input")
        await self.database_call(self.save_cancellation, token, network_cancelled=operation["operation_id"])
        return True
