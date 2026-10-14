from copy import deepcopy
import threading
from unittest.mock import Mock

import pytest

from dillflix_prime_player.errors import BusyError, NetworkError, OutcomeUnknownError, StaleResultError
from dillflix_prime_player.network import NetworkConfig
from dillflix_prime_player.network_device import NetworkDevice


class Runtime:
    def __init__(self):
        self.manual_hold, self.owner_id = False, None
        self.session = "initial"
        self.current = None
        self.retiring_backend = None
        self.lifecycle_lock = threading.RLock()
        self.held, self.starts, self.retires = False, 0, 0
        self._save_state = Mock()
        self.calls = []
        self.start_ready = True

    def hold_for_network(self):
        self.held = True

    def release_network_hold(self):
        self.held = False

    def health(self):
        return {"api_version": 11, "session_id": self.session, "capabilities": ["search", "play", "cancel"],
                "closed": False, "ownership": {"epoch": 0, "session_id": self.session, "mode": "automatic"},
                "compatibility": {"capabilities": {"search": {"available": True}}}}

    def retire_for_network(self):
        self.retires += 1
        self.current = None
        self.session = "retired-" + str(self.retires)

    def start_for_network(self, first=False):
        if first:
            self.starts += 1
        if not self.start_ready:
            return False
        self.session = "runtime-" + str(self.starts)
        self.current = Mock(health=self.health)
        return True

    def search(self, **params):
        self.calls.append(params)
        return {"session_id": self.session, "items": []}

    def close(self):
        pass


class Bridge:
    def __init__(self):
        self.value = {"bridge_instance_id": "bridge-1", "boot_id": "boot-1", "generation": 1,
                      "state": "direct", "profile_uuid": None, "verified": True,
                      "bridge_version": "1", "vpn_version": "qualified"}
        self.ops, self.calls = {}, []
        self.lose_reply = False
        self.settled = True

    def request(self, method, **params):
        self.calls.append((method, params))
        if method == "status":
            return deepcopy(self.value)
        if method == "transition":
            self.value.update(profile_uuid=params["profile_uuid"],
                              state="connected" if params["profile_uuid"] else "direct")
            self.value["generation"] += 1
            self.ops[params["operation_id"]] = {"state": "ready"}
            if self.lose_reply:
                self.lose_reply = False
                raise OutcomeUnknownError("lost response after dispatch")
        if method == "operation":
            return self.ops.get(params["operation_id"], {"state": "not_found"})
        if method == "cancel":
            return {"settled": self.settled}
        return {}


@pytest.fixture
def device(tmp_path):
    cfg = NetworkConfig({"enabled": True, "token_file": str(tmp_path / "secret"), "profiles": [
        {"id": "calgary", "profile_uuid": "profile-a", "country": "CA", "province": "AB",
         "city": "Calgary", "server": "qualified-server", "routing_confirmed": True}]})
    d = NetworkDevice(Runtime(), tmp_path, config=cfg, bridge=Bridge(), manual_input=Mock(), clock=lambda: 1000)
    for _ in range(5):
        d.tick()
    assert d.health()["network"]["state"] == "ready"
    return d


def connect(d, id="test-op", target="calgary", **extra):
    return d.vpn_connect(target, operation_id=id, deadline_at=1500,
                         ownership=d.health()["ownership"], interrupt_playback=True, qualify=True, **extra)


def settle(d, id="test-op"):
    for _ in range(7):
        d.tick()
    return d.vpn_status(id)


def test_old_native_status_is_not_accepted_without_network_context(device):
    device.context = None
    with pytest.raises(NetworkError, match="network_context_unavailable"):
        device.playback_status(attempt_id="old-attempt")


def test_same_boot_bridge_replacement_requires_new_prime_context(device):
    old = device.context["id"]
    starts = device.runtime.starts
    device.bridge.value["bridge_instance_id"] = "bridge-replacement"
    for _ in range(7):
        device.tick()
    assert device.context["id"] != old
    assert device.runtime.starts == starts + 1


def test_disabling_networking_keeps_durable_manual_hold(device):
    d = device
    d.suspend(True, owner_id="operator", previous=d.health()["ownership"])
    runtime = Runtime()
    NetworkDevice(runtime, d.journal.path.parent.parent, config=NetworkConfig())
    assert runtime.manual_hold and runtime.owner_id == "operator"


def test_transition_restarts_prime_once_and_qualifies_only_after_return_to_direct(device):
    d = device
    assert not d.vpn_locations()["locations"][0]["qualified"]
    connect(d)
    assert d.health()["network"]["context_id"] is None
    assert settle(d)["state"] == "ready"
    assert d.runtime.starts == 2 and d.runtime.retires == 2
    assert not d.vpn_locations()["locations"][0]["qualified"]
    connect(d, "return-direct", "direct")
    assert settle(d, "return-direct")["state"] == "ready"
    assert d.vpn_locations()["locations"][0]["qualified"]


