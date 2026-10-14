"""Persistent runtime adaptation, with a stable API across backend replacement.

Discovery never dispatches playback. The controller must obtain a new ownership
receipt and reevaluate its current intent after every backend session change.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .errors import BusyError, CompatibilityError, OutcomeUnknownError, StaleResultError, UnavailableError


API_CAPABILITIES = ["search", "resolve", "broadcasts", "inspect_page",
                    "resolution_evidence", "pages", "discover", "play", "attempt",
                    "playback_status", "suspend", "resume", "cancel", "stop", "manual_input"]


def _native_failure_code(exc, fallback):
    """Expose our bounded validator codes, never arbitrary exception text/URLs."""
    value = str(exc)
    return value if re.fullmatch(r"native_[a-z0-9_]{1,88}", value) else fallback


class RuntimeSupervisor:
    def __init__(self, base_profile, state_dir, backend_factory, *, clock=time.time,
                 store=None, build_profile=None, controller_factory=None, start_options=None,
                 serial=None):
        from .adapter_store import AdapterStore
        from .native_adapter import build_native_profile
        from .controller import Controller

        self.base = deepcopy(base_profile)
        self.path = Path(state_dir) / "adaptation"
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.store = store or AdapterStore(self.path / "native")
        self.store.fail_trial()  # A process exit cannot approve an unfinished trial.
        self.build_profile = build_profile or build_native_profile
        self.backend_factory = backend_factory
        self.serial = serial
        self.controller_factory = controller_factory or Controller
        self.start_options = dict(start_options or {})
        self.clock = clock
        self.lock = threading.RLock()
        self.factory_lock = threading.Lock()
        self.lifecycle_lock = threading.RLock()
        self.lifecycle_hold = threading.Event()
        self.suspend_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.current = None
        self.session_id = uuid.uuid4().hex
        self.recovery_epoch = 0
        self.recovery_handoff = None
        self.thread = None
        self.state = "observing"
        self.reason = "starting"
        self.revision = 0
        self.failures = 0
        self.next_retry = 0
        self.manual_hold = False
        self.owner_id = None
        self.started_at = self.clock()
        self.last_signature = None
        self.starting_backend = None
        self.retiring_backend = None
        self.restart_times = []
        self.healthy_since = None
        self.healthy_generation = None
        self.incompatible_signature = None
        self.acquisition_fingerprint = None
        self.failure_category = None
        self.trial_descriptor = None
        self.last_failure = None
        try:
            failure_path = self.path / "last-failure.json"
            if failure_path.stat().st_size <= 2 * 1024 * 1024:
                saved = json.loads(failure_path.read_text(encoding="utf-8"))
                if isinstance(saved.get("summary"), dict):
                    self.last_failure = saved["summary"]
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.binding_key = hashlib.sha256(json.dumps({
            "validator": 1, "engine": self.base["engine"],
            "modules": self.base["adapter"]["modules"],
            "contracts": self.base["adapter"].get("contracts"),
            "values": self.base["adapter"].get("valueContracts"),
        }, sort_keys=True).encode()).hexdigest()
        self._load_state()
        if self.manual_hold:
            self.recovery_handoff = uuid.uuid4().hex
        self._load_hints()

    def _safe_hints(self, roles):
        if not isinstance(roles, list) or len(roles) > 128:
            return []
        result = []
        for row in roles:
            if not isinstance(row, dict) or row.get("role") not in self.base["adapter"]["modules"]:
                continue
            bindings = []
            values = row.get("bindings")
            if not isinstance(values, list):
                continue
            for value in values[:32]:
                if (isinstance(value, dict) and re.fullmatch(r"[0-9]{1,12}", str(value.get("moduleId", "")))
                    and all(isinstance(value.get(k), str) and re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]{0,79}", value[k])
                            for k in ("export", "observedExport"))):
                    bindings.append({k: str(value[k]) for k in ("moduleId", "export", "observedExport")})
            if bindings:
                result.append({"role": row["role"], "bindings": bindings})
        return result

    def _load_hints(self):
        try:
            path = self.path / "role-hints.json"
            if path.stat().st_size > 65536:
                return
            value = json.loads(path.read_text())
            if value.get("key") == self.binding_key:
                digest = value.get("resource_sha256")
                if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest):
                    self.base["adapter"]["roleHints"] = self._safe_hints(value.get("roles"))
                    self.base["adapter"]["roleHintResourceSha256"] = digest
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _capture_hints(self, adaptation, compatibility):
        hints = self._safe_hints(adaptation.get("roles"))
        resources = [r for r in compatibility.get("resources", [])
                     if r.get("name") == self.base["appResource"]["name"]]
        if len(resources) != 1 or not re.fullmatch(r"[a-f0-9]{64}", str(resources[0].get("sha256", ""))):
            return
        digest = resources[0]["sha256"]
        if hints and (hints != self.base["adapter"].get("roleHints")
                      or digest != self.base["adapter"].get("roleHintResourceSha256")):
            self._atomic("role-hints.json", {"key": self.binding_key, "roles": hints,
                         "resource_sha256": digest,
                         "qualification": "candidate_locations_only; revalidate_initialized_contracts"})
            self.base["adapter"]["roleHints"] = hints
            self.base["adapter"]["roleHintResourceSha256"] = digest

    def _load_state(self):
        try:
            path = self.path / "recovery.json"
            if path.stat().st_size > 65536:
                return
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                return
            self.manual_hold = value.get("manual_hold") is True
            self.owner_id = str(value.get("owner_id") or "recovered-manual-owner")[:128]
            self.failures = max(0, min(12, int(value.get("failures", 0))))
            # Wall-clock changes cannot strand automation indefinitely.
            self.next_retry = min(self.clock() + 900, max(0, float(value.get("next_retry", 0))))
            if value.get("failure_category") in {"acquisition", "packaging", "loaded_image"}:
                self.failure_category = value["failure_category"]
                self.state = "backoff"
                self.reason = _native_failure_code(value.get("reason", ""), "native_recovery_pending")
            times = value.get("restart_times", [])
            self.restart_times = [float(t) for t in (times[-16:] if isinstance(times, list) else [])
                                  if type(t) in (int, float) and self.clock() - 900 < t <= self.clock()]
        except (OSError, ValueError, TypeError):
            pass

    def _save_state(self):
        value = {"schema": 1, "manual_hold": self.manual_hold,
                 "failure_category": self.failure_category, "reason": self.reason,
                 "owner_id": self.owner_id, "failures": self.failures,
                 "next_retry": self.next_retry, "restart_times": self.restart_times}
        self._atomic("recovery.json", value)

    def _atomic(self, name, value):
        temporary = self.path / (uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(value, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path / name)
            if os.name == "posix":
                descriptor = os.open(self.path, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def _phase(self, state, reason, category=None):
        with self.lock:
            if (self.state, self.reason, self.failure_category) != (state, reason, category):
                self.state, self.reason = state, reason
                self.failure_category = category
                self.revision += 1
                print(f"Prime runtime: {state} ({reason}).", flush=True)

    def _record_failure(self, backend, reason, exc=None):
        """Retain in-memory evidence before teardown; never issue device RPCs."""
        summary = {"time": self.clock(), "reason": reason,
                   "phase": self.state, "error_type": type(exc).__name__ if exc else None}
        self.last_failure = summary
        try:
            evidence = {"summary": summary}
            if backend is not None:
                evidence["snapshot"] = backend.diagnostic_snapshot()
                evidence["compatibility"] = backend.compatibility()
            # This private file may contain content titles and raw runtime errors.
            # Public health exposes only summary, never arbitrary exception text.
            encoded = json.dumps(evidence, allow_nan=False).encode("utf-8")
            if len(encoded) > 2 * 1024 * 1024:
                evidence["truncated"] = True
                snapshot = evidence.get("snapshot", {})
                # Repeated compatibility inventories can dominate a failed boot.
                # Keep the actual state and latest record of each kind instead
                # of dropping the entire snapshot when the file limit is hit.
                for field in ("launch_records", "runtime_records"):
                    latest = {}
                    for row in snapshot.get(field, []):
                        if isinstance(row, dict):
                            latest[row.get("kind", "unknown")] = row
                    kept = []
                    for row in list(latest.values())[-64:]:
                        if len(json.dumps(row, allow_nan=False).encode("utf-8")) <= 8192:
                            kept.append(row)
                        else:
                            kept.append({"kind": row.get("kind"), "timeMs": row.get("timeMs"),
                                         "omitted": "record_size_limit"})
                    snapshot[field] = kept
                snapshot["memory_samples"] = snapshot.get("memory_samples", [])[-2:]
            if len(json.dumps(evidence, allow_nan=False).encode("utf-8")) > 2 * 1024 * 1024:
                evidence = {"summary": summary, "truncated": True,
                            "compatibility": evidence.get("compatibility"),
                            "snapshot": {k: evidence.get("snapshot", {}).get(k)
                                         for k in ("session_id", "app_pid", "seen", "latest_state")}}
            if len(json.dumps(evidence, allow_nan=False).encode("utf-8")) > 2 * 1024 * 1024:
                evidence = {"summary": summary, "truncated": True}
            self._atomic("last-failure.json", evidence)
        except Exception:
            # A diagnostic failure must not replace the original runtime error.
            print("Prime failure evidence could not be saved.", flush=True)

    def adaptation(self):
        with self.lock:
            return {"state": self.state, "reason": self.reason,
                    "failure_category": self.failure_category,
                    "native_profile_state": "trial" if self.trial_descriptor else (
                        "loaded_verified" if self.state == "ready" else "observing"),
                    "revision": self.revision,
                    "retry_after_seconds": max(0, round(self.next_retry - self.clock(), 1)),
                    "attempt_count": self.failures, "manual_hold": self.manual_hold,
                    "last_failure": deepcopy(self.last_failure)}

    def health(self):
        with self.lock:
            controller = self.current
            if controller is None:
                health = {"session_id": self.session_id, "api_version": 11,
                          "serial": self.serial,
                          "capabilities": API_CAPABILITIES, "closed": False, "failure": None,
                          "state": {}, "ownership": {"session_id": self.session_id,
                          "epoch": self.recovery_epoch, "mode": "manual" if self.manual_hold else "blocked",
                          "handoff_id": self.recovery_handoff, "acknowledged": False, "owner_id": self.owner_id},
                          "compatibility": {"capabilities": {}}}
                if self.starting_backend is not None:
                    health["compatibility"] = self.starting_backend.compatibility()
                    # Contract observations are diagnostic until the new
                    # Controller and its ownership receipt are published.
                    health["compatibility"]["startup_capabilities"] = deepcopy(
                        health["compatibility"].get("capabilities", {})
                    )
                    for capability in health["compatibility"].get("capabilities", {}).values():
                        capability.update(available=False, reasons=["runtime_starting"])
            else:
                health = controller.health()
            health = deepcopy(health)
            if health.get("failure") or health.get("closed"):
                health["runtime_failure"] = "backend_unavailable"
                health["failure"], health["closed"] = None, False
                for value in health.get("compatibility", {}).get("capabilities", {}).values():
                    value.update(available=False, reasons=["backend_recovering"])
            health.setdefault("compatibility", {}).setdefault("adaptation", {}).update(self.adaptation())
            health["compatibility"]["refreshPolicy"] = "bounded automatic recovery; preserve usable loaded capabilities"
            health["compatibility"]["promotionPolicy"] = "revalidate initialized contracts; qualify results on each operation"
            health["supervised"] = True
            return health

    def compatibility(self):
        return self.health()["compatibility"]

    def suspend(self, value, owner_id=None, previous=None):
        with self.suspend_lock:
            return self._suspend(value, owner_id, previous)

    def _suspend(self, value, owner_id, previous):
        if type(value) is not bool:
            raise ValueError("suspended must be boolean")
        if value:
            owner_id = owner_id or "local-client"
        with self.lock:
            controller = self.current
            if controller is None:
                receipt = self.health()["ownership"]
                if previous is not None or not value:
                    if not isinstance(previous, dict) or any(previous.get(k) != receipt[k]
                            for k in ("session_id", "epoch", "handoff_id")):
                        raise StaleResultError("recovery handoff requires the current receipt")
                if value and self.manual_hold:
                    if owner_id == self.owner_id:
                        return dict(receipt, recovery_pending=True)
                    if previous is None:
                        raise BusyError("another manual owner holds this device")
                self.manual_hold, self.owner_id = value, owner_id if value else None
                self.recovery_epoch += 1
                self.recovery_handoff = uuid.uuid4().hex if value else None
                if value and self.starting_backend:
                    self.starting_backend.startup_cancelled.set()
                    self.starting_backend.interrupt_waits()
                self._save_state()
                self.wake_event.set()
                return dict(self.health()["ownership"], recovery_pending=True)
            # Manual intent fences recovery immediately, even while the device
            # acknowledgement is still in flight. A failed handoff stays held.
            prior_hold, prior_owner = self.manual_hold, self.owner_id
            prior_epoch = controller.ownership.epoch
            if value:
                with controller.ownership.lock:
                    if previous is not None:
                        controller.ownership.validate(previous)
                    if controller.ownership.input_uncertain:
                        raise OutcomeUnknownError("manual input delivery remains uncertain")
                    if (controller.ownership.mode == "manual"
                            and owner_id != controller.ownership.owner_id and previous is None):
                        raise BusyError("another manual owner holds this device")
                self.manual_hold, self.owner_id = True, owner_id
                self._save_state()
        try:
            result = controller.suspend(value, owner_id=owner_id, previous=previous)
        except (StaleResultError, BusyError):
            # Ownership rejects these before dispatching its device barrier.
            # A concurrent epoch change must not persist a rejected new owner.
            with self.lock:
                self.manual_hold, self.owner_id = prior_hold, prior_owner
                self._save_state()
            raise
        except OutcomeUnknownError:
            with self.lock, controller.ownership.lock:
                if controller.ownership.epoch == prior_epoch:
                    self.manual_hold, self.owner_id = prior_hold, prior_owner
                    self._save_state()
            raise
        if not value:
            with self.lock:
                self.manual_hold, self.owner_id = False, None
                self._save_state()
                self.wake_event.set()
        return result

    def __getattr__(self, method):
        if method not in API_CAPABILITIES:
            raise AttributeError(method)
        def invoke(*args, **kwargs):
            with self.lock:
                controller = self.current
                if controller is None:
                    raise UnavailableError("Prime runtime recovering; request was not dispatched")
            return getattr(controller, method)(*args, **kwargs)
        return invoke

    def _retire(self):
        with self.lock:
            controller = self.current
            if self.manual_hold or self.stop_event.is_set():
                return False
            if controller:
                ownership = controller.ownership
                if not ownership.transitions.acquire(blocking=False):
                    return False
                try:
                    if not ownership.input_lock.acquire(blocking=False):
                        return False
                    try:
                        with ownership.lock:
                            if ownership.mode == "manual":
                                return False
                            ownership.mode = "transitioning"
                            ownership.epoch += 1
                            if ownership.lease:
                                ownership.lease.cancelled.set()
                            controller.suspended = True
                            self.current = None
                            self.retiring_backend = controller.backend
                            self.session_id = uuid.uuid4().hex
                    finally:
                        ownership.input_lock.release()
                finally:
                    ownership.transitions.release()
        if controller:
            self._fail_trial("native_trial_retired")
            controller.backend.interrupt_waits()
            controller.backend.close()
            self.retiring_backend = None
        return True

    def _backoff(self, reason, category=None):
        with self.lock:
            self.failures = min(12, self.failures + 1)
            self.healthy_since = None
            self.next_retry = self.clock() + min(900, 15 * (2 ** (self.failures - 1)))
            self._phase("backoff", reason, category)
            self._save_state()

    def _launch(self, profile, *, planned=False):
        if self.stop_event.is_set() or self.manual_hold:
            self._fail_trial("native_trial_cancelled")
            return
        now = self.clock()
        self.restart_times = [t for t in self.restart_times if now - 900 < t <= now]
        if not planned and len(self.restart_times) >= 3:
            self._fail_trial("native_trial_restart_budget")
            self.next_retry = max(self.next_retry, min(self.restart_times) + 900)
            self._save_state()
            self._phase("backoff", "restart_budget_exhausted")
            return
        if not planned:
            self.restart_times.append(now)
        self._save_state()
        self._phase("validating", "starting_runtime")
        with self.factory_lock:
            if self.stop_event.is_set() or self.manual_hold:
                return
            backend = self.backend_factory(profile)
            with self.lock:
                self.starting_backend = backend
                if self.stop_event.is_set() or self.manual_hold:
                    backend.startup_cancelled.set()
        try:
            backend.start(restart_prime=True, exclusive_server=True, wake_on_start=True,
                          **self.start_options)
            controller = self.controller_factory(backend)
            with self.lock:
                if self.stop_event.is_set():
                    backend.close()
                    return
                if self.manual_hold:
                    # Fresh process: no automatic operation was submitted.
                    controller.ownership.mode = "manual"
                    controller.ownership.owner_id = self.owner_id
                    controller.ownership.handoff_id = uuid.uuid4().hex
                    controller.suspended = True
                self.current = controller
                self.next_retry = 0
                self.session_id = backend.session_id
                self.started_at = self.clock()
                self.last_signature = None
                self.healthy_since = None
                self.incompatible_signature = None
        except BaseException as exc:
            self._fail_trial("native_trial_startup_failed")
            if not self.stop_event.is_set() and not self.manual_hold:
                self._record_failure(backend, "startup_failed", exc)
            self.retiring_backend = backend
            backend.close()
            self.retiring_backend = None
            raise
        finally:
            with self.lock:
                self.starting_backend = None

    @staticmethod
    def _has_retained_playback(backend, health, caps):
        """Use existing bounded observations; a playback page alone is insufficient."""
        page = health.get("state", {}).get("page", {})
        if (page.get("pageType") != "RUST_PLAYBACK" or not page.get("pageId")
                or not all(caps.get(name, {}).get("available") is True
                           for name in ("core", "play", "native_inspection"))):
            return False
        with backend.cv:
            # The journal is bounded by the backend. Existing status checkpoints
            # carry the app's retained event even when its original row expired.
            for row in reversed(backend.rows):
                data = row.get("data", {})
                if row.get("kind") == "control_action_finished" and data.get("op") == "playback-status":
                    checkpoint = data.get("result", {})
                    event = checkpoint.get("event") or {}
                    return (checkpoint.get("page", {}).get("pageId") == page["pageId"]
                            and event.get("pageId") == page["pageId"]
                            and type(checkpoint.get("epoch")) is int
                            and event.get("epoch") == checkpoint["epoch"]
                            and event.get("name") == "PLAYBACK_STATE_CHANGE"
                            and event.get("rawState") in ("Playing", "Buffering"))
                if row.get("kind") != "control_app_event":
                    continue
                name = data.get("name", "")
                if (name in ("NAVIGATE_TO_PAGE", "PLAYBACK_LOAD", "PLAYBACK_CONTENT_SWITCH",
                             "PLAYBACK_UNLOAD", "PLAYBACK_EXPECTED_FATAL")
                        or name.startswith("PLAYBACK_FATAL_")):
                    return False
                if name == "PLAYBACK_STATE_CHANGE":
                    observed_page = data.get("state", {}).get("page", {})
                    return (observed_page.get("pageType") == "RUST_PLAYBACK"
                            and observed_page.get("pageId") == page["pageId"]
                            and type(data.get("playbackEpoch")) is int
                            and data.get("params", {}).get("value", {}).get("state")
                            in ("Playing", "Buffering"))
        return False

    def step(self):
        """One bounded supervisor pass; only the worker calls this in production."""
        if self.lifecycle_hold.is_set():
            return
        with self.lifecycle_lock:
            if not self.lifecycle_hold.is_set():
                self._step()

    def hold_for_network(self):
        """Fence future recovery starts; the caller must subsequently drain lifecycle."""
        self.lifecycle_hold.set()
        with self.lock:
            if self.starting_backend:
                self.starting_backend.startup_cancelled.set()
                self.starting_backend.interrupt_waits()

    def retire_for_network(self):
        """One lifecycle owner retires Prime before any network mutation."""
        with self.lifecycle_lock:
            if self.retiring_backend:
                self.retiring_backend.close()
                self.retiring_backend = None
            if not self._retire():
                raise BusyError("Prime retirement is blocked by ownership")

    def start_for_network(self, *, first=False):
        with self.lifecycle_lock:
            if first and self.current is None:
                self._launch(self.store.load_active(self.base) or self.base, planned=True)
            else:
                self._step()
            return self.state == "ready" and self.current is not None

    def release_network_hold(self):
        self.lifecycle_hold.clear()
        self.wake_event.set()

    def _step(self):
        if self.stop_event.is_set() or self.manual_hold:
            return
        if self.current is None and self.clock() < self.next_retry:
            return
        if self.retiring_backend:
            # Failed teardown is retried before a new private Frida server or
            # backend can be created. No uncertain mutation is replayed.
            self.retiring_backend.close()
            self.retiring_backend = None
        controller = self.current
        if controller is None:
            self._launch(self.store.load_active(self.base) or self.base)
            return
        backend = controller.backend
        health = backend.health()
        if health.get("failure") or health.get("closed"):
            self._fail_trial("native_trial_disconnected")
            self._record_failure(backend, "runtime_disconnected")
            if self._retire():
                self._backoff("runtime_disconnected")
            return
        compatibility = health.get("compatibility", {})
        caps = compatibility.get("capabilities", {})
        adaptation = compatibility.get("adaptation", {})
        signature = json.dumps([compatibility.get("generation"), backend.engine_unavailable,
                                {name: value.get("reasons", []) for name, value in caps.items()}],
                               sort_keys=True)
        def incompatible(reason, category="api_contract"):
            self.incompatible_signature = signature
            self._phase("contract_incompatible", reason, category)
        essential = ("core", "search", "broadcasts", "play", "native_inspection")
        if all(caps.get(name, {}).get("available") is True for name in essential):
            if self.trial_descriptor:
                if self.clock() < self.next_retry:
                    return
                # Image verification is supplied by the live backend, never by
                # successful download or a previous process's cache.
                latest = backend.adaptation_manifest()
                from .native_adapter import descriptor_identity
                if (not latest or descriptor_identity(latest["descriptor"])
                        != descriptor_identity(self.trial_descriptor)):
                    self._fail_trial("native_trial_manifest_changed")
                    return
                self.store.activate(self.base, self.trial_descriptor, backend.profile)
                self.trial_descriptor = None
            self._capture_hints(adaptation, compatibility)
            self._phase("ready", "contracts_available")
            self.incompatible_signature = None
            if self.healthy_since is None or self.healthy_generation != compatibility.get("generation"):
                self.healthy_since = self.clock()
                self.healthy_generation = compatibility.get("generation")
            if self.clock() - self.healthy_since >= 300 and (self.failures or self.next_retry):
                self.failures, self.next_retry = 0, 0
                self._save_state()
            return
        if self.incompatible_signature == signature:
            return
        if backend.engine_unavailable:
            self._fail_trial("native_trial_engine_incompatible")
            incompatible("engine_contract_unsupported", "engine_contract")
            return
        if any(value.get("qualification") == "contract_failed" or any(
                str(reason).startswith("response_schema_failed:") for reason in value.get("reasons", []))
                for value in caps.values()):
            self._fail_trial("native_trial_response_incompatible")
            incompatible("response_contract_unsupported")
            return
        self.healthy_since = None
        # URLs are requested only when capability recovery needs them and never
        # included in our status, logs, durable supervisor state or exceptions.
        manifest = backend.adaptation_manifest()
        descriptor = manifest.get("descriptor") if isinstance(manifest, dict) else None
        # URLs are private acquisition evidence, not runtime identity. A rotated
        # URL must reopen acquisition without invalidating application commands.
        fingerprint = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
        changed = self.acquisition_fingerprint is not None and fingerprint != self.acquisition_fingerprint
        if self.clock() < self.next_retry and not (
                changed and self.failure_category in {"acquisition", "packaging"}):
            return
        self.acquisition_fingerprint = fingerprint
        self._phase("discovering", "runtime_contracts_pending")
        if descriptor and manifest.get("engineBuildId") == self.base["engine"]["buildId"]:
            from .native_adapter import ContractError, NativeAdapterError, descriptor_identity, failure_category
            try:
                identity = descriptor_identity(descriptor)
            except ContractError as exc:
                reason = _native_failure_code(exc, "native_descriptor_unsupported")
                self._record_failure(backend, reason, exc)
                if failure_category(reason) == "engine_contract":
                    incompatible(reason, "engine_contract")
                else:
                    self._native_backoff(reason, "packaging")
                return
            active = backend.profile or self.base
            if identity["sha256"] != active["testRuntime"]["compressedSha256"]:
                remaining = self.store.retry_after(self.base, descriptor, evidence=fingerprint)
                if remaining:
                    self.next_retry = self.clock() + remaining
                    self._phase("backoff", "native_candidate_backoff", "acquisition")
                    return
                self._phase("validating", "native_resource_identity")
                try:
                    candidate = self.store.load(self.base, descriptor)
                    if candidate is None:
                        candidate = self.build_profile(self.base, descriptor)
                        self.store.save(self.base, descriptor, candidate)
                    # Recheck after download: an intervening Prime update cannot
                    # promote a descriptor captured from an older manifest.
                    latest = backend.adaptation_manifest()
                    if not latest or descriptor_identity(latest["descriptor"]) != identity:
                        self._phase("discovering", "manifest_changed_during_validation")
                        return
                    if self._retire():
                        self.store.begin_trial(self.base, descriptor, candidate)
                        self.trial_descriptor = descriptor
                        self._launch(candidate)
                    return
                except ContractError as exc:
                    reason = _native_failure_code(exc, "native_contract_unsupported")
                    self.store.mark_failure(self.base, descriptor, reason, evidence=fingerprint)
                    self._record_failure(backend, reason, exc)
                    if failure_category(reason) == "engine_contract":
                        incompatible(reason, "engine_contract")
                    else:
                        self._native_backoff(reason, "packaging")
                    return
                except NativeAdapterError as exc:
                    reason = _native_failure_code(exc, "native_acquisition_failed")
                    self.store.mark_failure(self.base, descriptor, reason, evidence=fingerprint)
                    self._record_failure(backend, reason, exc)
                    self._native_backoff(reason, "acquisition")
                    return
        if adaptation.get("restartRequired"):
            if self._retire():
                self._backoff("loaded_bindings_changed")
            return
        if self.clock() - self.started_at > 120:
            reasons = [str(reason) for value in caps.values() for reason in value.get("reasons", [])]
            if any(reason in {"binary_fingerprint_mismatch", "native_image_unloaded", "binary_not_observed"}
                   for reason in reasons) and not self._has_retained_playback(backend, health, caps):
                self._fail_trial("native_trial_image_unverified")
                self._record_failure(backend, "native_image_unverified")
                if self._retire():
                    self._native_backoff("native_image_unverified", "loaded_image")
                return
            if any(reason.startswith(("module_incompatible:", "module_ambiguous:",
                                      "export_contract_failed:")) for reason in reasons):
                incompatible("runtime_export_contract_unsupported")
                return
            # A lazy module or incomplete bootstrap is not an API contract change.
            # Preserve usable playback while waiting for optional initialization.
            if self._has_retained_playback(backend, health, caps):
                self._phase("observing", "partial_runtime_playback_retained")
            else:
                self._fail_trial("native_trial_initialization_incomplete")
                self._record_failure(backend, "runtime_initialization_incomplete")
                if self._retire():
                    self._backoff("runtime_initialization_incomplete")

    def _native_backoff(self, reason, category):
        self._backoff(reason, category)

    def _fail_trial(self, reason):
        if self.trial_descriptor is not None:
            self.store.fail_trial(reason)
            self.trial_descriptor = None

    def _run(self):
        from .native_adapter import NativeAdapterError, ContractError, failure_category
        while not self.stop_event.is_set():
            try:
                self.step()
            except NativeAdapterError as exc:
                if self.stop_event.is_set() or self.manual_hold:
                    continue
                reason = _native_failure_code(exc, "native_recovery_failed")
                category = failure_category(reason) if isinstance(exc, ContractError) else "acquisition"
                backend = self.current.backend if self.current else None
                self._record_failure(backend, reason, exc)
                if category == "engine_contract":
                    self.next_retry = self.clock() + 900
                    self._phase("contract_incompatible", reason, category)
                    self._save_state()
                else:
                    self._native_backoff(reason, category)
            except CompatibilityError:
                if self.stop_event.is_set() or self.manual_hold:
                    continue
                self.next_retry = self.clock() + 900
                self._save_state()
                self._phase("contract_incompatible", "startup_contract_unsupported")
            except Exception as exc:
                if self.stop_event.is_set() or self.manual_hold:
                    continue
                # Detailed resource transport exceptions may contain signed URLs.
                print(f"Prime recovery failed: {type(exc).__name__}; retrying with backoff.", flush=True)
                self._backoff("runtime_recovery_failed")
            self.wake_event.wait(5)
            self.wake_event.clear()

    def start(self):
        self.thread = threading.Thread(target=self._run, name="prime-recovery", daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        # The private-server factory must finish publishing its backend before
        # the CLI can tear down that server. Cancellation fences later launch.
        with self.factory_lock:
            with self.lock:
                starting = self.starting_backend
        if starting:
            # Startup checks this event before launch and while waiting for its
            # gated process. The worker owns cleanup, avoiding detach races.
            starting.startup_cancelled.set()
            starting.interrupt_waits()
            starting.close()
        if self.thread:
            self.thread.join(timeout=45)
        with self.lock:
            controller, self.current = self.current, None
        if controller:
            controller.backend.close()
        if self.retiring_backend:
            self.retiring_backend.close()
