"""Bounded, restart-persistent evidence; no device or network operations.

Keep this small wire-format helper compatible with Python 3.10 and the player's
copy. Producers never wait on disk. Export reports queue loss and read failures.
"""

import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SOURCES = ("rpc", "runtime", "device", "resolver")
FILE_BYTES = 4 * 1024 * 1024
RECORD_BYTES = 256 * 1024
EXPORT_BYTES = 1024 * 1024


def scrub(value):
    if isinstance(value, dict):
        return {
            str(k): "[redacted]"
            if any(
                s in str(k).lower()
                for s in ("password", "secret", "authorization", "api_key", "access_token", "cookie")
            )
            else scrub(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        for name, secret in os.environ.items():
            if len(secret) >= 8 and any(
                s in name.lower() for s in ("api_key", "token", "password", "secret")
            ):
                value = value.replace(secret, "[redacted]")
        value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[redacted]", value)
        value = re.sub(
            r"(?i)([?&](?:token|key|signature|credential|auth)=[^\s&\"']+)", "&credential=[redacted]", value
        )
    return value


def request_body(method, params):
    params = dict(params)
    if method == "manual_input" and "text" in params:
        params["text"] = "[manual text omitted]"
    return {"method": method, "params": params}


def read_logs(directory, *, limit=EXPORT_BYTES):
    """Read the shared directory even when its owning process/socket is gone."""
    directory = Path(directory)
    result = {
        "schema_version": 1,
        "directory": str(directory),
        "sources": {},
        "retention": {
            "files_per_source": 3,
            "file_bytes": FILE_BYTES,
            "record_bytes": RECORD_BYTES,
            "export_bytes_per_source": limit,
        },
    }
    try:
        result["capture_status"] = json.loads((directory / "status.json").read_text())
    except (OSError, ValueError) as exc:
        result["capture_status"] = {"state": "unavailable", "error": str(exc)}
    for source in SOURCES:
        records, errors, consumed, omitted = [], [], 0, False
        # Newest first, then restore chronological order. A concurrent rotation
        # can move files; report errors/duplicates rather than imply atomicity.
        seen = set()
        for suffix in ("", ".1", ".2"):
            path = directory / (source + ".jsonl" + suffix)
            try:
                with path.open("rb") as handle:
                    size = os.fstat(handle.fileno()).st_size
                    room = max(0, limit - consumed)
                    offset = max(0, size - room)
                    handle.seek(offset)
                    raw = handle.read(room)
                consumed += len(raw)
                if offset:
                    omitted = True
                    raw = raw.partition(b"\n")[2]
                for line in reversed(raw.splitlines()):
                    try:
                        record = json.loads(line)
                        identity = record.get("record_id")
                        if identity not in seen:
                            records.append(record)
                            seen.add(identity)
                    except (ValueError, AttributeError):
                        errors.append("partial or invalid record in " + path.name)
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append(str(exc))
        result["sources"][source] = {
            "state": "partial" if errors else "available" if records else "missing",
            "records": list(reversed(records)),
            "errors": errors,
            "export_truncated": omitted,
            "read_bytes": consumed,
        }
    return result


class Recorder:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.queue = queue.Queue(maxsize=256)
        self.stopping = threading.Event()
        self.process_id = uuid.uuid4().hex
        self.dropped = 0
        self.error = None
        self.thread = threading.Thread(target=self._run, name="diagnostic-writer", daemon=True)
        self.thread.start()
        self.record("runtime", "capture_started", pid=os.getpid())

    def record(self, source, kind, **data):
        try:
            self._record(source, kind, **data)
        except Exception as exc:
            # Diagnostic failures must never fail a device operation.
            self.dropped += 1
            self.error = type(exc).__name__ + ": " + str(exc)

    def _record(self, source, kind, **data):
        if source not in SOURCES:
            return
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "record_id": uuid.uuid4().hex,
            "process_id": self.process_id,
            "source": source,
            "kind": kind,
            "data": scrub(data),
        }
        wire = json.dumps(record, default=str, ensure_ascii=True)
        if len(wire) > RECORD_BYTES:
            record["data"] = {
                **{
                    k: v
                    for k, v in record["data"].items()
                    if k
                    in {
                        "call_id",
                        "token",
                        "request_id",
                        "session_id",
                        "method",
                        "phase",
                        "http_status",
                        "duration_seconds",
                    }
                    and isinstance(v, (str, int, float))
                },
                "truncated": True,
                "original_bytes": len(wire),
                "prefix": wire[: RECORD_BYTES // 4],
            }
            wire = json.dumps(record, ensure_ascii=True)
        try:
            self.queue.put_nowait((source, wire + "\n"))
        except queue.Full:
            self.dropped += 1

    def _status(self):
        return {
            "state": "error" if self.error else "stopped" if self.stopping.is_set() else "running",
            "at": datetime.now(timezone.utc).isoformat(),
            "process_id": self.process_id,
            "dropped_records": self.dropped,
            "pending_records": self.queue.qsize(),
            "writer_alive": self.thread.is_alive(),
            "error": self.error,
            "durability": "flushed to OS per record; queued records may be lost on process crash",
        }

    def _write_status(self):
        temp = self.directory / "status.tmp"
        temp.write_text(json.dumps(self._status()))
        temp.chmod(0o640)
        temp.replace(self.directory / "status.json")

    def _run(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o750)
            self._write_status()
        except OSError as exc:
            self.error = str(exc)
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                source, wire = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                path = self.directory / (source + ".jsonl")
                if path.exists() and path.stat().st_size + len(wire) > FILE_BYTES:
                    for old, new in ((".1", ".2"), ("", ".1")):
                        prior = self.directory / (source + ".jsonl" + old)
                        if prior.exists():
                            prior.replace(self.directory / (source + ".jsonl" + new))
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
                with os.fdopen(fd, "a") as handle:
                    handle.write(wire)
                self._write_status()
            except OSError as exc:
                self.error = str(exc)
                self.dropped += 1
            finally:
                self.queue.task_done()
        try:
            self._write_status()
        except OSError as exc:
            self.error = str(exc)

    def snapshot(self):
        deadline = time.monotonic() + 1
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        result = read_logs(self.directory)
        result["capture_status"] = self._status()
        return result

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=2)


class LogHandler(logging.Handler):
    def __init__(self, recorder):
        super().__init__(logging.INFO)
        self.recorder = recorder

    def emit(self, record):
        try:
            if hasattr(record, "diagnostic_rpc"):
                self.recorder.record("rpc", "player_rpc", **record.diagnostic_rpc)
                return
            self.recorder.record(
                "runtime",
                "python_log",
                logger=record.name,
                level=record.levelname,
                message=self.format(record),
            )
        except Exception:
            self.handleError(record)
