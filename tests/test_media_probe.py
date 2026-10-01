"""Pinned probe packaging and explicit installer/permission boundaries."""

import copy
import sys
from importlib.resources import files

import pytest
from test_real_executor import settings
from test_runtime_evidence import MEDIA, update

from controller.executor.adb import AdbDevice
from controller.executor.media import MediaTracker, identity_key
from controller.executor.models import ExecutorError
from controller.executor.probe import ACTIVITY, APK_SHA256, SERVICE, check, install, verify_apk


class Device:
    def __init__(self, *, ready=True, result=b"Success\n"):
        self.commands, self.is_ready, self.result = [], ready, result

    async def run(self, *args):
        self.commands.append(args)
        return self.result

    async def shell(self, *args):
        self.commands.append(args)
        return ""

    async def runtime(self):
        self.commands.append(("runtime",))
        return {
            "source_health": "fresh" if self.is_ready else "disconnected",
            "probe_instance": "123:456",
            "history_available": self.is_ready,
            "session": None,
            "session_count": 0,
        }


def apk():
    return files("controller.executor").joinpath("assets/prime-media-probe.apk")


def test_original_packaged_apk_is_checksum_pinned():
    assert verify_apk(apk()) == APK_SHA256


async def test_apk_corruption_is_rejected_before_any_device_side_effect(tmp_path):
    broken = tmp_path / "corrupt.apk"
    broken.write_bytes(b"wrong content")
    device = Device()
    with pytest.raises(ValueError, match="SHA-256"):
        await install(device, broken, grant_listener_access=True)
    assert not device.commands


@pytest.mark.parametrize("grant", [False, True])
async def test_install_only_grants_listener_access_when_explicitly_requested(grant):
    device = Device()
    result = await install(device, apk(), grant_listener_access=grant)
    assert result["ready"] and result["prime_session_count"] == 0
    assert device.commands[0][:2] == ("install", "-r")
    assert (("cmd", "notification", "allow_listener", SERVICE) in device.commands) is grant
    assert ("am", "start", "-n", ACTIVITY) in device.commands
    assert not any(
        "uninstall" in command or "force-stop" in command or "keyevent" in command
        for command in device.commands
    )


async def test_signature_conflict_preserves_app_and_journals():
    device = Device(result=b"Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]\n")
    with pytest.raises(ExecutorError) as error:
        await install(device, apk(), grant_listener_access=True)
    assert error.value.code == "probe_signature_mismatch"
    assert len(device.commands) == 1


async def test_adb_install_stderr_classifies_signature_conflict(tmp_path):
    config = settings(tmp_path)
    executable = tmp_path / "adb-probe-fixture"
    executable.write_text(
        "#!" + sys.executable + "\nimport sys\n"
        "sys.stderr.write('Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]')\nsys.exit(1)\n"
    )
    executable.chmod(0o755)
    from dataclasses import replace

    device = AdbDevice(replace(config, screen_adb_path=str(executable)))
    with pytest.raises(ExecutorError) as error:
        await device.run("install", "-r", "probe.apk")
    assert error.value.code == "probe_signature_mismatch"


async def test_permission_denied_never_launches_or_claims_readiness():
    device = Device()

    async def denied(*args):
        device.commands.append(args)
        return "Unknown command: allow_listener"

    device.shell = denied
    with pytest.raises(ExecutorError) as error:
        await install(device, apk(), grant_listener_access=True)
    assert error.value.code == "probe_permission_unconfirmed"
    assert not any(command[:2] == ("am", "start") for command in device.commands)


async def test_installed_but_unready_probe_has_bounded_wait_and_check_is_read_only():
    device = Device(ready=False)
    result = await install(device, apk(), readiness_timeout=0.02)
    assert not result["ready"] and result["error"] == "probe_not_ready"
    device.commands.clear()
    assert not (await check(device))["ready"]
    assert device.commands == [("runtime",)]


def test_probe_process_restart_changes_identity_even_when_session_hash_is_reused():
    tracker = MediaTracker()
    before = update(tracker, MEDIA[4], probe_instance="123:456")
    after = update(tracker, MEDIA[4], probe_instance="123:789")
    assert identity_key(before) != identity_key(after)


@pytest.mark.parametrize("reason", ["session_destroyed", "disconnected", "connected", "poll_error"])
def test_lifecycle_callback_with_same_composite_snapshot_withdraws_continuity(reason):
    tracker = MediaTracker()
    before = update(tracker, MEDIA[4])
    callback = copy.deepcopy(MEDIA[4])
    callback.update(
        reason=reason,
        elapsedRealtimeMs=callback["elapsedRealtimeMs"] + 100,
        sequence=callback["sequence"] + 1,
    )
    callback["eventPayload"]["value"] = None
    after = update(tracker, callback)
    assert identity_key(before) != identity_key(after)


def test_bounded_tail_sequence_gap_withdraws_continuity():
    tracker = MediaTracker()
    before = update(tracker, MEDIA[4])
    later = copy.deepcopy(MEDIA[4])
    later.update(elapsedRealtimeMs=later["elapsedRealtimeMs"] + 100, sequence=later["sequence"] + 2)
    after = update(tracker, later)
    assert after["history_gap"]
    assert identity_key(before) != identity_key(after)


async def test_probe_restart_during_collection_cannot_return_a_bound_sample(tmp_path):
    device = AdbDevice(settings(tmp_path))
    counter = 0

    async def instance():
        nonlocal counter
        counter += 1
        return f"123:{counter}"

    async def shell(*args):
        if args[0] == "cat":
            return (
                "12345678-1234-1234-1234-123456789abc\n" + str(MEDIA[4]["elapsedRealtimeMs"] / 1000) + " 0\n"
            )
        import json

        return json.dumps(MEDIA[4])

    async def run(*args):
        return b""

    device.probe_instance, device.shell, device.run = instance, shell, run
    result = await device.runtime(device_state=("com.amazon.firebat", []))
    assert result["source_health"] == "unavailable"
    assert result["source_error"] == "probe_process_changed"
    assert identity_key(result) is None