def test_lost_response_inspects_original_operation_without_reconnecting(device):
    device.bridge.lose_reply = True
    connect(device)
    assert settle(device)["state"] == "ready"
    assert sum(m == "transition" for m, _ in device.bridge.calls) == 1


def test_idempotency_precedes_stale_ownership_and_conflicting_payload_is_rejected(device):
    receipt = device.health()["ownership"]
    connect(device)
    result = settle(device)
    assert device.vpn_connect("calgary", operation_id="test-op", deadline_at=1500, ownership=receipt,
                              interrupt_playback=True, qualify=True) == result
    with pytest.raises(NetworkError, match="conflict"):
        device.vpn_connect("direct", operation_id="test-op", deadline_at=1500, ownership=receipt,
                           interrupt_playback=True, qualify=True)


def test_search_rejects_old_network_even_with_current_ownership(device):
    old = device.health()["network"]["context_id"]
    connect(device)
    settle(device)
    with pytest.raises(StaleResultError):
        device.search(query="event", ownership=device.health()["ownership"], network_context_id=old)
    assert not device.runtime.calls
    current = device.health()
    result = device.search(query="event", ownership=current["ownership"], network_context_id=current["network"]["context_id"])
    assert result["network_context_id"] == current["network"]["context_id"]


def test_same_verified_target_is_noop_and_does_not_require_interruption(device):
    result = device.vpn_connect("direct", operation_id="noop", deadline_at=1500,
                                 ownership=device.health()["ownership"])
    assert result["noop"] and result["state"] == "ready"
    assert device.runtime.retires == 1


def test_missing_authorization_or_qualification_has_no_device_effect(device):
    for kwargs, code in (({}, "unqualified"), ({"qualify": True}, "authorization")):
        with pytest.raises(NetworkError, match=code):
            device.vpn_connect("calgary", operation_id="refused", deadline_at=1500,
                               ownership=device.health()["ownership"], **kwargs)
    assert device.active is None and device.runtime.retires == 1


def test_prime_startup_failure_does_not_repeat_vpn_connection(device):
    connect(device)
    device.runtime.start_ready = False
    settle(device)
    assert device.active["phase"] == "starting_prime"
    device.runtime.start_ready = True
    assert settle(device)["state"] == "ready"
    assert sum(m == "transition" for m, _ in device.bridge.calls) == 1


def test_restart_recovers_submitted_operation_from_bridge(tmp_path, device):
    connect(device)
    device.tick()  # retirement
    device.tick()  # submitted
    d = NetworkDevice(Runtime(), tmp_path, config=device.config, bridge=device.bridge, manual_input=Mock(), clock=lambda: 1000)
    assert settle(d)["state"] == "ready"
    assert sum(m == "transition" for m, _ in d.bridge.calls) == 1


def test_manual_takeover_waits_for_connection_settlement_and_does_not_disconnect(device):
    connect(device)
    device.tick()
    device.tick()
    device.bridge.settled = False
    receipt = device.suspend(True, owner_id="manual", previous=device.health()["ownership"])
    assert not receipt["acknowledged"]
    device.tick()
    with pytest.raises(BusyError):
        device.manual_input(device.health()["ownership"], 1, key="home")
    device.bridge.settled = True
    device.tick()
    receipt = device.health()["ownership"]
    assert receipt["acknowledged"] and receipt["mode"] == "manual"
    device.manual_input(receipt, 1, key="home")
    device.input.assert_called_once_with(key="home", text=None)
    assert device.bridge.value["profile_uuid"] == "profile-a"
    assert sum(m == "transition" for m, _ in device.bridge.calls) == 1


def test_cancel_before_retirement_keeps_existing_runtime_and_context(device):
    context = device.health()["network"]["context_id"]
    connect(device)
    device.vpn_cancel("test-op", ownership=device.health()["ownership"])
    device.tick()
    assert device.vpn_status("test-op")["state"] == "cancelled"
    assert device.health()["network"]["context_id"] == context
    assert device.runtime.retires == 1


def test_unverified_observation_revokes_context(device):
    device.bridge.value["verified"] = False
    device.tick()
    assert device.health()["network"]["context_id"] is None


def test_runtime_change_cannot_reuse_old_network_context(device):
    device.runtime.session = "unexpected-new-runtime"
    assert device.health()["network"]["context_id"] is None


def test_feature_disabled_delegates_existing_calls_without_requiring_bridge(tmp_path):
    r = Runtime()
    d = NetworkDevice(r, tmp_path)
    assert d.health()["network"]["state"] == "disabled"
    d.search(query="event")
    assert r.calls == [{"query": "event"}]
    with pytest.raises(NetworkError, match="disabled"):
        connect(d)
