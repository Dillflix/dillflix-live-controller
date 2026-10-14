"""Exclusive, persistent Frida session using the validated ARM32 transport."""

from contextlib import contextmanager
import subprocess
import re
import shlex
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid
from collections import deque
from .errors import (
    PrimeError,
    CancelledError,
    PlaybackRefusedError,
    CompatibilityError,
    EvidenceLostError,
    OutcomeUnknownError,
    UnavailableError,
)
from .transport import ACTIVITY, PACKAGE, AdbGate, force_stop, rpc

RUNTIME = Path(__file__).parent / "_runtime"


def payload_source(profile=None):
    options = dict(
        mode="test-utils",
        profile=profile or json.loads((RUNTIME / "profile.json").read_text()),
        intervalMs=1000,
        timeoutMs=15000,
        settleMs=10000,
        maxRecordChars=524288,
        scene=True,
        player=False,
        loadedItems=True,
        control=True,
        capabilities=True,
        lightweight=True,
        ownership=True,
        metadataDiscovery=True,
        sceneChunks=True,
    )

    def expr(name):
        return (RUNTIME / name).read_text().strip().rstrip(";")

    capability = expr("capability-probe.js").replace(
        "__PV_RENDERER_FACTORY__", expr("renderer-mirror.js")
    )
    control = (
        expr("control-probe.js")
        .replace("__PV_CAPABILITY_FACTORY__", capability)
        .replace("__PV_SEARCH_FACTORY__", expr("search-evidence.js"))
        .replace("__PV_CATALOG_FACTORY__", expr("catalog-search.js"))
        .replace("__PV_BROADCASTS_FACTORY__", expr("broadcasts.js"))
    )
    probe = expr("app-probe.js").replace("__PV_CONTROL_FACTORY__", control)
    encoded = json.dumps(options, ensure_ascii=True, separators=(",", ":"))
    payload = dict(
        options=options,
        bootstrap=expr("bootstrap.js"),
        probe=probe,
        modules=expr("module-observer.js"),
        roles=expr("role-registry.js"),
        runtimes=expr("runtime-observer.js"),
    )
    for name in ("bootstrap", "probe", "modules", "roles", "runtimes"):
        payload[name] = payload[name].replace("__PV_OPTIONS__", encoded)
        if not payload[name].isascii():
            raise ValueError("runtime source must be ASCII")
    gate = expr("stable-gate.js").replace(
        "__PV_NATIVE_GATE_SOURCE__", json.dumps((RUNTIME / "native-gate.c").read_text())
    )
    return (
        expr("agent.js")
        .replace("__PV_STABLE_GATE_FACTORY__", gate)
        .replace("__PV_LIGHT_TRANSPORT_FACTORY__", expr("light-transport.js"))
        .replace("__PV_PAYLOAD__", json.dumps(payload, ensure_ascii=True))
    )


