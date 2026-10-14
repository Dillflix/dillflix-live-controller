"""Durable, bounded native identity cache; contains no manifest URLs or handles."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

from .native_adapter import descriptor_identity, NativeAdapterError, VALIDATOR_VERSION

MAX_RECORD_BYTES = 64 * 1024
MAX_ENTRIES = 128
_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class AdapterStore:
    """Cache observed binary identity, never a universal compatibility approval.

    The caller serializes lifecycle changes with its existing device ownership
    lock. Individual writes remain atomic and fsynced across process restarts.
    """

    def __init__(self, directory):
        self.directory = Path(directory)

    def _identity(self, base_profile, descriptor):
        engine = base_profile.get("engine", {})
        if engine.get("architecture") != "ARM32 Thumb":
            raise NativeAdapterError("native_base_engine_unsupported")
        return {
            "validatorVersion": VALIDATOR_VERSION,
            "engineSha256": hashlib.sha256(_wire(engine)).hexdigest(),
            "architecture": engine["architecture"],
            "resource": descriptor_identity(descriptor),
        }

    def key(self, base_profile, descriptor):
        return hashlib.sha256(_wire(self._identity(base_profile, descriptor))).hexdigest()

    def _read(self, path):
        try:
            if path.is_symlink() or path.stat().st_size > MAX_RECORD_BYTES:
                return None
            with path.open("rb") as stream:
                wire = stream.read(MAX_RECORD_BYTES + 1)
            if len(wire) > MAX_RECORD_BYTES:
                return None
            def invalid_constant(_):
                raise ValueError("nonfinite JSON number")
            return json.loads(wire, parse_constant=invalid_constant)
        except (OSError, ValueError, RecursionError):
            return None

    def _write(self, path, value):
        wire = _wire(value)
        if len(wire) > MAX_RECORD_BYTES:
            raise NativeAdapterError("native_cache_record_too_large")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Only this cache's known regular files may be evicted, never directories.
        active = self._read(self.directory / "active-native.json")
        active_name = str(active.get("key")) + ".json" if isinstance(active, dict) else None
        entries = sorted((p for p in self.directory.iterdir() if re.fullmatch(r"[a-f0-9]{64}(?:\.failure)?\.json", p.name) and p.name != active_name and p.is_file() and not p.is_symlink()), key=lambda p: p.stat().st_mtime)
        if not path.exists():
            for old in entries[:max(0, len(entries) - MAX_ENTRIES + 1)]:
                old.unlink()
        fd, temporary = tempfile.mkstemp(prefix=".native-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(wire)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _valid_native(native, identity):
        if not isinstance(native, dict) or set(native) != {"resourceName", "compressedSha256", "decodedSha256", "buildId", "checks", "executableAnchors"}:
            return False
        if native["resourceName"] != identity["resource"].get("name") or native["compressedSha256"] != identity["resource"].get("sha256"):
            return False
        if not re.fullmatch(r"[a-f0-9]{64}", str(native["decodedSha256"])) or not re.fullmatch(r"(?:[a-f0-9]{2}){4,64}", str(native["buildId"])):
            return False
        checks, anchors = native["checks"], native["executableAnchors"]
        if not isinstance(checks, list) or len(checks) != 1 or not isinstance(anchors, list) or len(anchors) != 3:
            return False
        if not isinstance(checks[0], dict) or set(checks[0]) != {"name", "offset", "hex"} or checks[0].get("name") != "GNU build-id note":
            return False
        if any(not isinstance(a, dict) or set(a) != {"offset", "hex"} for a in anchors):
            return False
        for entry in checks + anchors:
            if not isinstance(entry, dict) or type(entry.get("offset")) is not int or not 0 <= entry["offset"] < 2**32:
                return False
            if not isinstance(entry.get("hex"), str) or not re.fullmatch(r"(?:[a-f0-9]{2}){1,128}", entry["hex"]):
                return False
        return all(len(a["hex"]) == 128 for a in anchors) and len({a["offset"] for a in anchors}) == 3

    def _profile(self, base_profile, value, identity=None):
        if not isinstance(value, dict) or not isinstance(value.get("identity"), dict):
            return None
        stored = value["identity"]
        if identity is not None and stored != identity:
            return None
        if (stored.get("validatorVersion") != VALIDATOR_VERSION
                or stored.get("engineSha256") != hashlib.sha256(_wire(base_profile.get("engine", {}))).hexdigest()
                or stored.get("architecture") != base_profile.get("engine", {}).get("architecture")
                or not isinstance(stored.get("resource"), dict)
                or not self._valid_native(value.get("native"), stored)):
            return None
        expected = hashlib.sha256(_wire({"identity": stored, "native": value["native"]})).hexdigest()
        if value.get("digest") != expected:
            return None
        profile = deepcopy(base_profile)
        profile["testRuntime"] = value["native"]
        profile["automaticNative"] = {"validatorVersion": VALIDATOR_VERSION, "identity": stored["resource"], "qualification": "image_identity_only; live_method_contracts_required"}
        return profile

    def load(self, base_profile, descriptor):
        identity = self._identity(base_profile, descriptor)
        value = self._read(self.directory / (self.key(base_profile, descriptor) + ".json"))
        return self._profile(base_profile, value, identity)

    def load_active(self, base_profile):
        pointer = self._read(self.directory / "active-native.json")
        if (not isinstance(pointer, dict) or pointer.get("qualification") != "loaded_verified"
                or not re.fullmatch(r"[a-f0-9]{64}", str(pointer.get("key")))):
            return None
        value = self._read(self.directory / (pointer["key"] + ".json"))
        if not isinstance(value, dict) or hashlib.sha256(_wire(value.get("identity"))).hexdigest() != pointer["key"]:
            return None
        return self._profile(base_profile, value)

    def activate(self, base_profile, descriptor, profile):
        """Commit a profile only after the supervisor verifies the loaded image."""
        key = self.save(base_profile, descriptor, profile)
        self._write(self.directory / "active-native.json", {"key": key, "qualification": "loaded_verified"})
        self._write(self.directory / "trial-native.json", {"key": key, "state": "confirmed"})
        self.clear_failure(base_profile, descriptor)
        return key

    def begin_trial(self, base_profile, descriptor, profile):
        key = self.save(base_profile, descriptor, profile)
        self._write(self.directory / "trial-native.json", {"key": key, "state": "pending"})

    def fail_trial(self, reason="native_trial_interrupted"):
        """Pending trials are never defaults, including after a host crash."""
        trial = self._read(self.directory / "trial-native.json")
        if (not isinstance(trial, dict) or trial.get("state") != "pending"
                or not re.fullmatch(r"[a-f0-9]{64}", str(trial.get("key")))):
            return
        self._mark_key_failure(trial["key"], reason)
        self._write(self.directory / "trial-native.json", {**trial, "state": "failed", "reason": reason})

    def save(self, base_profile, descriptor, profile):
        identity = self._identity(base_profile, descriptor)
        native = deepcopy(profile.get("testRuntime"))
        if profile.get("engine") != base_profile.get("engine") or not self._valid_native(native, identity):
            raise NativeAdapterError("native_cache_candidate_invalid")
        key = self.key(base_profile, descriptor)
        existing = self.load(base_profile, descriptor)
        if existing is not None:
            if existing["testRuntime"] != native:
                raise NativeAdapterError("native_cache_identity_conflict")
            return key
        value = {"identity": identity, "native": native}
        value["digest"] = hashlib.sha256(_wire(value)).hexdigest()
        self._write(self.directory / (key + ".json"), value)
        return key

    def mark_failure(self, base_profile, descriptor, reason_code, *, evidence=None):
        return self._mark_key_failure(self.key(base_profile, descriptor), reason_code, evidence=evidence)

    def _mark_key_failure(self, key, reason_code, *, evidence=None):
        if not isinstance(reason_code, str) or not _CODE.fullmatch(reason_code):
            raise NativeAdapterError("native_cache_failure_code_invalid")
        path = self.directory / (key + ".failure.json")
        previous = self._read(path)
        attempts = previous.get("attempts", 0) if isinstance(previous, dict) else 0
        attempts = min(10, (attempts if type(attempts) is int and attempts >= 0 else 0) + 1)
        delay = min(3600, 30 * 2 ** (attempts - 1))
        self._write(path, {"attempts": attempts, "reason": reason_code, "retryAt": time.time() + delay,
                           "evidence": evidence})
        return delay

    def retry_after(self, base_profile, descriptor, *, evidence=None):
        value = self._read(self.directory / (self.key(base_profile, descriptor) + ".failure.json"))
        if isinstance(value, dict) and value.get("evidence") and value["evidence"] != evidence:
            return 0
        retry_at = value.get("retryAt") if isinstance(value, dict) else None
        return max(0, min(3600, retry_at - time.time())) if type(retry_at) in (float, int) else 0

    def clear_failure(self, base_profile, descriptor):
        path = self.directory / (self.key(base_profile, descriptor) + ".failure.json")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
