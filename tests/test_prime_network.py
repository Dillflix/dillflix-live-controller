from dataclasses import replace
import json
import time
from datetime import UTC, datetime, timedelta

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

    def outcome(self):
        value = {**super().outcome(), "network_context_id": self.context}
        if self.target == "direct":
            value.update(state="failed", resolved_id=None, reason="Resolver declined live playback",
                         evidence={"launch": {"disposition": "not_invoked"}})
        return value

    async def broadcasts(self, content_id, ownership):
        return {**await super().broadcasts(content_id, ownership), "network_context_id": self.context}

    async def vpn_locations(self):
        return {"enabled": True, "locations": [
            {"id": target, "qualified": True, "routing_confirmed": True}
            for target in ("toronto", "montreal", "calgary")
        ]}

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


@pytest.mark.parametrize("failed_read", ["health", "attempt"])
@pytest.mark.parametrize("error_code", ["prime_busy", "prime_transport_unknown", "prime_operation_unknown"])
async def test_launch_read_failure_retains_attempt_until_observation_recovers(tmp_path, failed_read, error_code):
    c, w = network_rig(tmp_path)
    w.player.target = "calgary"
    original_play = w.player.play
    original_read = getattr(w.player, failed_read)

    async def pending_play(*args):
        return {**await original_play(*args), "state": "launch_requested"}

    async def unavailable_read(*args):
        if w.player.attempt_id:
            raise ExecutorError(error_code, "Observation unavailable")
        return await original_read(*args)

    w.player.play = pending_play
    setattr(w.player, failed_read, unavailable_read)
    try:
        report, _ = w.store.submit(payload())
        token = report["token"]
        deadline = report["operation"]["deadline_at"]
        for _ in range(2):
            await w.navigate(row(w, token))
            current = w.store.report(token)
            assert current["operation"]["state"] == "navigating"
            assert current["operation"]["deadline_at"] == deadline
            assert current["observation_status"]["error"]["code"] == error_code
            assert row(w, token)["cancel_requested"] == 0
            assert row(w, token)["next_check"] > time.time()
        attempt = w.player.attempt_id
        setattr(w.player, failed_read, original_read)
        await w.navigate(row(w, token))
        assert w.store.report(token)["operation"]["state"] == "playing_verified"
        assert w.store.report(token)["observation_status"]["error"] is None
        assert w.player.attempt_id == attempt
        assert len([call for call in w.player.calls if call[0] == "play"]) == 1
        assert not any(call[0] in {"cancel", "stop"} for call in w.player.calls)
        assert not w.player.transitions
    finally:
        await cleanup(c)


