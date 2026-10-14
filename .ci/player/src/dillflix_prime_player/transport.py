"""Bounded native Frida RPCs, ADB launch, and cleanup for this finite experiment."""

import queue
import shlex
import subprocess
import threading
import time

PACKAGE = "com.amazon.firebat"
ACTIVITY = "com.amazon.pyrocore.IgnitionActivity"


def rpc(call, *args, timeout=10, **kwargs):
    """Cancel a stuck RPC without leaving an unbounded helper thread behind."""
    import frida

    cancellable = frida.Cancellable()
    timer = threading.Timer(timeout, cancellable.cancel)
    timer.daemon = True
    timer.start()
    try:
        with cancellable:
            return call(*args, **kwargs)
    finally:
        timer.cancel()


def force_stop(adb):
    subprocess.run(
        adb + ["shell", "am", "force-stop", PACKAGE],
        capture_output=True,
        text=True,
        check=True,
        timeout=8,
    )
    for _ in range(4):
        probe = subprocess.run(adb + ["shell", "pidof", PACKAGE], capture_output=True,
                               text=True, timeout=3)
        if probe.returncode in (0, 1) and not probe.stdout.strip() and not probe.stderr.strip():
            return
        if probe.returncode not in (0, 1):
            raise RuntimeError("Prime process retirement could not be inspected")
        time.sleep(0.25)
    raise RuntimeError("Prime process retirement was not confirmed")


class AdbGate:
    """Hold only the first exact Prime spawn until its script is loaded.

    Enable/disable operate on Frida's native zygote path. A separate worker
    resumes other spawn events while the main thread attaches to Prime. Use
    with the capture's private server; never with a shared instrumentation run.
    """

    def __init__(self, device, adb, record, launch_timeout=90, cancelled=None):
        self.device, self.adb, self.record = device, adb, record
        self.events = queue.Queue()
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.pid = None
        self.error = None
        self.process = None
        self.gating = False
        self.registered = False
        self.worker = None
        self.requested = False
        self.deadline_timer = None
        self.disable_lock = threading.Lock()
        self.callback = lambda spawn: self.events.put(spawn)
        self.launch_timeout = launch_timeout
        self.cancelled = cancelled or (lambda: False)

    def _drain(self):
        while not self.stopping.is_set():
            try:
                spawn = self.events.get(timeout=0.1)
            except queue.Empty:
                continue
            if spawn.identifier == PACKAGE and self.pid is None:
                self.pid = spawn.pid
                self.record("spawn_gated", pid=spawn.pid, identifier=spawn.identifier)
                self.ready.set()
            else:
                try:
                    rpc(self.device.resume, spawn.pid, timeout=3)
                    self.record(
                        "other_spawn_resumed",
                        pid=spawn.pid,
                        identifier=spawn.identifier,
                    )
                except Exception as error:
                    self.error = error
                    self.ready.set()

    def start(self):
        if self.cancelled():
            raise RuntimeError("Probe stopped before ADB activity startup")
        self.device.on("spawn-added", self.callback)
        self.registered = True
        self.worker = threading.Thread(target=self._drain, daemon=True)
        self.worker.start()
        # Set before the call: a timeout may occur after the server enabled it.
        self.gating = True
        if self.cancelled():
            raise RuntimeError("Probe stopped before spawn gating")
        rpc(self.device.enable_spawn_gating, timeout=12)
        self.record("spawn_gating_enabled")
        self.deadline_timer = threading.Timer(20, self._expire)
        self.deadline_timer.daemon = True
        self.deadline_timer.start()
        self.requested = True
        # -W waits for activity startup. Never wait for this subprocess before
        # attaching and resuming the gated child; that would deadlock launch.
        if self.cancelled():
            raise RuntimeError("Probe stopped before ADB activity launch")
        self.process = subprocess.Popen(
            self.adb + ["shell", "am", "start", "-W", "-n", PACKAGE + "/" + ACTIVITY],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if not self.ready.wait(10) or self.pid is None or self.error is not None:
            raise RuntimeError(
                "No usable gated Prime spawn within 10 seconds: " + str(self.error)
            )
        return self.pid

    def _expire(self):
        self.error = RuntimeError("Gated launch exceeded its 20-second budget")
        self.ready.set()
        try:
            self.disable()
        except Exception as error:
            self.record(
                "cleanup_warning", mechanism="gate_deadline", message=str(error)
            )

    def resume(self):
        if self.cancelled():
            raise RuntimeError("Probe stopped before resuming Prime")
        if self.error:
            raise RuntimeError(
                "Could not release an unrelated spawn: " + str(self.error)
            )
        rpc(self.device.resume, self.pid, timeout=5)
        self.record("process_resumed", pid=self.pid, mechanism="adb_gated")
        self.disable()
        if self.error:
            raise RuntimeError(str(self.error))
        # Gating is already disabled. Loading the pinned native test runtime can
        # take longer than Android's first-frame acknowledgement. Give that
        # separate phase a finite budget and preserve partial ADB output.
        started = time.monotonic()
        deadline = started + self.launch_timeout
        while True:
            if self.cancelled():
                raise RuntimeError(
                    "Probe stopped while waiting for ADB activity startup"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.record(
                    "adb_launch_timeout",
                    pid=self.pid,
                    timeoutSeconds=self.launch_timeout,
                )
                raise RuntimeError(
                    f"ADB activity startup did not finish within {self.launch_timeout} seconds; see launch progress and probe records"
                )
            try:
                stdout, stderr = self.process.communicate(timeout=min(5, remaining))
                break
            except subprocess.TimeoutExpired as error:

                def as_text(value):
                    return (
                        value.decode("utf-8", errors="replace")
                        if isinstance(value, bytes)
                        else value or ""
                    )

                self.record(
                    "adb_launch_pending",
                    pid=self.pid,
                    elapsedSeconds=round(time.monotonic() - started, 1),
                    budgetSeconds=self.launch_timeout,
                    stdout=as_text(error.output),
                    stderr=as_text(error.stderr),
                )
        self.record(
            "adb_launch_result",
            stdout=stdout,
            stderr=stderr,
            exitCode=self.process.returncode,
        )
        if self.process.returncode or "Status: ok" not in stdout:
            raise RuntimeError("ADB activity launch did not report Status: ok")

    def disable(self):
        with self.disable_lock:
            if self.gating:
                # The 17.2.17 server resumes all still-pending spawns on disable.
                rpc(self.device.disable_spawn_gating, timeout=5)
                self.gating = False
                self.record("spawn_gating_disabled")
            if self.deadline_timer:
                self.deadline_timer.cancel()

    def close(self):
        try:
            self.disable()
        finally:
            self.stopping.set()
            if self.registered:
                self.device.off("spawn-added", self.callback)
                self.registered = False
            if self.worker:
                self.worker.join(timeout=4)
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.communicate(timeout=2)
