"""Select a device port and stop only the capture's verified server process."""

import re
import secrets
import shlex


def listening_ports(text):
    """Parse both Linux /proc/net/tcp tables; include wildcard and IPv6 binds."""
    ports = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A":
            continue
        if re.fullmatch(r"[0-9A-Fa-f]+:[0-9A-Fa-f]{4}", fields[1]):
            ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return ports


def select_port(occupied, requested=None, excluded=()):
    blocked = set(occupied) | set(excluded)
    if requested is not None:
        if requested in blocked:
            raise RuntimeError(
                f"Requested device port {requested} is already in use; omit --server-port to allocate a private port"
            )
        return requested
    available = [port for port in range(37042, 61000) if port not in blocked]
    if not available:
        raise RuntimeError("No available private Frida port in 37042..60999")
    return secrets.choice(available)


def read_identity_command(pid):
    pid = int(pid)
    return f"""if [ ! -r /proc/{pid}/stat ]; then echo PV_SERVER_GONE; exit 0; fi
cat /proc/{pid}/stat || exit 1
tr '\\000' '\\n' < /proc/{pid}/cmdline || exit 1
echo PV_IDENTITY_END"""


def parse_identity(text, pid, path, port):
    lines = text.strip().splitlines()
    if lines == ["PV_SERVER_GONE"]:
        return None
    expected = [path, "-l", "127.0.0.1:" + str(port)]
    if len(lines) != 5 or lines[-1] != "PV_IDENTITY_END" or lines[1:-1] != expected:
        raise RuntimeError(
            "Server identity did not match the capture executable and listen address"
        )
    stat = lines[0]
    fields = stat.rsplit(") ", 1)[-1].split()
    if stat.split()[0] != str(pid) or len(fields) < 20 or not fields[19].isdigit():
        raise RuntimeError("Server process start time could not be verified")
    if fields[0] in ("Z", "X"):
        return None
    return dict(pid=str(pid), path=path, port=port, startTime=fields[19])


def stop_owned_command(identity):
    """Identity is rechecked before each signal, including TERM -> KILL fallback."""
    pid = int(identity["pid"])
    start = str(identity["startTime"])
    if not start.isdigit():
        raise ValueError("Invalid server start time")
    argv = "\n".join(
        [identity["path"], "-l", "127.0.0.1:" + str(int(identity["port"]))]
    )
    return f"""same_server() {{
  [ -r /proc/{pid}/stat ] || return 1
  stat=$(cat /proc/{pid}/stat) || return 1
  rest=${{stat##*) }}
  set -- $rest
  [ "$1" != Z ] && [ "$1" != X ] || return 1
  shift 19
  [ "$1" = {shlex.quote(start)} ] || return 1
  argv=$(tr '\\000' '\\n' < /proc/{pid}/cmdline) || return 1
  [ "$argv" = {shlex.quote(argv)} ]
}}
if ! same_server; then echo PV_SERVER_NOT_OWNED_OR_EXITED; exit 0; fi
kill -TERM {pid} || {{ echo PV_SERVER_TERM_FAILED; exit 1; }}
i=0
while [ "$i" -lt 10 ]; do
  if ! same_server; then echo PV_SERVER_STOPPED_TERM; exit 0; fi
  sleep 0.2
  i=$((i+1))
done
if ! same_server; then echo PV_SERVER_STOPPED_TERM; exit 0; fi
kill -KILL {pid} || {{ echo PV_SERVER_KILL_FAILED; exit 1; }}
i=0
while [ "$i" -lt 10 ]; do
  if ! same_server; then echo PV_SERVER_STOPPED_KILL; exit 0; fi
  sleep 0.2
  i=$((i+1))
done
echo PV_SERVER_STILL_RUNNING
exit 1"""