class FridaBackend:
    def __init__(self, serial, endpoint, state_dir, *, profile=None):
        from .scene_transfer import SceneTransfer

        self.scene_transfer = SceneTransfer()
        self.local = threading.local()
        self.ownership_epoch = 0
        self.serial = serial
        self.endpoint = endpoint
        self.state_dir = Path(state_dir)
        self.profile = profile
        self.session_id = uuid.uuid4().hex
        self.adb = ["adb", "-s", serial]
        self.cv = threading.Condition()
        self.rpc_lock = threading.Lock()
        self.rows = deque(maxlen=8192)
        self.launch_records = deque(maxlen=256)
        self.memory_samples = deque(maxlen=480)  # Four hours at one sample per 30s.
        self.app_pid = None
        self.pending = {}
        self.seen = set()
        self.failure = None
        self.stopping = threading.Event()
        self.startup_cancelled = threading.Event()
        self.lifecycle_lock = threading.RLock()
        self.cleanup_complete = False
        self.device_manager = None
        self.remote_registration_pending = False
        self.remote_missing_error = ()
        self.script = None
        self.session = None
        self.gate = None
        self.thread = None
        self.owner = None
        self.launched = False
        self.initial = {}
        self.latest_state = {}
        self.disabled_native_methods = set()
        self.native_retry_after = {}
        self.native_inspection_reported = False
        self.last_observation = 0
        self.compatibility_contexts = {}
        self.control_context = None
        self.engine_evidence = {}
        self.engine_unavailable = None

    def record_launch(self, kind, **data):
        with self.cv:
            self.launch_records.append(
                {"timeMs": int(time.time() * 1000), "kind": kind, "data": data}
            )

    def diagnostic_snapshot(self):
        """Read retained evidence without requiring a healthy runtime or any RPC."""
        with self.cv:
            return {
                "session_id": self.session_id,
                "app_pid": self.app_pid,
                "failure": self.failure,
                "closed": self.stopping.is_set(),
                "seen": sorted(self.seen),
                "initial": self.initial,
                "latest_state": self.latest_state,
                "launch_records": list(self.launch_records),
                "runtime_records": list(self.rows),
                "memory_samples": list(self.memory_samples),
            }

    def _message(self, message, data):
        with self.cv:
            if message.get("type") == "error":
                self.record_launch("agent_error", message=message)
                self.failure = "agent script error: " + message.get("description", "")
            if message.get("type") == "send":
                body = message.get("payload", {})
                kind = body.get("kind")
                value = body.get("data", {})
                self.seen.add(kind)
                if kind not in ("observations", "runtime_memory"):
                    self.record_launch(kind, value=value)
                if kind == "runtime_memory":
                    self.memory_samples.append(value)
                if kind == "engine_unavailable":
                    self.engine_evidence = {}
                    self.engine_unavailable = value.get("reason")
                if kind == "engine_verified":
                    self.engine_evidence = value
                if kind == "compatibility":
                    self.compatibility_contexts[value["context"]] = value["snapshot"]
                if kind == "app_vm_found" and self.control_context is None:
                    self.control_context = value.get("context")
                if kind == "agent_ready":
                    self.initial = value
                if kind in ("fatal", "config_rejected"):
                    self.failure = kind + ": " + str(value.get("message", value))
                if kind == "control_result" and value.get("id") in self.pending:
                    self.pending[value["id"]] = value
                if kind == "observations":
                    batch = value.get("batch", {})
                    if "disabledMethods" in batch:
                        self.native_inspection_reported = True
                        self.disabled_native_methods = set(batch["disabledMethods"])
                        self.native_retry_after = dict(batch.get("retryAfterMs", {}))
                    if batch.get("criticalDropped"):
                        self.failure = "critical runtime evidence was dropped"
                    decoded = self.scene_transfer.feed(batch.get("records", []))
                    if self.scene_transfer.errors:
                        self.failure = (
                            "native scene transfer failed: "
                            + self.scene_transfer.errors[-1]["reason"]
                        )
                    for row in decoded:
                        self.rows.append(row)
                        if row.get("kind") == "control_state":
                            self.latest_state = row["data"]
                    self.last_observation = time.monotonic()
            self.cv.notify_all()

    @contextmanager
    def operation_scope(self, lease):
        self.local.lease = lease
        try:
            yield
        finally:
            self.local.lease = None

    def interrupt_waits(self):
        with self.cv:
            self.cv.notify_all()

    def fence(self, epoch):
        # Out-of-band from scheduled commands and their receipt waiters.
        result = self._rpc(self.script.exports_sync.fence, epoch, timeout=15)
        if result.get("acknowledged"):
            self.ownership_epoch = epoch
        return result

    def manual_input(self, *, key=None, text=None):
        from .device_input import manual_input
        return manual_input(self.adb, key=key, text=text)

    def _check(self):
        lease = getattr(self.local, "lease", None)
        if lease:
            lease.check()
        if self.startup_cancelled.is_set():
            raise CancelledError("runtime startup or session was cancelled")
        if self.failure:
            raise UnavailableError(self.failure)
        if self.stopping.is_set():
            raise UnavailableError("service session is closed")

    def _rpc(self, call, *args, timeout=5, **kwargs):
        with self.rpc_lock:
            return rpc(call, *args, timeout=timeout, **kwargs)

    def _detached(self, reason, crash=None):
        if not self.stopping.is_set():
            with self.cv:
                self.record_launch(
                    "detached", reason=reason, crash=str(crash) if crash else None
                )
                self.failure = "Prime detached: " + reason
                self.cv.notify_all()

    def _pump(self):
        failures = 0
        while not self.stopping.wait(2):
            try:
                self._rpc(self.script.exports_sync.poll, timeout=3)
                failures = 0
            except Exception:
                failures += 1
                if failures >= 3:
                    with self.cv:
                        self.failure = "three consecutive runtime poll failures; session recovery required"
                        self.cv.notify_all()
                    return

    def _connect(self, frida, timeout):
        """One bounded read-only handshake before any app lifecycle changes."""
        self.startup_phase = "frida_handshake"
        self.connection_timeout = timeout
        started = time.monotonic()
        stopped = threading.Event()
        print(
            f"Frida handshake: {self.endpoint}; budget {timeout:g} seconds.", flush=True
        )

        def progress():
            while not stopped.wait(5):
                print(
                    f"Frida handshake pending: {time.monotonic() - started:.0f} / {timeout:g} seconds.",
                    flush=True,
                )

        reporter = threading.Thread(target=progress, daemon=True)
        reporter.start()
        try:
            # The manager is a process singleton. Register before the call so an
            # uncertain add outcome is still cleaned up; serialize with close so
            # shutdown cannot remove the address before add finishes publishing it.
            with self.lifecycle_lock:
                self._check()
                self.device_manager = frida.get_device_manager()
                self.remote_missing_error = getattr(frida, "InvalidArgumentError", ())
                self.remote_registration_pending = True
                device = self._rpc(
                    self.device_manager.add_remote_device,
                    self.endpoint,
                    timeout=timeout,
                )
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError(
                    "remote-device connection exhausted handshake budget"
                )
            while True:
                self._check()
                try:
                    params = self._rpc(device.query_system_parameters, timeout=remaining)
                    break
                except frida.TransportError as exc:
                    # Frida can time out this read before our outer deadline.
                    # Retry only this idempotent query, never app lifecycle work.
                    remaining = timeout - (time.monotonic() - started)
                    if "timeout was reached" not in str(exc).lower() or remaining <= 0:
                        raise
                    print(
                        "Frida system-parameter query timed out; retrying within "
                        f"the remaining {remaining:.0f}-second handshake budget.",
                        flush=True,
                    )
                    if stopped.wait(min(1, remaining)):
                        raise
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise
            self._check()
            print("Frida handshake complete.", flush=True)
            return device, params
        except CancelledError:
            raise
        except Exception as exc:
            raise UnavailableError(
                f"Frida handshake failed after {time.monotonic() - started:.1f}s "
                f"(budget {timeout:g}s): {type(exc).__name__}: {exc}. "
                "Prime has not been restarted or instrumented by this startup."
            ) from exc
        finally:
            stopped.set()
            reporter.join(timeout=1)

    def start(
        self, *, restart_prime=False, exclusive_server=False, connection_timeout=120,
        wake_on_start=False
    ):
        if not restart_prime or not exclusive_server:
            raise ValueError(
                "startup requires explicit restart_prime and exclusive_server acknowledgement"
            )
        import frida

        if (
            type(connection_timeout) not in (int, float)
            or not 5 <= connection_timeout <= 120
        ):
            raise ValueError("connection_timeout must be 5..120 seconds")

        if frida.__version__ != "17.2.17":
            raise CompatibilityError("install frida==17.2.17; server must match")
        with self.lifecycle_lock:
            self._check()
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            key = hashlib.sha256(self.serial.encode()).hexdigest()[:24]
            self.owner = open(self.state_dir / (key + ".lock"), "a+")
            try:
                fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.owner.close()
                self.owner = None
                raise UnavailableError("device already owned by this host user")
        try:
            device, params = self._connect(frida, connection_timeout)
            if params.get("arch") != "arm":
                raise CompatibilityError(
                    "only the validated ARM32 adapter is supported"
                )
            self._check()
            self.startup_phase = "prime_launch"
            if wake_on_start:
                with self.lifecycle_lock:
                    self._check()
                    self.wake_for_playback()
            with self.lifecycle_lock:
                self._check()
                force_stop(self.adb)
                self.launched = True
            with self.lifecycle_lock:
                self._check()
                self.gate = AdbGate(
                    device, self.adb, self.record_launch, 90,
                    lambda: bool(self.failure) or self.stopping.is_set()
                    or self.startup_cancelled.is_set()
                )
                pid = self.gate.start()
                self.app_pid = pid
            with self.lifecycle_lock:
                self._check()
                self.session = self._rpc(device.attach, pid)
                self.session.on("detached", self._detached)
            with self.lifecycle_lock:
                self._check()
                self.script = self._rpc(
                    self.session.create_script,
                    payload_source(self.profile) if self.profile else payload_source(),
                    timeout=10, runtime="qjs"
                )
                self.script.on("message", self._message)
            with self.lifecycle_lock:
                self._check()
                self._rpc(self.script.load)
            self.wait(lambda: "agent_ready" in self.seen, 5)
            if self.initial.get("enginePresentAtInjection") is not False:
                raise CompatibilityError("early bootstrap ordering unproven")
            with self.lifecycle_lock:
                self._check()
                self.gate.resume()
                self.gate.close()
                self.gate = None
            with self.lifecycle_lock:
                self._check()
                self.thread = threading.Thread(
                    target=self._pump, name="prime-poll", daemon=True
                )
                self.thread.start()
            self.startup_phase = "runtime_readiness"
            self.wait(
                lambda: (
                    "compatibility_degraded" in self.seen
                    or (
                        all(
                            k in self.seen
                            for k in (
                                "engine_verified",
                                "lightweight_handoff",
                                "app_vm_found",
                            )
                        )
                        and self.latest_state.get("ready")
                    )
                ),
                100,
            )
            self.startup_phase = "ready"
            return self
        except BaseException:
            self.close()
            raise

    def wait(self, predicate, timeout):
        end = time.monotonic() + timeout
        with self.cv:
            while True:
                self._check()
                value = predicate()
                if value:
                    return value
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("runtime observation deadline exceeded")
                self.cv.wait(min(0.5, remaining))

    @property
    def cursor(self):
        with self.cv:
            return self.rows[-1]["sequence"] if self.rows else 0

    def records(self, after=0):
        with self.cv:
            self._check()
            if after and self.rows and after < self.rows[0]["sequence"] - 1:
                raise EvidenceLostError("observation cursor expired")
            return [r for r in self.rows if r.get("sequence", 0) > after]

    def wake_for_playback(self):
        """Wake only for an authorized launch; never submit playback if unconfirmed."""
        self._check()
        try:
            self.manual_input(key="wake")
            for _ in range(3):
                self._check()
                result = subprocess.run(
                    self.adb + ["shell", "dumpsys", "power"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    check=True, timeout=3,
                )
                if any(line.strip() == "mWakefulness=Awake" for line in result.stdout.splitlines()):
                    self._check()
                    self.record_launch("playback_wake", awake=True)
                    return
                time.sleep(0.25)
            raise RuntimeError("Android did not report Awake after wake input")
        except CancelledError:
            raise
        except Exception as exc:
            self.record_launch("playback_wake", awake=False, error=str(exc))
            raise PlaybackRefusedError(
                "device wake unconfirmed; playback command not submitted: " + str(exc)
            ) from exc

    def foreground_for_playback(self):
        """Resume the instrumented Prime process, never silently replace it."""
        def adb(*args, timeout=5):
            self._check()
            return subprocess.run(
                self.adb + ["shell", *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=True, timeout=timeout,
            ).stdout

        def verify_pid():
            if str(self.app_pid) not in adb("pidof", PACKAGE).split():
                raise RuntimeError("instrumented Prime process is absent; restart player session")

        def foreground():
            activity = adb("dumpsys", "activity", "activities")
            return any(
                re.search(r"(?:mResumedActivity|topResumedActivity|ResumedActivity):", line)
                and re.search(r"\b" + re.escape(PACKAGE) + r"/", line)
                for line in activity.splitlines()
            )

        try:
            verify_pid()
            if not foreground():
                # REORDER_TO_FRONT reuses the existing activity. No force-stop.
                adb("am", "start", "-W", "-f", "0x00020000", "-n",
                    PACKAGE + "/" + ACTIVITY, timeout=10)
                verify_pid()
                if not foreground():
                    raise RuntimeError("Prime activity did not become resumed")
            self._check()
            self.record_launch("playback_foreground", confirmed=True, pid=self.app_pid)
        except CancelledError:
            raise
        except Exception as exc:
            self.record_launch("playback_foreground", confirmed=False, error=str(exc))
            raise PlaybackRefusedError(
                "Prime foreground unconfirmed; playback command not submitted: " + str(exc)
            ) from exc

    def command(self, op, timeout=30, command_id=None, **fields):
        self._check()
        from .compatibility import OPERATION_CAPABILITIES

        capability = OPERATION_CAPABILITIES.get(op)
        if capability:
            self.require_capability(capability)
        if op in ("play-live", "play-content"):
            if self.health()["playback_inspection"]["state"] == "degraded" and self.health()["playback_inspection"]["recovery"] != "retry_after_cooldown":
                raise PlaybackRefusedError(
                    "playback inspection degraded; restart player session before a new launch"
                )
            self.wake_for_playback()
            self.foreground_for_playback()
        cid = command_id or uuid.uuid4().hex
        with self.cv:
            self.pending[cid] = None
        try:
            result = self._rpc(
                self.script.exports_sync.submitcontrol,
                {
                    "id": cid,
                    "op": op,
                    "ownerEpoch": getattr(
                        getattr(self.local, "lease", None),
                        "epoch",
                        self.ownership_epoch,
                    ),
                    **fields,
                },
            )
            if not result.get("queued"):
                raise OutcomeUnknownError("runtime did not acknowledge queueing")
            receipt = self.wait(lambda: self.pending.get(cid), timeout)
            if not receipt.get("success"):
                error = str(receipt.get("error", "command failed"))
                if op in ("play-live", "play-content") and receipt.get("launchDisposition") == "not_invoked":
                    raise PlaybackRefusedError(error)
                if "compatibility:" in error:
                    raise CompatibilityError(error)
                if op in ("search-data", "broadcasts-data"):
                    raise UnavailableError("metadata request rejected before submission")
                raise OutcomeUnknownError(error)
            return receipt
        except TimeoutError as exc:
            raise OutcomeUnknownError(
                "command receipt deadline; do not automatically retry"
            ) from exc
        except PrimeError:
            raise
        except Exception as exc:
            raise OutcomeUnknownError(
                "runtime transport failed; command may have executed; do not retry automatically"
            ) from exc
        finally:
            with self.cv:
                self.pending.pop(cid, None)

    def compatibility(self):
        from .compatibility import report

        with self.cv:
            result = report(
                self.compatibility_contexts,
                self.control_context,
                self.engine_evidence,
                degraded="compatibility_degraded" in self.seen
                or bool(self.engine_unavailable),
            )
            if self.engine_unavailable:
                result["layers"]["engine"]["reasons"] = [self.engine_unavailable]
            return result

    def adaptation_manifest(self):
        """Private host-only resource descriptor; never expose URLs in diagnostics."""
        self._check()
        if not self.script:
            return None
        return self._rpc(self.script.exports_sync.adaptationmanifest, timeout=5)

    def require_capability(self, name):
        decision = self.compatibility().get("capabilities", {}).get(name, {})
        if not decision.get("available"):
            raise CompatibilityError(
                name + ": " + "; ".join(decision.get("reasons", ["not_validated"]))
            )

    def health(self):
        disabled = sorted(self.disabled_native_methods.intersection({
            "app-player-plugin/getTitleId", "app-player-plugin/getCurrentPosition",
        }))
        inspection = {
            "state": "degraded" if disabled else (
                "available" if self.native_inspection_reported else "unknown"
            ),
            "disabled_methods": disabled,
            "reason": "native_player_query_failed" if disabled else None,
            "recovery": ("retry_after_cooldown" if all(k in self.native_retry_after for k in disabled)
                         else "restart_player_session") if disabled else None,
            "retry_after_ms": max((self.native_retry_after.get(k, 0) for k in disabled), default=0) or None,
            "scope": "query_availability; not proof of active playback",
        }
        return {
            "playback_inspection": inspection,
            "degraded": bool(disabled),
            "session_id": self.session_id,
            "serial": self.serial,
            "app_pid": self.app_pid,
            "runtime_memory": self.memory_samples[-1] if self.memory_samples else None,
            "runtime_version": self.compatibility().get("runtimeVersion"),
            "compatibility": self.compatibility(),
            "failure": self.failure,
            "closed": self.stopping.is_set(),
            "state": self.latest_state,
            "observation_age_seconds": (
                time.monotonic() - self.last_observation
                if self.last_observation
                else None
            ),
        }

    def close(self):
        # Cancellation is distinct from cleanup completion: a failed force-stop
        # must be retried before retiring the private instrumentation server.
        self.startup_cancelled.set()
        self.stopping.set()
        with self.cv:
            self.cv.notify_all()
        # Fixed native gates retain injected code. Stop our app process before
        # detaching instead of hot-unpatching a playing ARM process.
        with self.lifecycle_lock:
            if self.cleanup_complete:
                return
            cleanup_error = None
            if self.launched:
                try:
                    force_stop(self.adb)
                except Exception as exc:
                    cleanup_error = exc
            if self.gate:
                try:
                    self.gate.close()
                    self.gate = None
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
            if self.thread and self.thread is not threading.current_thread():
                self.thread.join(timeout=5)
                if self.thread.is_alive():
                    cleanup_error = cleanup_error or RuntimeError("runtime poller has not stopped")
            if cleanup_error:
                # Keep both the device ownership lock and session alive so the
                # supervisor can retry teardown without detaching live hooks.
                raise UnavailableError(
                    "Prime cleanup unconfirmed; do not assume instrumentation detached safely"
                ) from cleanup_error
            if self.session:
                try:
                    self._rpc(self.session.detach)
                except Exception as exc:
                    try:
                        detached = self.session.is_detached is True
                    except Exception:
                        detached = False
                    if not detached:
                        raise UnavailableError(
                            "Prime session detach unconfirmed; remote registration retained"
                        ) from exc
                try:
                    self.session.off("detached", self._detached)
                except Exception:
                    pass
                self.session = None
            if self.remote_registration_pending:
                try:
                    self._rpc(self.device_manager.remove_remote_device, self.endpoint, timeout=5)
                except Exception as exc:
                    # Frida17.2.17 uses exactly this typed result when a previous
                    # uncertain remove already completed (or add never committed).
                    if not (isinstance(exc, self.remote_missing_error) and str(exc) == "Device not found"):
                        raise UnavailableError(
                            "Frida remote registration cleanup unconfirmed; retry teardown"
                        ) from exc
                self.remote_registration_pending = False
                self.device_manager = None
            if self.script:
                try:
                    self.script.off("message", self._message)
                except Exception:
                    pass
                self.script = None
            if self.owner:
                self.owner.close()
                self.owner = None
            self.launched = False
            self.cleanup_complete = True
