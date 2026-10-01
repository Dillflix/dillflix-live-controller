"""Explicit, bounded installation/readiness commands for the supplied media probe.

Nothing here is called by controller startup or Play. Installation preserves
app data; granting notification-listener access requires its explicit CLI flag.
"""

import argparse
import asyncio
import hashlib
import json
import os
from importlib.resources import as_file, files
from pathlib import Path
from types import SimpleNamespace

from .adb import AdbDevice
from .config import ExecutorConfig
from .media import PROBE
from .models import ExecutorError

APK_SHA256 = "24a71c8ed36864125b85b519b7cd125067c9214ac549d86f0fa22adb5c547392"
SERVICE = PROBE + "/.ProbeService"
ACTIVITY = PROBE + "/.StartActivity"


def verify_apk(path, expected=APK_SHA256):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Probe APK must be a regular file under 4 MiB")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if not expected or actual != expected.lower():
        raise ValueError("Probe APK SHA-256 does not match the expected artifact")
    return actual


async def check(device):
    runtime = await device.runtime()
    session = runtime.get("session") or {}
    return {
        "package": PROBE,
        "ready": bool(
            runtime.get("source_health") == "fresh"
            and runtime.get("probe_instance")
            and runtime.get("history_available")
        ),
        "source_health": runtime.get("source_health"),
        "error": runtime.get("source_error"),
        "probe_instance": runtime.get("probe_instance"),
        "journal_readable": bool(runtime.get("history_available")),
        "prime_session_count": runtime.get("session_count"),
        "transport": session.get("transport", "unknown"),
        "runtime_media_id": session.get("runtime_media_id"),
        "observed_at": runtime.get("observed_at"),
        "scope": "Probe connection and journal export; no event/live-playback verification",
    }


async def install(device, path, *, expected=APK_SHA256, grant_listener_access=False, readiness_timeout=30):
    digest = verify_apk(path, expected)  # Verify before contacting the device.
    output = (await device.run("install", "-r", str(Path(path).resolve()))).decode(errors="replace")
    if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in output:
        raise ExecutorError(
            "probe_signature_mismatch",
            "Existing probe and traces were preserved; signing certificates differ",
            retryable=False,
        )
    if not any(line.strip() == "Success" for line in output.splitlines()):
        raise ExecutorError("probe_install_unconfirmed", "ADB did not confirm probe installation")
    if grant_listener_access:
        result = await device.shell("cmd", "notification", "allow_listener", SERVICE)
        if any(word in result.casefold() for word in ("unknown command", "not allowed", "usage:", "denied")):
            raise ExecutorError(
                "probe_permission_unconfirmed", "Notification-listener access was not granted"
            )
    # This source activity has no display and only requests a listener rebind.
    await device.shell("am", "start", "-n", ACTIVITY)
    latest = None
    try:
        async with asyncio.timeout(readiness_timeout):
            while True:
                # Explicit install/rebind gets a bounded readiness retry, unlike
                # ordinary monitoring's unavailable-probe backoff.
                device._probe_retry_at = 0
                latest = await check(device)
                if latest["ready"]:
                    return {**latest, "installed_apk_sha256": digest}
                await asyncio.sleep(0.5)
    except TimeoutError:
        return {
            **(latest or {"ready": False, "package": PROBE}),
            "installed_apk_sha256": digest,
            "error": "probe_not_ready",
            "next_step": "Check notification-listener access and journal export; installation alone does not grant access",
        }


async def execute(args, path):
    # The installer needs no model endpoint, controller database or playback job.
    configuration = SimpleNamespace(
        screen_adb_serial=args.serial,
        screen_adb_path=args.adb,
        executor=ExecutorConfig(adb_timeout=args.adb_timeout),
    )
    device = AdbDevice(configuration)
    try:
        await device.ready()
        if args.command == "check":
            return await check(device)
        return await install(
            device, path, expected=args.sha256 or APK_SHA256, grant_listener_access=args.grant_listener_access
        )
    finally:
        await device.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify-apk", "check", "install"])
    parser.add_argument("--serial", default=os.getenv("SCREEN_ADB_SERIAL", ""))
    parser.add_argument("--adb", default=os.getenv("SCREEN_ADB_PATH", "adb"))
    parser.add_argument("--adb-timeout", type=float, default=10)
    parser.add_argument(
        "--grant-listener-access",
        action="store_true",
        help="Explicitly grant the probe notification-listener access during install",
    )
    parser.add_argument("--apk", type=Path, help="Use a locally built APK; requires --sha256")
    parser.add_argument("--sha256", help="Expected SHA-256 for --apk")
    args = parser.parse_args()
    if bool(args.apk) != bool(args.sha256):
        parser.error("--apk and --sha256 must be supplied together")
    if args.command != "install" and args.grant_listener_access:
        parser.error("--grant-listener-access is only valid with install")
    if args.command != "verify-apk" and not args.serial:
        parser.error("Provide --serial or SCREEN_ADB_SERIAL")
    if not 0 < args.adb_timeout <= 60:
        parser.error("--adb-timeout must be in (0, 60]")
    try:
        resource = files("controller.executor").joinpath("assets/prime-media-probe.apk")
        with as_file(resource) as bundled:
            path = args.apk or bundled
            digest = verify_apk(path, args.sha256 or APK_SHA256)
            result = (
                {"apk_sha256": digest, "verified": True}
                if args.command == "verify-apk"
                else asyncio.run(execute(args, path))
            )
        print(json.dumps(result, indent=2))
        if result.get("ready") is False:
            raise SystemExit(1)
    except ExecutorError as error:
        parser.exit(1, json.dumps({"error": error.detail()}) + "\n")
    except (OSError, ValueError) as error:
        parser.exit(1, f"Probe setup error: {error}\n")


if __name__ == "__main__":
    main()
