"""Durable bounded network acquisition, using the normal feed chooser."""
import asyncio
import time
from uuid import uuid4

from ..executor.models import ExecutorError
from ..model_diagnostics import capture_model_calls
from ..network_policy import NetworkPolicy
from ..planner import parse_time
from .matching import selection_state
from .search import choose_search


class NetworkAcquisition:
    @property
    def network_policy(self):
        return NetworkPolicy(self.config.prime_network_policy)

    def navigation_budget(self, options, default):
        return max(default, self.network_policy.timeout) if self.network_policy.rule(options) else default

    def _network_allowed(self, token, acquisition):
        with self.db.transaction() as db:
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
            if acquisition["transitions"] >= self.network_policy.maximum:
                raise ExecutorError("prime_network_exhausted", "Configured network transition limit reached")
            op = {"operation_id": uuid4().hex, "target_id": target,
                  "deadline_at": acquisition["deadline_at"], "interrupt_playback": True}
            acquisition.update(operation=op, transitions=acquisition["transitions"] + 1)
            self._save_network(token, acquisition)  # Must precede the first RPC.
        while True:
            self._network_allowed(token, acquisition)
            health = await self.check_session(control=True)
            self.player.require(health, "vpn_status", "vpn_connect", "vpn_cancel")
            if health.get("api_version", 0) < 12 or not health.get("network", {}).get("enabled"):
                raise ExecutorError("prime_network_disabled", "Managed networking is not enabled in Prime Player")
            receipt = self.player.control_ownership(health)
            status = await self.player.vpn_status(op["operation_id"])
            self._network_allowed(token, acquisition)
            if status["state"] == "not_found":
                if receipt.get("mode") != "automatic" or not receipt.get("acknowledged"):
                    raise ExecutorError("prime_ownership_unavailable", "Waiting for automatic device ownership")
                with self.db.transaction() as db:
                    row = self.store.allowed(db, token, navigation=True)
                    device = self.db.device(db, row["device_id"])
                    if device.get("observed"):
                        device["observed"]["verified"] = False
                    self.db.save_device(db, device)
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
                self._save_network(token, acquisition)
                return latest
            elif status["state"] in {"failed", "cancelled"}:
                raise ExecutorError("prime_network_transition_failed", "Prime network transition failed; inspect its operation status")
            self.store.phase(token, "network_" + status.get("phase", "pending"))
            await asyncio.sleep(2)

    async def acquire_network_catalogue(self, token, request):
        rule = self.network_policy.rule(request["allowed_viewing_options"])
        if not rule:
            return None
        workflow = self.store.report(token).get("prime_player") or {}
        acquisition = workflow.get("network_acquisition")
        if not acquisition:
            acquisition = {"policy_revision": self.network_policy.revision, "tried": [], "transitions": 0,
                           "deadline_at": min(parse_time(request["deadline_at"]).timestamp(), time.time() + self.network_policy.timeout),
                           "operation": None}
            self._save_network(token, acquisition)
        with self.db.transaction() as db:
            timezone = self.db.device(db, request["device_id"])["preferences"]["timezone"]
        while True:
            self._network_allowed(token, acquisition)
            health = await self.check_session(control=True)
            network = health.get("network", {})
            if not network.get("enabled"):
                raise ExecutorError("prime_network_disabled", "Configured network acquisition requires enabled Prime Player networking")
            if acquisition.get("operation"):
                health = await self.network_transition(token, acquisition, acquisition["operation"]["target_id"])
                network = health["network"]
            if network.get("state") != "ready":
                # Startup/adoption already belongs to the player. Do not replace it.
                self.store.phase(token, "network_recovering")
                await asyncio.sleep(2)
                continue
            target = network["context"]["target_id"]
            if target in acquisition["tried"]:
                previous = acquisition.get("last_state")
                choices = [t for t in rule["targets"] if t not in acquisition["tried"]]
                if previous not in rule["fallback_on"] or not choices:
                    self.complete_search(token, previous or "access_unknown", error=acquisition.get("last_error"))
                    return False
                await self.network_transition(token, acquisition, choices[0])
                continue
            session, context = health["session_id"], network["context_id"]
            self.save_workflow(token, session_id=session, network_context_id=context,
                               ownership=self.player.ownership(health, automatic=True))
            prepared = self.prepared_catalogue(request, session, generation=health.get("compatibility", {}).get("generation"), network_context_id=context)
            if prepared:
                return prepared
            evidence = {"id": "network-" + uuid4().hex, "network_context_id": context}

            async def current(capability):
                self._network_allowed(token, acquisition)
                value = await self.check_session(session, capabilities=(capability,))
                if value.get("network", {}).get("context_id") != context:
                    raise ExecutorError("prime_network_context_changed", "Network changed during feed acquisition")
                return value

            async def search(query):
                value = await current("search")
                self.store.phase(token, "searching")
                results = await self.mutation(token, "PRIME_SEARCH", lambda: self.player.search(
                    query, self.config.prime_search_timeout, self.player.ownership(value, automatic=True)))
                self.require_catalogue_generation(value, results)
                if results.get("session_id") != session:
                    raise ExecutorError("prime_stale_result", "Search belongs to a replaced session")
                evidence.update(query=query, results=results, received_at=time.time())
                return results

            async def broadcasts(results, content_id):
                value = await current("broadcasts")
                self.require_catalogue_generation(value, results)
                return await self.mutation(token, "PRIME_BROADCASTS", lambda: self.player.broadcasts(
                    content_id, self.player.ownership(value, automatic=True)))

            with capture_model_calls(lambda e: self.save_model_call(token, e), recorder=self.model_captures.record,
                                     context={"source": "network_acquisition", "token": token, "content_id": request["content_id"],
                                              "network_context_id": context, "target_id": target}):
                results, selected, audit = await choose_search(request, timezone, self.matcher, search, broadcasts,
                                                             lambda a: self.save_workflow(token, selection=a))
            await current("play")
            state = selection_state(selected, audit)
            evidence.update(results=results, selected=selected, audit=audit)
            self.save_workflow(token, selection=audit, catalogue=results)
            if state == "ready":
                return evidence
            acquisition["tried"].append(target)
            acquisition.update(last_state=state, last_error=audit.get("error"))
            # A language rejection is not evidence that a different region helps.
            if (audit.get("broadcast_selection") or {}).get("match_status") == "no_match":
                acquisition["last_state"] = "feeds_locked"
            self._save_network(token, acquisition)

    async def interrupt_network(self, token, workflow):
        operation = (workflow.get("network_acquisition") or {}).get("operation")
        if not operation:
            return False
        health = await self.check_session(control=True)
        receipt = self.player.control_ownership(health)
        result = await self.player.vpn_cancel(operation["operation_id"], receipt)
        if not result.get("acknowledged"):
            raise ExecutorError("prime_network_settling", "Waiting for network transition cleanup before releasing input")
        self.save_cancellation(token, network_cancelled=operation["operation_id"])
        return True
