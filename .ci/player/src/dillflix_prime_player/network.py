"""VPN inventory, durable transition journal and authenticated device transport.

No credentials or OpenVPN configuration are accepted by the public player RPC.
The Android bridge owns its own dispatch journal; uncertain sends are inspected.
"""
from copy import deepcopy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import tarfile
import time

from .errors import NetworkError, OutcomeUnknownError, UnavailableError


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ValueError("invalid network identifier")
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def archive_network(value, output):
    """Export only public network metadata, without tokens/provider messages."""
    payload = json.dumps(value, indent=2, allow_nan=False).encode()
    if len(payload) > 256 * 1024:
        raise ValueError("network evidence exceeds archive limit")
    output = Path(output).expanduser().resolve()
    with output.open("xb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as archive:
        info = tarfile.TarInfo("network.json")
        info.size, info.mode = len(payload), 0o600
        archive.addfile(info, io.BytesIO(payload))
    return output


def deadline(value, now):
    if type(value) not in (int, float) or not math.isfinite(value) or not now < value <= now + 1800:
        raise ValueError("network deadline must be in the next 1800 seconds")
    return float(value)


class NetworkConfig:
    def __init__(self, value=None):
        value = value or {}
        if not isinstance(value, dict) or set(value) - {"enabled", "token_file", "profiles", "observation_ttl"}:
            raise ValueError("unknown network configuration field")
        self.enabled = value.get("enabled", False)
        if type(self.enabled) is not bool:
            raise ValueError("network enabled must be boolean")
        self.token_file = value.get("token_file")
        self.ttl = value.get("observation_ttl", 30)
        if type(self.ttl) is not int or not 10 <= self.ttl <= 60:
            raise ValueError("network observation_ttl must be 10..60")
        profiles = value.get("profiles", [])
        if not isinstance(profiles, list) or len(profiles) > 32:
            raise ValueError("network profiles must be a list of at most 32 entries")
        self.profiles = {}
        for p in profiles:
            fields = {"id", "profile_uuid", "country", "province", "city", "server", "routing_confirmed"}
            if not isinstance(p, dict) or set(p) != fields:
                raise ValueError("network profile fields do not match the inventory contract")
            for key in fields - {"routing_confirmed"}:
                if not isinstance(p[key], str) or not 1 <= len(p[key]) <= 128:
                    raise ValueError("invalid network profile metadata")
            identifier(p["id"])
            if p["id"] == "direct" or p["id"] in self.profiles:
                raise ValueError("duplicate or reserved network target")
            if type(p["routing_confirmed"]) is not bool:
                raise ValueError("routing_confirmed must be boolean")
            self.profiles[p["id"]] = deepcopy(p)
        if self.enabled and (not isinstance(self.token_file, str) or not Path(self.token_file).is_absolute()):
            raise ValueError("enabled VPN requires an absolute token_file")
        self.revision = fingerprint(self.profiles)

    @classmethod
    def read(cls, path):
        if not path:
            return cls()
        path = Path(path)
        if path.stat().st_size > 65536:
            raise ValueError("network configuration too large")
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def target(self, target_id):
        identifier(target_id)
        if target_id == "direct":
            return {"id": "direct", "profile_uuid": None}
        if target_id not in self.profiles:
            raise NetworkError("network_target_unknown")
        return deepcopy(self.profiles[target_id])


class NetworkJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.value = {"schema": 1, "operations": {}, "manual_hold": False,
                      "owner_id": None, "input_uncertain": False, "qualifications": {}}
        if self.path.exists():
            if self.path.stat().st_size > 4 * 1024 * 1024:
                raise NetworkError("network_journal_invalid")
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("schema") != 1 or not isinstance(saved.get("operations"), dict):
                raise NetworkError("network_journal_invalid")
            self.value.update(saved)

    def save(self):
        # Only terminal, expired requests may be evicted. An identical old retry
        # then fails its original deadline instead of replaying a physical effect.
        ops = self.value["operations"]
        removable = sorted((o for o in ops.values() if o.get("finished_at") and
                            o["deadline_at"] < time.time()), key=lambda o: o["finished_at"])
        for old in removable[:max(0, len(ops) - 256)]:
            del ops[old["operation_id"]]
        temp = self.path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.value, f, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.path)
        if os.name != "nt":
            fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


class AdbVpnBridge:
    """Bounded newline JSON over an owned ADB forward to an Android local socket."""
    def __init__(self, serial, token_file, *, adb="adb"):
        self.adb = [adb, "-s", serial]
        self.token_file = Path(token_file)
        self.port = None
        self.lock = threading.Lock()

    def _forward(self):
        if self.port is None:
            p = subprocess.run(self.adb + ["forward", "tcp:0", "localabstract:dillflix.vpn"],
                               capture_output=True, text=True, check=True, timeout=5)
            port = p.stdout.strip()
            if not port.isdecimal() or not 1024 <= int(port) <= 65535:
                raise UnavailableError("network_bridge_forward_failed")
            self.port = int(port)

    def request(self, method, **params):
        with self.lock:
            try:
                if os.name != "nt" and self.token_file.stat().st_mode & 0o077:
                    raise NetworkError("network_pairing_permissions")
                secret = self.token_file.read_text(encoding="utf-8").strip()
                if not re.fullmatch(r"[a-f0-9]{64}", secret):
                    raise NetworkError("network_pairing_invalid")
                self._forward()
                wire = json.dumps({"protocol": 1, "secret": secret, "method": method, "params": params}).encode() + b"\n"
                with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
                    s.settimeout(8)
                    s.sendall(wire)
                    data = bytearray()
                    while b"\n" not in data and len(data) <= 131072:
                        part = s.recv(8192)
                        if not part:
                            break
                        data.extend(part)
                    if not data.endswith(b"\n") or len(data) > 131072:
                        raise ValueError("invalid bridge response")
                    result = json.loads(data)
                if result.get("protocol") != 1:
                    raise NetworkError("network_bridge_incompatible")
                if "error" in result:
                    code = result["error"]
                    raise NetworkError(code if isinstance(code, str) and re.fullmatch(r"network_[a-z_]{1,64}", code)
                                       else "network_bridge_failed")
                return result["result"]
            except NetworkError:
                raise
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                # Never repeat a mutation to repair a forward/connection failure.
                self._close()
                raise OutcomeUnknownError("network_bridge_unavailable; inspect the operation before retrying") from exc

    def _close(self):
        if self.port is not None:
            try:
                subprocess.run(self.adb + ["forward", "--remove", "tcp:" + str(self.port)],
                               capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
            self.port = None

    def close(self):
        with self.lock:
            self._close()