@pytest.mark.parametrize("error_code", ["prime_busy", "prime_transport_unknown"])
async def test_launch_observation_retry_does_not_extend_deadline(tmp_path, error_code):
    c, w = network_rig(tmp_path)
    try:
        report, _ = w.store.submit(payload())
        token = report["token"]
        w.store.phase(token, "verifying")
        w.defer_launch_observation(token, ExecutorError(error_code, "Observation unavailable"))
        request = json.loads(row(w, token)["request"])
        request["deadline_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with w.db.transaction() as db:
            db.execute("UPDATE executor_jobs SET request=? WHERE token=?", (json.dumps(request), token))
        await w.navigate(row(w, token))
        assert w.store.report(token)["operation"]["state"] == "timed_out"
        assert row(w, token)["cancel_requested"] == 1
        assert not w.player.calls
    finally:
        await cleanup(c)


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


CITY_POLICY = {"enabled": True, "locations": [
    {"target_id": "toronto", "team_cities": ["Toronto", "Hamilton", "Ottawa"]},
    {"target_id": "montreal", "team_cities": ["Montréal"]},
], "acquisition_timeout": 1800}


@pytest.mark.parametrize("cities,expected", [
    (("Toronto", "Boston"), ["toronto", "direct"]),
    (("Ottawa", "MONTREAL"), ["toronto", "montreal", "direct"]),
    (("Boston", "Seattle"), ["direct"]),
    ((None, None), ["direct"]),
])
def test_city_policy_uses_either_structured_team_city(cities, expected):
    request = payload()
    for side, city in zip(("away", "home"), cities):
        request["content_snapshot"]["event"][side + "_team_details"]["city"] = city
    request["content_snapshot"]["title"] = "Toronto at Montreal"
    request["content_snapshot"]["event"]["venue"] = {"city": "Toronto"}
    rule = NetworkPolicy(CITY_POLICY).rule(request["allowed_viewing_options"], request["content_snapshot"])
    assert rule["targets"] == expected
    assert "feeds_locked" in rule["fallback_on"]
    assert "feeds_language" in rule["fallback_on"]
    assert "access_unknown" not in rule["fallback_on"]
    assert "waiting_for_feed" not in rule["fallback_on"]


async def test_saved_city_policy_overrides_environment_and_fences_edits(tmp_path):
    c, w = network_rig(tmp_path)
    try:
        with c.db.transaction() as db:
            device = c.db.device(db)
            device["preferences"]["vpn_policy"] = CITY_POLICY
            c.db.save_device(db, device)
        assert w.network_policy.locations == CITY_POLICY["locations"]
        old = w.network_policy.revision
        with c.db.transaction() as db:
            device = c.db.device(db)
            device["preferences"]["vpn_policy"] = {**CITY_POLICY, "enabled": False}
            c.db.save_device(db, device)
        assert not w.network_policy.enabled
        assert old != w.network_policy.revision
    finally:
        await cleanup(c)


@pytest.mark.parametrize("initial,city,expected", [
    ("direct", "Toronto", ["toronto"]),
    ("montreal", "Boston", []),
])
async def test_city_acquisition_searches_current_before_switching(tmp_path, initial, city, expected):
    c, w = network_rig(tmp_path)
    try:
        with c.db.transaction() as db:
            device = c.db.device(db)
            device["preferences"]["vpn_policy"] = CITY_POLICY
            c.db.save_device(db, device)
        w.player.target = initial
        w.player.context = "network-" + initial
        request = payload()
        request["content_snapshot"]["event"]["home_team_details"]["city"] = city
        report, _ = w.store.submit(request)
        await w.navigate(row(w, report["token"]))
        assert w.store.report(report["token"])["operation"]["state"] == "playing_verified"
        assert [v["target_id"] for v in w.player.transitions] == expected
    finally:
        await cleanup(c)


def test_invalid_city_policy_is_rejected():
    from controller.models import VPNPolicy
    for value in (
        {"locations": [{"target_id": "direct", "team_cities": ["Toronto"]}]},
        {"locations": [{"target_id": "montreal", "team_cities": ["Montréal", "montreal"]}]},
        {"locations": [{"target_id": "toronto", "team_cities": [" "]}]},
    ):
        with pytest.raises(ValueError):
            VPNPolicy.model_validate(value)


@pytest.mark.parametrize("initial,cities,outcomes,expected", [
    ("montreal", ["Toronto"], {"montreal": "feeds_locked", "toronto": "ready"}, ["toronto"]),
    ("toronto", ["Boston"], {"toronto": "feeds_unavailable", "direct": "ready"}, ["direct"]),
    ("direct", ["Boston"], {"direct": "no_matching_feed"}, []),
    ("direct", ["Toronto"], {"direct": "access_unknown"}, []),
    ("direct", ["Toronto"], {"direct": "waiting_for_feed"}, []),
    ("direct", ["Toronto", "Montreal"], {"direct": "feeds_locked", "toronto": "feeds_locked", "montreal": "ready"}, ["toronto", "montreal"]),
])
async def test_city_fallback_is_selective_and_never_revisits(tmp_path, monkeypatch, initial, cities, outcomes, expected):
    c, w = network_rig(tmp_path)
    try:
        with c.db.transaction() as db:
            device = c.db.device(db)
            device["preferences"]["vpn_policy"] = CITY_POLICY
            c.db.save_device(db, device)
        w.player.target = initial
        w.player.context = "network-" + initial
        request = payload()
        for side, city in zip(("away", "home"), cities):
            request["content_snapshot"]["event"][side + "_team_details"]["city"] = city
        report, _ = w.store.submit(request)
        visited = []
        async def choose(*args):
            visited.append(w.player.target)
            return {}, {"readiness": outcomes[w.player.target]}, {}
        monkeypatch.setattr("controller.prime_player.network.choose_search", choose)
        await w.acquire_network_catalogue(report["token"], request)
        assert visited == [initial] + expected
        assert [v["target_id"] for v in w.player.transitions] == expected
    finally:
        await cleanup(c)


async def test_unqualified_city_does_not_prevent_direct_fallback(tmp_path, monkeypatch):
    c, w = network_rig(tmp_path)
    try:
        with c.db.transaction() as db:
            device = c.db.device(db)
            device["preferences"]["vpn_policy"] = CITY_POLICY
            c.db.save_device(db, device)
        w.player.target, w.player.context = "toronto", "network-toronto"
        async def inventory():
            return {"locations": [{"id": "montreal", "qualified": False, "routing_confirmed": True}]}
        w.player.vpn_locations = inventory
        request = payload()
        request["content_snapshot"]["event"]["home_team_details"]["city"] = "Montreal"
        report, _ = w.store.submit(request)
        async def choose(*args):
            return {}, {"readiness": "ready" if w.player.target == "direct" else "feeds_locked"}, {}
        monkeypatch.setattr("controller.prime_player.network.choose_search", choose)
        await w.acquire_network_catalogue(report["token"], request)
        assert [v["target_id"] for v in w.player.transitions] == ["direct"]
        assert w.store.report(report["token"])["prime_player"]["network_acquisition"]["unavailable_targets"] == ["montreal"]
    finally:
        await cleanup(c)
