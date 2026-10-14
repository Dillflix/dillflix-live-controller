"""Service-lifetime device authority for opt-in managed network transitions.

The runtime's ownership engine remains the child fence for its injected commands;
only this authority accepts public ownership when network management is enabled.
One worker owns lifecycle transitions. Status and cancellation never wait on it.
"""
from copy import deepcopy
from pathlib import Path
import threading
import time
import uuid

from .errors import BusyError, NetworkError, OutcomeUnknownError, StaleResultError
from .network import NetworkConfig, NetworkJournal, deadline, fingerprint, identifier
from .recovery import API_CAPABILITIES

NETWORK_APIS = ["vpn_locations", "vpn_status", "vpn_connect", "vpn_disconnect", "vpn_cancel"]
SCOPED = {"search", "broadcasts", "resolve", "discover", "pages", "play"}
EVIDENCE = {"attempt", "playback_status"}
TERMINAL = {"ready", "failed", "cancelled"}


class NetworkDevice:
    def __init__(self, runtime, state_dir, *, config=None, bridge=None, manual_input=None, clock=time.time):
        self.runtime, self.config, self.bridge = runtime, config or NetworkConfig(), bridge
        self.clock = clock
        self.service_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.operation_lock = threading.Lock()
        self.handoff_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.epoch, self.handoff_id, self.last_seq = 0, None, 0
        self.input = manual_input
        self.context = None
        self.observation = {}
        self.observed_at = 0
        self.claimed = None
        self.worker_busy = False
        self.last_error = None
        self.journal = NetworkJournal(Path(state_dir) / "network" / "state.json")
        self.active = next((o for o in self.journal.value["operations"].values()
                            if o["state"] not in TERMINAL), None)
        if self.active:
            self.active["recovering"] = True
        if self.config.enabled:
            self.runtime.hold_for_network()
            # Manual hold is authoritative even after either host component restarts.
            if self.runtime.manual_hold:
                self.journal.value.update(manual_hold=True, owner_id=self.runtime.owner_id)
                self.journal.save()
            if self.journal.value["manual_hold"]:
                self.handoff_id = uuid.uuid4().hex
            self.runtime.manual_hold, self.runtime.owner_id = False, None
            self.runtime._save_state()

    @property
    def manual(self):
        return self.journal.value["manual_hold"]

    def _receipt(self):
        busy = bool(self.active or (self.manual and self.worker_busy))
        mode = "transitioning" if busy else "manual" if self.manual else "automatic"
        if self.journal.value["input_uncertain"]:
            mode = "blocked"
        return {"network_context_id": self.context["id"] if self._context_ready() else None,
                "service_instance_id": self.service_id, "session_id": self.runtime.health()["session_id"],
                "epoch": self.epoch, "mode": mode, "handoff_id": self.handoff_id,
                "owner_id": self.journal.value["owner_id"], "last_seq": self.last_seq,
                "acknowledged": mode in {"manual", "automatic"}}

    def _validate(self, receipt, *, session=False):
        current = self._receipt()
        fields = ("service_instance_id", "epoch", "handoff_id") + (("session_id",) if session else ())
        if not isinstance(receipt, dict) or any(receipt.get(k) != current[k] for k in fields):
            raise StaleResultError("device ownership receipt is stale")

    def health(self):
        result = self.runtime.health()
        with self.lock:
            result["api_version"] = 12
            result["capabilities"] = list(dict.fromkeys(result["capabilities"] + NETWORK_APIS))
            result["network"] = self.vpn_status()
            if self.config.enabled:
                result["ownership"] = self._receipt()
                result["suspended"] = self.manual
                if not self._context_ready():
                    for capability in result.get("compatibility", {}).get("capabilities", {}).values():
                        capability.update(available=False, reasons=["network_context_pending"])
            return result

    def compatibility(self):
        return self.health()["compatibility"]

    def vpn_status(self, operation_id=None):
        with self.lock:
            if operation_id is not None:
                identifier(operation_id)
                op = self.journal.value["operations"].get(operation_id)
                return deepcopy(op) if op else {"operation_id": operation_id, "state": "not_found"}
            return {"enabled": self.config.enabled, "service_instance_id": self.service_id,
                    "state": "disabled" if not self.config.enabled else "ready" if self._context_ready()
                    else "transitioning" if self.active else "unknown",
                    "last_error": self.last_error,
                    "configuration_revision": self.config.revision,
                    "context_id": self.context["id"] if self._context_ready() else None,
                    "context": deepcopy(self.context), "observation": deepcopy(self.observation),
                    "observation_age": max(0, self.clock() - self.observed_at) if self.observed_at else None,
                    "operation": deepcopy(self.active)}

    def _context_ready(self):
        return bool(self.context and not self.active and self.clock() - self.observed_at <= self.config.ttl
                    and self.observation.get("verified") is True
                    and self.context["session_id"] == self.runtime.health()["session_id"])

    def _qualified(self, target):
        if target["id"] == "direct":
            return True
        known = self.journal.value["qualifications"].get(target["id"])
        return bool(known and known == self._qualification(target))

    def _qualification(self, target):
        return fingerprint([target, self.observation.get("bridge_version"), self.observation.get("vpn_version")])

    def vpn_locations(self):
        with self.lock:
            return {"enabled": self.config.enabled, "revision": self.config.revision,
                    "locations": [dict(p, qualified=self._qualified(p)) for p in self.config.profiles.values()]}

    def vpn_connect(self, target_id, *, operation_id, deadline_at, ownership, interrupt_playback=False,
                    qualify=False):
        if not self.config.enabled:
            raise NetworkError("network_disabled")
        identifier(operation_id)
        if type(interrupt_playback) is not bool or type(qualify) is not bool:
            raise ValueError("network authorization flags must be boolean")
        request = {"target_id": target_id, "deadline_at": deadline_at,
                   "interrupt_playback": interrupt_playback, "qualify": qualify,
                   "configuration_revision": self.config.revision}
        with self.lock:
            old = self.journal.value["operations"].get(operation_id)
            if old:
                if old["request"] != request:
                    raise NetworkError("network_operation_conflict")
                return deepcopy(old)  # Inspectable across replacement ownership/runtime sessions.
            self._validate(ownership)
            deadline(deadline_at, self.clock())
            if self.manual or self.journal.value["input_uncertain"]:
                raise BusyError("manual device ownership is active or uncertain")
            if self.active:
                raise BusyError("a network transition is already active")
            target = self.config.target(target_id)
            if target_id != "direct" and (not target["routing_confirmed"] or not (qualify or self._qualified(target))):
                raise NetworkError("network_profile_unqualified")
            noop = self._context_ready() and self.context["target_id"] == target_id
            if not noop and not interrupt_playback:
                raise NetworkError("network_interruption_authorization_required")
            recent = sum(not o.get("noop") and self.clock() - o["started_at"] < 900
                         for o in self.journal.value["operations"].values())
            if not noop and recent >= 8:
                raise NetworkError("network_transition_budget")
            op = {"operation_id": operation_id, "request": request, "target": target,
                  "deadline_at": float(deadline_at), "started_at": self.clock(), "state": "pending",
                  "phase": "retiring_prime", "dispatch": "not_sent", "cancel_requested": False,
                  "before_session_id": self.runtime.health()["session_id"], "error": None}
            op["previous_context"] = deepcopy(self.context)
            self.journal.value["operations"][operation_id] = op
            if noop:
                op.update(state="ready", phase="ready", finished_at=self.clock(), noop=True,
                          context_id=self.context["id"], session_id=self.context["session_id"])
            else:
                self.active = op
                self.context = None
                self.epoch += 1
                self.runtime.hold_for_network()
            self.journal.save()
            self.wake.set()
            return deepcopy(op)

    def vpn_disconnect(self, **params):
        return self.vpn_connect("direct", **params)

    def vpn_cancel(self, operation_id, *, ownership):
        with self.lock:
            self._validate(ownership)
            op = self.journal.value["operations"].get(identifier(operation_id))
            if not op:
                return {"operation_id": operation_id, "state": "not_found", "acknowledged": True}
            if op["state"] not in TERMINAL:
                op["cancel_requested"] = True
                self.epoch += 1
                self.runtime.hold_for_network()
                self.journal.save()
                self.wake.set()
            return dict(deepcopy(op), acknowledged=op["state"] in TERMINAL)

    def _save_phase(self, op, phase, **fields):
        with self.lock:
            op.update(phase=phase, updated_at=self.clock(), **fields)
            self.journal.save()

    def _finish(self, op, state, error=None):
        with self.lock:
            op.update(state=state, phase=state, error=error, finished_at=self.clock())
            self.active = None
            self.journal.save()

    def _observe(self):
        value = self.bridge.request("status")
        required = {"bridge_instance_id", "boot_id", "generation", "state", "profile_uuid", "verified",
                    "bridge_version", "vpn_version"}
        if not isinstance(value, dict) or not required <= value.keys():
            raise NetworkError("network_bridge_incompatible")
        # No raw provider messages or credentials cross into player diagnostics.
        result = {k: value.get(k) for k in (*required, "egress_ip", "operation")}
        with self.lock:
            self.observation, self.observed_at = result, self.clock()
            self.last_error = None
            if self.context and (self.context["identity"] != self._identity(result) or result.get("verified") is not True):
                self.context = None
                self.epoch += 1
                self.runtime.hold_for_network()
        claim = value["bridge_instance_id"]
        if claim != self.claimed:
            self.bridge.request("claim", owner=self.service_id)
            self.claimed = claim
        return result

    @staticmethod
    def _identity(value):
        return [value.get(k) for k in ("boot_id", "bridge_instance_id", "generation", "profile_uuid")]

    def _matches(self, value, target):
        return (value.get("verified") is True and value.get("state") in {"direct", "connected"}
                and value.get("profile_uuid") == target["profile_uuid"])

    def _fence_runtime(self):
        current = self.runtime.current
        if current is not None:
            h = current.health()
            current.cancel(session_id=h["session_id"], ownership=h["ownership"])

    def _cancelled(self, op):
        return self.stop_event.is_set() or self.manual or op["cancel_requested"] or self.clock() >= op["deadline_at"]

    def _settle(self, op):
        # A cancellation never disconnects or restores a previous connection.
        if op["dispatch"] != "not_sent":
            result = self.bridge.request("cancel", operation_id=op["operation_id"], owner=self.service_id)
            if result.get("settled") is not True:
                self._save_phase(op, "settling_network", error="network_outcome_unknown")
                return
        with self.operation_lock:
            # Any already-entered startup/cleanup must finish before manual keys.
            with self.runtime.lifecycle_lock:
                if self.runtime.retiring_backend:
                    self.runtime.retiring_backend.close()
                    self.runtime.retiring_backend = None
                if op.get("startup_started") and self.runtime.current:
                    self.runtime.retire_for_network()
        prior = op.get("previous_context")
        self.context = prior if (not op.get("retirement_started") and prior
                                 and prior["session_id"] == self.runtime.health()["session_id"]
                                 and prior["identity"] == self._identity(self.observation)) else None
        self._finish(op, "cancelled" if op["cancel_requested"] or self.manual else "failed",
                     "network_cancelled" if op["cancel_requested"] or self.manual else "network_deadline_expired")
        if self.context and not self.manual:
            self.runtime.release_network_hold()

    def _process(self, op):
        if op["request"]["configuration_revision"] != self.config.revision:
            op["cancel_requested"] = True
        if self._cancelled(op):
            self._settle(op)
            return
        value = self._observe()
        if op["phase"] == "retiring_prime":
            self._fence_runtime()
            with self.operation_lock:
                if self._cancelled(op):
                    return
                self._save_phase(op, "retiring_prime", retirement_started=True)
                self.runtime.retire_for_network()
            self._save_phase(op, "changing_network")
            return
        if op["phase"] in {"changing_network", "reconciling_network"}:
            if op["dispatch"] == "not_sent":
                if self._matches(value, op["target"]):
                    self._save_phase(op, "starting_prime")
                    return
                if op.get("adopt_only"):
                    raise NetworkError("network_context_unavailable")
                self._save_phase(op, "reconciling_network", dispatch="uncertain")
                self.bridge.request("transition", operation_id=op["operation_id"], owner=self.service_id,
                                    profile_uuid=op["target"]["profile_uuid"], deadline_at=op["deadline_at"])
                return
            result = self.bridge.request("operation", operation_id=op["operation_id"])
            if result.get("state") == "failed":
                self._save_phase(op, "reconciling_network", dispatch="settled")
                raise NetworkError(result.get("error", "network_connection_failed"))
            if result.get("state") == "ready" and self._matches(value, op["target"]):
                self._save_phase(op, "starting_prime", dispatch="settled")
            elif result.get("state") == "not_found" and self._matches(value, op["target"]):
                # Journal loss can be reconciled from actual state, never by replay.
                self._save_phase(op, "starting_prime", dispatch="settled")
            return
        if op["phase"] == "starting_prime":
            if not self._matches(value, op["target"]):
                raise NetworkError("network_context_changed")
            identity = self._identity(value)
            first = not op.get("startup_started")
            self._save_phase(op, "starting_prime", startup_started=True)
            with self.operation_lock:
                if self._cancelled(op):
                    return
                ready = self.runtime.start_for_network(first=first)
            if self._cancelled(op):
                return
            latest = self._observe()
            if not self._matches(latest, op["target"]) or self._identity(latest) != identity:
                raise NetworkError("network_context_changed")
            if not ready:
                return
            with self.lock:
                session = self.runtime.health()["session_id"]
                self.context = {"id": uuid.uuid4().hex, "target_id": op["target"]["id"],
                                "identity": identity, "session_id": session, "operation_id": op["operation_id"]}
                op.update(context_id=self.context["id"], session_id=session)
                if op["request"].get("qualify") and op["target"]["id"] != "direct":
                    self.journal.value["qualification_trial"] = {
                        "id": op["target"]["id"], "fingerprint": self._qualification(op["target"])}
                if op["target"]["id"] == "direct":
                    trial = self.journal.value.pop("qualification_trial", None)
                    if trial:
                        self.journal.value["qualifications"][trial["id"]] = trial["fingerprint"]
                self._finish(op, "ready")
                self.runtime.release_network_hold()

    def tick(self):
        if not self.config.enabled:
            return
        with self.lock:
            op = self.active
            self.worker_busy = True
        try:
            if op:
                self._process(op)
            elif not self.manual:
                value = self._observe()
                if self._context_ready():
                    return
                target = next((p["id"] for p in self.config.profiles.values()
                               if p["profile_uuid"] == value.get("profile_uuid")), None)
                if value.get("state") == "direct" and value.get("profile_uuid") is None:
                    target = "direct"
                if not target or value.get("verified") is not True:
                    self.runtime.hold_for_network()
                    return
                # Startup reconciles the observed connection; it never restores
                # a saved desired city or reconnects a VPN by itself.
                self.worker_busy = False
                result = self.vpn_connect(target, operation_id="adopt-" + uuid.uuid4().hex,
                                          deadline_at=self.clock() + 600, ownership=self._receipt(),
                                          interrupt_playback=True, qualify=True)
                with self.lock:
                    self.active["adopt_only"] = True
                    self.active["request"]["qualify"] = False
                    self.journal.save()
        except NetworkError as exc:
            self.last_error = str(exc)
            if op and op["dispatch"] in {"not_sent", "settled"}:
                self._finish(op, "failed", str(exc))
            elif op:
                self._save_phase(op, "reconciling_network", error=str(exc))
        except Exception:
            if op:
                self._save_phase(op, op["phase"], error="network_recovery_pending")
            # Missing bridge/uncertain observation grants no network capability.
            self.observed_at = 0
            self.last_error = "network_observation_unavailable"
        finally:
            with self.lock:
                self.worker_busy = False

    def start(self):
        if self.config.enabled:
            def run():
                while not self.stop_event.is_set():
                    self.tick()
                    self.wake.wait(2)
                    self.wake.clear()
            self.thread = threading.Thread(target=run, name="network-device", daemon=True)
            self.thread.start()
        return self

    def suspend(self, value, owner_id=None, previous=None):
        if not self.config.enabled:
            return self.runtime.suspend(value, owner_id=owner_id, previous=previous)
        if type(value) is not bool:
            raise ValueError("suspended must be boolean")
        with self.handoff_lock:
            with self.lock:
                self._validate(previous)
                if self.journal.value["input_uncertain"]:
                    raise OutcomeUnknownError("manual input outcome remains uncertain")
                self.epoch += 1
                self.journal.value.update(manual_hold=value, owner_id=(owner_id or "local-client") if value else None)
                self.handoff_id = uuid.uuid4().hex if value else None
                self.last_seq = 0
                if value and self.active:
                    self.active["cancel_requested"] = True
                self.journal.save()
                self.runtime.hold_for_network()
                self.wake.set()
            if value:
                self._fence_runtime()
                with self.operation_lock:
                    pass
            # The caller may poll/retry with the latest receipt until cleanup settles.
            with self.lock:
                result = self._receipt()
                result["recovery_pending"] = not result["acknowledged"]
                return result

    def manual_input(self, receipt, seq, *, key=None, text=None):
        if not self.config.enabled:
            return self.runtime.manual_input(receipt, seq, key=key, text=text)
        with self.operation_lock:
            with self.lock:
                self._validate(receipt)
                if self._receipt()["mode"] != "manual" or not self._receipt()["acknowledged"]:
                    raise BusyError("manual device ownership is not acknowledged")
                if type(seq) is not int or seq <= self.last_seq:
                    raise StaleResultError("manual input sequence consumed")
                self.last_seq = seq
            try:
                self.input(key=key, text=text)
            except Exception as exc:
                with self.lock:
                    self.journal.value["input_uncertain"] = True
                    self.journal.save()
                raise OutcomeUnknownError("manual input delivery unconfirmed") from exc
            return {"delivered": True, "seq": seq, "session_id": self._receipt()["session_id"]}

    def __getattr__(self, method):
        if method not in API_CAPABILITIES:
            raise AttributeError(method)
        def invoke(*args, **params):
            if not self.config.enabled:
                params.pop("network_context_id", None)
                return getattr(self.runtime, method)(*args, **params)
            if method == "cancel":
                # Attempt cancellation does not cancel an unrelated network operation.
                with self.lock:
                    self._validate(params.get("ownership"), session=True)
                    if self.active:
                        return dict(self._receipt(), cancelled=False, reason="network_transition_active")
                    h = self.runtime.health()
                    if params.get("session_id") != h["session_id"]:
                        raise StaleResultError("runtime session changed")
                result = self.runtime.cancel(session_id=h["session_id"], attempt_id=params.get("attempt_id"),
                                             ownership=h["ownership"])
                with self.operation_lock:
                    if result.get("cancelled"):
                        self.epoch += 1
                    return dict(self._receipt(), cancelled=result.get("cancelled", False),
                                reason=result.get("reason"), playback_stopped=False)
            with self.operation_lock:
                with self.lock:
                    receipt = params.get("ownership")
                    if receipt is not None:
                        self._validate(receipt, session=True)
                    if self.active or (self.manual and method in SCOPED):
                        raise BusyError("device ownership is transitioning or manual")
                    if method in SCOPED | EVIDENCE and not self._context_ready():
                        raise NetworkError("network_context_unavailable")
                    if method in SCOPED:
                        if not self._context_ready():
                            raise NetworkError("network_context_unavailable")
                        if params.pop("network_context_id", None) != self.context["id"]:
                            raise StaleResultError("network context changed or was not supplied")
                    epoch = self.epoch
                    context_id = self.context["id"] if self._context_ready() else None
                    h = self.runtime.health()
                    if receipt is not None:
                        params["ownership"] = h["ownership"]
                result = getattr(self.runtime, method)(*args, **params)
                with self.lock:
                    if method in SCOPED | EVIDENCE and (epoch != self.epoch or not self._context_ready()
                                              or context_id != self.context["id"]):
                        raise StaleResultError("network or ownership changed during the operation")
                    if isinstance(result, dict) and method in SCOPED | EVIDENCE:
                        result["network_context_id"] = context_id
                    return result
        return invoke

    def close(self):
        self.stop_event.set()
        self.runtime.hold_for_network()
        self.wake.set()
        if self.thread:
            self.thread.join(45)
        self.runtime.close()
        if self.thread and self.thread.is_alive():
            self.thread.join(45)
        if self.bridge:
            self.bridge.close()
