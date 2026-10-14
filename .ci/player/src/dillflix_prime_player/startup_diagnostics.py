"""Bounded diagnostics for failures before the API harness can connect."""

from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import traceback
import uuid


def capture_failure(state_dir, serial, error, backend=None, private=None):
    stamp = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:6]
    )
    directory = Path(state_dir) / "failures" / ("startup-" + stamp)
    directory.mkdir(parents=True, mode=0o700)
    summary = {
        "time": datetime.now(timezone.utc).isoformat(),
        "error_type": type(error).__name__,
        "error": str(error),
        "phase": getattr(backend, "startup_phase", "server_provisioning"),
        "connection_timeout": getattr(backend, "connection_timeout", None),
        "prime_restart_requested": getattr(backend, "launched", False),
        "endpoint": getattr(backend, "endpoint", None),
    }
    if private:
        summary["server"] = {
            "path": private.path,
            "pid": private.pid,
            "port": private.port,
            "forward": private.forward,
            "identity": private.identity,
        }
    (directory / "failure.json").write_text(json.dumps(summary, indent=2) + "\n")
    (directory / "traceback.txt").write_text(
        "".join(traceback.format_exception(type(error), error, error.__traceback__))
    )
    print(
        f"Collecting startup diagnostics in {directory} (bounded ADB reads).",
        flush=True,
    )

    def save(name, operation):
        try:
            value = operation()
        except Exception as exc:
            value = (json.dumps({"capture_error": type(exc).__name__, "message": str(exc)})
                     if name.endswith(".json") else f"{type(exc).__name__}: {exc}\n")
        if len(value) > 4 * 1024 * 1024 and name.endswith(".json"):
            value = json.dumps(
                {"truncated": True, "reason": "diagnostic JSON exceeds 4 MiB"}
            )
        (directory / name).write_text(value[: 4 * 1024 * 1024])

    def adb(*args):
        p = subprocess.run(
            ["adb", "-s", serial, *args], capture_output=True, text=True, timeout=8
        )
        return f"exit_code={p.returncode}\n" + p.stdout + p.stderr

    if backend and hasattr(backend, "diagnostic_snapshot"):
        def runtime_evidence():
            evidence = backend.diagnostic_snapshot()
            # Keep complete JSON records, newest first in the budget, without invoking
            # records()/health() which may reject an already-failed backend.
            retained, size = [], 0
            for record in reversed(evidence.pop("runtime_records", [])):
                encoded = json.dumps(record, ensure_ascii=True)
                if size + len(encoded) > 2 * 1024 * 1024:
                    break
                retained.append(record)
                size += len(encoded)
            evidence["runtime_records"] = list(reversed(retained))
            return json.dumps(evidence, indent=2)

        save("runtime-evidence.json", runtime_evidence)
    save(
        "android-crash.log",
        lambda: adb("logcat", "-b", "crash", "-d", "-v", "threadtime", "-t", "300"),
    )
    save(
        "android-recent.log",
        lambda: adb("logcat", "-d", "-v", "threadtime", "-t", "1500"),
    )
    save(
        "android-activities.txt",
        lambda: adb("shell", "dumpsys", "activity", "activities"),
    )
    save("adb-state.txt", lambda: adb("get-state"))
    save("adb-forwards.txt", lambda: adb("forward", "--list"))
    if private:
        save(
            "frida-server.log",
            lambda: private.shell(
                "tail -c 65536 " + shlex.quote(private.path + ".log"), root=True
            ),
        )
        save(
            "device-listeners.txt",
            lambda: private.shell("cat /proc/net/tcp /proc/net/tcp6", root=True),
        )
        if private.pid:
            from .server_identity import read_identity_command

            save(
                "frida-process.txt",
                lambda: private.shell(read_identity_command(private.pid), root=True),
            )
    archive = directory.with_name(directory.name + ".tar.gz")
    with tarfile.open(archive, "w:gz") as output:
        output.add(directory, arcname=directory.name)
    print(f"STARTUP CAPTURE ARCHIVE: {archive}", flush=True)
    return archive

