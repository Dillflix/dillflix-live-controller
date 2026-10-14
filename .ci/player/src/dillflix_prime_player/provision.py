"""Provision only a supplied Frida binary using already available device root."""

import hashlib
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from .errors import UnavailableError
from .server_identity import (
    listening_ports,
    parse_identity,
    read_identity_command,
    select_port,
    stop_owned_command,
)


class PrivateServer:
    def __init__(self, serial, binary, root_prefix=None):
        self.adb = ["adb", "-s", serial]
        self.binary = Path(binary)
        self.prefix = root_prefix
        self.cache_dir = "/data/local/tmp/dillflix-prime-cache"
        self.path = None
        self.identity = None
        self.forward = None
        self.pid = None
        self.port = None

    def run(self, *args, timeout=15):
        p = subprocess.run(
            self.adb + list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        return p.stdout.strip()

    def shell(self, command, root=False):
        if root and self.prefix:
            command = (
                " ".join(shlex.quote(p) for p in shlex.split(self.prefix))
                + " "
                + shlex.quote(command)
            )
        return self.run("shell", command)

    def prepare_binary(self):
        """Reuse immutable content-addressed code, including after host crashes.

        Stage in a fresh directory, verify, then hard-link without replacing a
        potentially executing inode. Only abandoned staging directories age out.
        No existing executable or unrelated experiment artifact is swept.
        """
        digest = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        self.path = self.cache_dir + "/frida-17.2.17-arm-" + digest
        directory = shlex.quote(self.cache_dir)
        self.shell(
            f"[ ! -L {directory} ] && mkdir -p {directory} && chmod 700 {directory}"
        )
        # Directory mtimes are created on-device, unlike adb-pushed file mtimes.
        # Staging is never executable and every ADB upload has a 60-second timeout.
        self.shell(
            f"""find {directory} -mindepth 1 -maxdepth 1 -type d -name 'upload-*' -mmin +60 | while IFS= read -r stage; do
  [ ! -L "$stage" ] || continue
  name=${{stage##*/}}
  suffix=${{name#upload-}}
  [ "${{#suffix}}" -eq 32 ] || continue
  case "$suffix" in *[!0123456789abcdef]*|'') continue;; esac
  rm -f "$stage/server" && rmdir "$stage"
done""",
            root=True,
        )
        path = shlex.quote(self.path)
        existing = self.shell(
            f"if [ -L {path} ]; then exit 1; fi; "
            f"if [ -e {path} ]; then sha256sum {path}; else echo MISSING; fi",
            root=True,
        )
        if existing != "MISSING":
            if existing.split()[0:1] != [digest]:
                raise UnavailableError(
                    "Cached Frida checksum mismatch; existing file left untouched"
                )
            return
        stage = self.cache_dir + "/upload-" + uuid.uuid4().hex
        staged = stage + "/server"
        self.shell("mkdir " + shlex.quote(stage))
        try:
            self.run("push", str(self.binary), staged, timeout=60)
            actual = self.shell("sha256sum " + shlex.quote(staged), root=True)
            if actual.split()[0:1] != [digest]:
                raise UnavailableError("Uploaded Frida checksum mismatch")
            self.shell("chmod 700 " + shlex.quote(staged), root=True)
            # Another provisioner can win publication; never replace its inode.
            self.shell(
                "ln "
                + shlex.quote(staged)
                + " "
                + path
                + " 2>/dev/null || test -f "
                + path,
                root=True,
            )
            verified = self.shell(
                "[ ! -L " + path + " ] && sha256sum " + path, root=True
            )
            if verified.split()[0:1] != [digest]:
                raise UnavailableError("Published Frida checksum mismatch")
        finally:
            self.shell(
                "rm -f " + shlex.quote(staged) + "; rmdir " + shlex.quote(stage),
                root=True,
            )

    def start(self):
        header = self.binary.read_bytes()[:20]
        if (
            len(header) < 20
            or header[:6] != b"\x7fELF\x01\x01"
            or int.from_bytes(header[18:20], "little") != 40
        ):
            raise ValueError(
                "provide the decompressed Android ARM32 frida-server binary"
            )
        if self.prefix is None:
            if "uid=0(" in self.shell("id"):
                self.prefix = ""
            else:
                self.prefix = "su -c"
        if "uid=0(" not in self.shell("id", root=True):
            raise UnavailableError(
                "existing root unavailable; this library never roots or reboots a device"
            )
        self.port = select_port(
            listening_ports(self.shell("cat /proc/net/tcp /proc/net/tcp6", root=True))
        )
        try:
            self.prepare_binary()
            if (
                self.shell(shlex.quote(self.path) + " --version", root=True)
                != "17.2.17"
            ):
                raise ValueError("Frida server must be 17.2.17")
            # Reuse two log names per binary build, rather than one per start.
            log_path = shlex.quote(self.path + ".log")
            self.shell(
                f"if [ -f {log_path} ]; then tail -c 65536 {log_path} > "
                + shlex.quote(self.path + ".log.previous")
                + "; fi",
                root=True,
            )
            output = self.shell(
                "nohup "
                + shlex.quote(self.path)
                + " -l 127.0.0.1:"
                + str(self.port)
                + " >"
                + shlex.quote(self.path + ".log")
                + " 2>&1 </dev/null & echo $!",
                root=True,
            )
            if not output.isdigit():
                raise UnavailableError("server PID was not established")
            self.pid = int(output)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                self.identity = parse_identity(
                    self.shell(read_identity_command(self.pid), root=True),
                    self.pid,
                    self.path,
                    self.port,
                )
                if self.identity and self.port in listening_ports(
                    self.shell("cat /proc/net/tcp /proc/net/tcp6", root=True)
                ):
                    break
                time.sleep(0.2)
            else:
                raise UnavailableError("private Frida server did not start")
            forward = self.run("forward", "tcp:0", "tcp:" + str(self.port))
            if not forward.isdigit():
                raise UnavailableError("ADB forward port unavailable")
            self.forward = forward
            return "127.0.0.1:" + forward
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.identity:
            self.shell(stop_owned_command(self.identity), root=True)
            self.identity = None
            self.pid = None
        elif self.pid:
            raise UnavailableError(
                "server identity not verified; left untouched; inspect "
                + self.path
                + ".log"
            )
        if self.forward:
            self.run("forward", "--remove", "tcp:" + self.forward)
            self.forward = None
        # Immutable binary is deliberately cached. Starts reuse the same binary
        # and current/previous log names, even after an unclean host exit.
