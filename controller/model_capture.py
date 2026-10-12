"""Durable local chooser evidence and read-only JSONL exports for later review."""

import argparse
import json
import logging
import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)
MAX_CAPTURE_BYTES = 4 * 1024 * 1024


def connection(path, *, readonly=False):
    # Never create a second, empty database if the configured path is wrong.
    uri = Path(path).resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw")
    return sqlite3.connect(uri, uri=True, timeout=0.1)


class ModelCaptureStore:
    def __init__(self, database_path):
        self.path = str(database_path)
        self.last_error = None
        self.failure_count = 0

    def record(self, trace):
        """Fail open: capture must never change or prevent a playback decision."""
        try:
            from .storage_lock import database_guard

            call_id = trace.get("call_id")
            if not isinstance(call_id, str) or not call_id or len(call_id) > 128:
                raise ValueError("Invalid capture identity")
            with database_guard(self.path), closing(connection(self.path)) as db:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute(
                    "SELECT payload FROM chooser_captures WHERE call_id=?", (call_id,)
                ).fetchone()
                value = {**(json.loads(previous[0]) if previous else {}), **trace, "schema_version": 1}
                recorded_at = datetime.now(UTC).isoformat()
                value["recorded_at"] = recorded_at
                payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                if len(payload.encode("utf-8")) > MAX_CAPTURE_BYTES:
                    raise ValueError("Capture exceeds its storage bound")
                db.execute(
                    "INSERT INTO chooser_captures VALUES (?,?,?) ON CONFLICT(call_id) DO UPDATE SET "
                    "recorded_at=excluded.recorded_at,payload=excluded.payload",
                    (call_id, recorded_at, payload),
                )
                db.commit()
            return True
        except Exception as exc:
            self.failure_count += 1
            self.last_error = {
                "at": datetime.now(UTC).isoformat(),
                "code": type(exc).__name__,
                "message": "Chooser capture could not be saved; playback continues",
            }
            # Exception messages can contain upstream data or SQL payloads.
            log.warning("Chooser capture could not be saved (%s); playback continues", type(exc).__name__)
            return False

    def status(self):
        result = {
            "state": "ok",
            "retention": "indefinite",
            "failure_count_since_start": self.failure_count,
            "last_error": self.last_error,
        }
        try:
            with closing(connection(self.path, readonly=True)) as db:
                row = db.execute("SELECT COUNT(*),MAX(recorded_at) FROM chooser_captures").fetchone()
                result.update(count=row[0], last_recorded_at=row[1])
        except Exception as exc:
            result.update(state="error", count=None, read_error=type(exc).__name__)
        if self.last_error and self.last_error["at"] >= (result.get("last_recorded_at") or ""):
            result["state"] = "error"
        return result


def message_seed(record):
    """Validated examples are review seeds, never claims of correct ground truth."""
    if record.get("state") != "returned" or (record.get("validation") or {}).get("state") != "passed":
        return None
    request, response = record.get("request") or {}, record.get("response") or {}
    if request.get("truncated") is not False or response.get("truncated") is not False:
        return None
    if request.get("redacted") or response.get("redacted"):
        return None
    body, answer = request.get("json"), response.get("json")
    if not isinstance(body, dict) or not isinstance(answer, dict):
        return None
    messages, choices = body.get("messages"), answer.get("choices")
    if not isinstance(messages, list) or not messages or not all(
        isinstance(m, dict) and isinstance(m.get("role"), str) and isinstance(m.get("content"), str)
        for m in messages
    ):
        return None
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return None
    choice = choices[0]
    assistant = choice.get("message")
    if (
        choice.get("finish_reason") != "stop"
        or not isinstance(assistant, dict)
        or assistant.get("role") != "assistant"
        or not isinstance(assistant.get("content"), str)
        or not assistant["content"]
        or len(assistant["content"]) > 32768
    ):
        return None
    return {
        "schema_version": 1,
        "call_id": record["call_id"],
        "messages": [*messages, {"role": "assistant", "content": assistant["content"]}],
        "metadata": {
            **{k: v for k, v in record.items() if k not in {"request", "response"}},
            "review_status": "unreviewed",
            "request": {k: v for k, v in body.items() if k != "messages"},
            "response": {k: v for k, v in answer.items() if k != "choices"},
            "request_capture": {k: v for k, v in request.items() if k != "json"},
            "response_capture": {k: v for k, v in response.items() if k != "json"},
        },
    }


def export_records(path, output, *, format="raw"):
    """Stream one consistent SQLite read snapshot without migration or mutation."""
    if format not in {"raw", "messages"}:
        raise ValueError("Unsupported export format")
    count = 0
    with closing(connection(path, readonly=True)) as db:
        db.execute("BEGIN")
        for row in db.execute("SELECT payload FROM chooser_captures ORDER BY rowid"):
            value = json.loads(row[0])
            if format == "messages":
                value = message_seed(value)
            if value is not None:
                output.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
                count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=os.getenv("CONTROLLER_DATABASE", "data/controller.sqlite3"))
    commands = parser.add_subparsers(dest="operation", required=True)
    export = commands.add_parser("export", help="Export retained chooser calls as JSONL")
    export.add_argument("--format", choices=("raw", "messages"), default="raw")
    commands.add_parser("status", help="Read capture counts and last recorded time")
    args = parser.parse_args()
    try:
        if args.operation == "export":
            export_records(args.database, sys.stdout, format=args.format)
        else:
            print(json.dumps(ModelCaptureStore(args.database).status(), indent=2))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Chooser capture command failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
