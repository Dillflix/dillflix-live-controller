from dataclasses import replace

import pytest
from playback_fixtures import payload
from test_prime_workflow import Player, cleanup, rig, row

from controller.executor.models import ExecutorError
from controller.network_policy import NetworkPolicy
from controller.prime_player.client import PrimePlayerClient


POLICY = {"enabled": True, "routes": {"prime-option": {
    "targets": ["direct", "calgary"], "fallback_on": ["feeds_unavailable"]}}}


class NetworkPlayer(Player):
    def __init__(self):
        super().__init__()
        self.target, self.context = "direct", "network-direct"
        self.operations = {}
        self.transitions = []
        self.uncertain = False
        self.cancel_settled = True

    async def health(self):
        h = await super().health()
        h["api_version"] = 12
        h["capabilities"] += ["vpn_status", "vpn_connect", "vpn_cancel"]
        h["network"] = {"enabled": True, "state": "ready", "context_id": self.context,
                        "context": {"target_id": self.target}}
        h["ownership"]["network_context_id"] = self.context
        return h

    async def search(self, query, timeout, ownership):
        value = await super().search(query, timeout, ownership)
        value["network_context_id"] = self.context
        if self.target == "direct":
            for item in value["containers"][0]["items"]:
                item["availability_status"] = "UNAVAILABLE"
        return value

    async def broadcasts(self, content_id, ownership):
        return {**await super().broadcasts(content_id, ownership), "network_context_id": self.context}

    async def vpn_status(self, operation_id=None):
        return self.operations.get(operation_id, {"state": "not_found"})

    async def vpn_connect(self, operation, ownership):
        assert operation["operation_id"] not in self.operations
        self.transitions.append(dict(operation))
        self.target = operation["target_id"]
        self.context = "network-" + self.target
        self.session = "session-" + self.target
        self.operations[operation["operation_id"]] = {
            "state": "ready", "context_id": self.context, "session_id": self.session}
        if self.uncertain:
            raise ExecutorError("prime_transport_unknown", "lost response")
        return self.operations[operation["operation_id"]]

    async def vpn_cancel(self, operation_id, ownership):
        return {"acknowledged": self.cancel_settled, "state": "cancelled" if self.cancel_settled else "pending"}


def network_rig(tmp_path):
    c, w = rig(tmp_path)
    w.config = replace(w.config, prime_network_policy=POLICY)
    w.player = NetworkPlayer()
    return c, w


@pytest.mark.parametrize("uncertain", [False, True])
async def test_unavailable_network_fallback_then_existing_live_launch(tmp_path, uncertain):
    c, w = network_rig(tmp_path)
    w.player.uncertain = uncertain
    try:
        report, _ = w.store.submit(payload())
        await w.navigate(row(w, report["token"]))
        final = w.store.report(report["token"])
        assert final["operation"]["state"] == "playing_verified", final
        assert len(w.player.transitions) == 1
        assert final["prime_player"]["network_context_id"] == "network-calgary"
        assert final["prime_player"]["session_id"] == "session-calgary"
    finally:
        await cleanup(c)


async def test_recovery_inspects_durable_operation_instead_of_resending(tmp_path):
    c, w = network_rig(tmp_path)
    try:
        request = payload()
        report, _ = w.store.submit(request)
        token = report["token"]
        from controller.planner import parse_time
        op = {"operation_id": "saved", "target_id": "calgary", "deadline_at": parse_time(request["deadline_at"]).timestamp(),
              "interrupt_playback": True}
        a = {"operation": op, "policy_revision": w.network_policy.revision, "deadline_at": op["deadline_at"],
             "tried": ["direct"], "transitions": 1, "last_state": "feeds_unavailable"}
        w.save_workflow(token, network_acquisition=a)
        await w.player.vpn_connect(op, {})
        w.recover()
        await w.navigate(row(w, token))
        assert w.store.report(token)["operation"]["state"] == "playing_verified"
        assert len(w.player.transitions) == 1
    finally:
        await cleanup(c)


async def test_network_cancel_must_settle_before_handoff(tmp_path):
    c, w = network_rig(tmp_path)
    try:
        report, _ = w.store.submit(payload())
        workflow = {"network_acquisition": {"operation": {"operation_id": "old"}}}
        w.player.cancel_settled = False
        with pytest.raises(ExecutorError, match="cleanup"):
            await w.interrupt_network(report["token"], workflow)
        w.player.cancel_settled = True
        assert await w.interrupt_network(report["token"], workflow)
    finally:
        await cleanup(c)


def test_policy_cannot_authorize_unknown_routes_or_subscription_errors():
    policy = NetworkPolicy(POLICY)
    assert policy.rule([{"id": "other", "app": "prime_video"}]) is None
    assert policy.rule(payload()["allowed_viewing_options"])["targets"] == ["direct", "calgary"]
    with pytest.raises(ValueError):
        NetworkPolicy({"enabled": True, "routes": {"x": {"targets": ["direct"], "fallback_on": ["feeds_locked"]}}})


def test_network_recovery_is_not_runtime_incompatibility():
    with pytest.raises(ExecutorError) as error:
        PrimePlayerClient.require({"api_version": 12, "network": {"enabled": True, "state": "transitioning"}}, "search")
    assert error.value.code == "prime_network_recovering"
