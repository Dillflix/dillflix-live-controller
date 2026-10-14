"""Local Unix-socket API. Disconnecting a client never closes the device backend."""

import errno
import fcntl
import json
import logging
import uuid

from .diagnostic_log import request_body
import os
import socket
import socketserver
import stat
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .errors import PrimeError

UTC = timezone.utc


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, *args, **kwargs):
        self.events = deque(maxlen=1000)
        self.pending = {}
        self.event_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def diagnostics(self):
        with self.event_lock:
            events = list(self.events)
            pending = [
                {k: v for k, v in event.items() if k != "started"}
                | {"elapsed_seconds": round(time.monotonic() - event["started"], 3)}
                for event in self.pending.values()
            ]
        return {
            "schema_version": 1,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "rpc_events": events,
            "inflight": pending,
            "limit": 1000,
            "retention": "current service process only",
            "capture_policy": {
                "frida_callback_persistence": "disabled",
                "continuous_device_logcat": "disabled",
                "periodic_device_activity": "disabled",
                "rpc_bodies": "enabled",
                "host_service_logs": "enabled",
                "note": "Retained runtime/device files may contain historical records from earlier versions.",
            },
            "persistent": self.recorder.snapshot() if getattr(self, "recorder", None) else {"state": "not_configured"},
            "excluded": ["credentials", "manual input text", "unrelated host journal"],
        }

    def service_actions(self):
        check = getattr(self, "failure_check", None)
        if check:
            failure = check()
            if failure:
                raise RuntimeError("Runtime unavailable: " + str(failure))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Queries and IDs are not written to HTTP access logs.

    def do_POST(self):
        response = {}
        status = 200
        started = time.monotonic()
        method = None
        params = {}
        allowed = set()
        call_id = self.headers.get("X-Dillflix-Call-Id", "")
        if not (len(call_id) == 32 and all(c in "0123456789abcdef" for c in call_id)):
            call_id = uuid.uuid4().hex
        recorder = getattr(self.server, "recorder", None)
        try:
            if self.path != "/rpc":
                raise ValueError("unknown API path")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536:
                raise ValueError("request must be 1..65536 bytes")
            self.connection.settimeout(5)
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            method = request["method"]
            params = request.get("params", {})
            if recorder and method not in {"diagnostics", "inspect_page", "resolution_evidence"} and isinstance(params, dict):
                recorder.record("rpc", "request", call_id=call_id, request=request_body(method, params))
            allowed = {
                "health",
                "diagnostics",
                "compatibility",
                "cancel",
                "stop",
                "manual_input",
                "search",
                "resolve",
                "broadcasts",
                "inspect_page",
                "resolution_evidence",
                "pages",
                "discover",
                "play",
                "attempt",
                "suspend",
                "playback_status",
                "vpn_locations", "vpn_status", "vpn_connect", "vpn_disconnect", "vpn_cancel",
            }
            if method not in allowed or not isinstance(params, dict):
                raise ValueError("unsupported method or parameters")
            if method in (
                "search",
                "resolve",
                "broadcasts",
                "inspect_page",
                "pages",
                "discover",
                "play",
                "cancel",
                "stop",
                "vpn_connect", "vpn_disconnect", "vpn_cancel",
            ) and not isinstance(params.get("ownership"), dict):
                raise ValueError("SDK ownership envelope required; upgrade the client")
            if method not in {"health", "diagnostics", "compatibility"}:
                with self.server.event_lock:
                    self.server.pending[threading.get_ident()] = {
                        "started": started,
                        "at": datetime.now(timezone.utc).isoformat(),
                        "method": method,
                        "attempt_id": str(params.get("attempt_id", ""))[:128],
                    }
            response = {
                "result": self.server.diagnostics()
                if method == "diagnostics" and not params
                else getattr(self.server.controller, method)(**params)
            }
        except (PrimeError, ValueError, TypeError, KeyError, TimeoutError) as exc:
            status = 409
            response = {"error": {"type": type(exc).__name__, "message": str(exc)}}
        except Exception:
            logging.getLogger(__name__).exception("Unhandled player RPC failure: %s", method)
            status = 500
            response = {
                "error": {
                    "type": "PrimeError",
                    "message": "internal service error; do not automatically replay the request",
                }
            }
        if (
            isinstance(method, str)
            and method in allowed
            and method not in {"health", "diagnostics", "compatibility"}
        ):
            params = params if isinstance(params, dict) else {}
            result = response.get("result")
            result = result if isinstance(result, dict) else {}
            event = {
                "at": datetime.now(timezone.utc).isoformat(),
                "method": method,
                "duration_seconds": round(time.monotonic() - started, 3),
                "http_status": status,
                "error_type": response.get("error", {}).get("type"),
                "attempt_id": str(params.get("attempt_id", ""))[:128],
                "session_id": str(params.get("session_id", ""))[:128],
                "state": result.get("state"),
                "stopped": result.get("stopped"),
                "acknowledged": result.get("acknowledged"),
            }
            with self.server.event_lock:
                self.server.pending.pop(threading.get_ident(), None)
                self.server.events.append(event)
        # On-demand evidence is returned to its caller, not copied into persistent
        # RPC logs. Keep the compact duration/status event above for diagnostics.
        if recorder and method not in {"diagnostics", "inspect_page", "resolution_evidence"}:
            recorder.record("rpc", "response", call_id=call_id, method=method, response=response,
                            http_status=status, duration_seconds=round(time.monotonic() - started, 3))
        wire = json.dumps(response, allow_nan=False).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)
        except (BrokenPipeError, ConnectionResetError):
            pass


class LocalService:
    def __init__(self, controller, path, *, shared=False, recover_stale=False):
        self.path = Path(path)
        self.controller = controller
        self.server = None
        self.shared, self.recover_stale = shared, recover_stale
        self.lock = None
        self.identity = None

    def start(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            # Held for the full service lifetime, including runtime startup.
            fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            self.lock = os.fdopen(fd, "a")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FileExistsError("another service owns this socket lock") from exc
            if os.path.lexists(self.path):
                existing = self.path.lstat()
                if (
                    not self.recover_stale
                    or not stat.S_ISSOCK(existing.st_mode)
                    or existing.st_uid != os.getuid()
                ):
                    raise FileExistsError("socket already exists; do not replace a potentially live service")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1)
                    try:
                        probe.connect(str(self.path))
                    except OSError as exc:
                        if exc.errno != errno.ECONNREFUSED:
                            raise
                    else:
                        raise FileExistsError("another live service owns this socket")
                if self.path.lstat() != existing:
                    raise FileExistsError("socket changed during recovery")
                self.path.unlink()
            old = os.umask(0o077)
            try:
                self.server = Server(str(self.path), Handler)
                self.server.controller = self.controller
            finally:
                os.umask(old)
            current = self.path.lstat()
            self.identity = (current.st_dev, current.st_ino)
            # Primary group is selected by systemd; each replacement gets access.
            os.chmod(self.path, 0o660 if self.shared else 0o600)
        except BaseException:
            self.close()
            raise
        return self

    def serve_forever(self, failure_check=None):
        self.server.failure_check = failure_check
        self.server.serve_forever(poll_interval=0.25)

    def close(self):
        if self.server:
            self.server.server_close()
            self.server = None
        if self.identity and os.path.lexists(self.path):
            current = self.path.lstat()
            if (current.st_dev, current.st_ino) == self.identity:
                self.path.unlink()
        self.identity = None
        if self.lock:
            self.lock.close()
            self.lock = None
