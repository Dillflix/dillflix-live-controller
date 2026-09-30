"""Consistent local backups and offline restore. Run: python -m controller.ops --help."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path

from .database import Database
from .maintenance import TABLES, counts
from .storage_lock import database_guard

JSON_COLUMNS = {
    "metadata": ("value",),
    "devices": ("payload",),
    "contents": ("snapshot",),
    "jobs": ("payload",),
    "commands": ("result",),
    "team_directory": ("payload",),
    "edit_history": ("before_payload", "after_payload"),
    "content_status": ("observation",),
    "simulated_jobs": ("payload", "observation"),
    "simulated_devices": ("observation",),
    "executor_jobs": ("request", "report", "completion_candidate"),
}


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def verify(path):
    """Read without migrating or changing the source database."""
    path = Path(path).resolve(strict=True)
    with closing(readonly(path)) as db:
        if [r[0] for r in db.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("SQLite integrity check failed")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != Database.SCHEMA_VERSION:
            raise ValueError(f"Expected database schema {Database.SCHEMA_VERSION}, found {version}")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(TABLES) | {"metadata", "leases", "simulated_devices"} <= tables:
            raise ValueError("This is not a complete controller database")
        for table, columns in JSON_COLUMNS.items():
            for column in columns:
                if db.execute(
                    f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL AND NOT json_valid({column}) LIMIT 1"
                ).fetchone():
                    raise ValueError(f"Invalid JSON in {table}.{column}")
        mode = Database.meta(db, "mode")
        if mode not in {"demo", "teamarr"}:
            raise ValueError("Controller mode is missing or invalid")
        for row in db.execute("SELECT id FROM devices"):
            device = Database.device(db, row[0])
            if (
                not {"plan", "rules", "team_ranks", "preferences", "intent_version", "automation"}
                <= device.keys()
            ):
                raise ValueError("Device configuration is incomplete")
            for entry in device["plan"]:
                if not db.execute("SELECT 1 FROM contents WHERE id=?", (entry["content_id"],)).fetchone():
                    raise ValueError("A watch-plan entry has no retained catalog snapshot")
        result = {"schema_version": version, "mode": mode, "counts": counts(db)}
    return {"path": str(path), "bytes": path.stat().st_size, **result}


def snapshot(source, destination):
    started = time.monotonic()

    def progress(_status, _remaining, _total):
        if time.monotonic() - started > 60:
            raise TimeoutError("Database snapshot exceeded 60 seconds; retry when writes are quieter")

    with closing(readonly(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst, pages=128, progress=progress, sleep=0.05)
        dst.execute("PRAGMA journal_mode=DELETE")
    return verify(destination)


def temporary(parent):
    fd, name = tempfile.mkstemp(prefix=".controller-", suffix=".sqlite3", dir=parent)
    os.close(fd)
    return Path(name)


def sync_file(path):
    with open(path, "rb") as file:
        os.fsync(file.fileno())


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_backup(source, destination):
    """Caller holds the source guard; publish without replacing an existing file."""
    destination = Path(destination).absolute()
    if os.path.lexists(destination):
        raise FileExistsError(f"Backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = temporary(destination.parent)
    try:
        report = snapshot(source, stage)
        sync_file(stage)
        os.link(stage, destination)
        sync_directory(destination.parent)
        with destination.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        return {**report, "path": str(destination), "sha256": digest}
    finally:
        stage.unlink(missing_ok=True)


def backup(source, destination):
    source = Path(source).resolve(strict=True)
    with database_guard(source):
        return publish_backup(source, destination)


def fence_values(path):
    values = {}
    with closing(readonly(path)) as db:
        for row in db.execute("SELECT id FROM devices"):
            d = Database.device(db, row[0])
            highest = db.execute(
                "SELECT intent FROM simulated_devices WHERE device_id=?", (d["id"],)
            ).fetchone()
            real = db.execute(
                "SELECT MAX(highest_intent,cancelled_through) FROM executor_devices WHERE device_id=?",
                (d["id"],),
            ).fetchone()
            values[d["id"]] = (
                d["revision"],
                max(d["intent_version"], highest[0] if highest else -1, real[0] if real else -1),
            )
    return values


def prepare_restore(path, prior):
    """Keep user data and receipts; discard runtime ownership and playback claims."""
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute("SELECT id FROM devices").fetchall():
            d = Database.device(db, row[0])
            revision, intent = prior.get(d["id"], (-1, -1))
            saved_highest = db.execute(
                "SELECT intent FROM simulated_devices WHERE device_id=?", (d["id"],)
            ).fetchone()
            real = db.execute(
                "SELECT MAX(highest_intent,cancelled_through) FROM executor_devices WHERE device_id=?",
                (d["id"],),
            ).fetchone()
            d.update(
                revision=max(d["revision"], revision) + 1,
                intent_version=max(
                    d["intent_version"],
                    intent,
                    saved_highest[0] if saved_highest else -1,
                    real[0] if real else -1,
                )
                + 1,
                automation="paused",
                manual_control=None,
                desired=None,
                observed=None,
                playback_state="waiting",
                reason="Database restored. Review your watch plan and resume when ready.",
                started_at=None,
                last_switch_at=None,
                next_candidate=None,
                recovery=None,
                executor_health={"state": "starting"},
                retry_playback=None,
                force_switch=True,
                failures={},
            )
            Database.set_meta(db, f"manual-owner:{d['id']}", None)
            d["input_handoff"] = (
                {"through_intent_version": d["intent_version"]}
                if Database.meta(db, "playback_adapter") == "prime-video"
                else None
            )
            Database.save_device(db, d)
            db.execute(
                "UPDATE executor_devices SET highest_intent=MAX(highest_intent,?),cancelled_through=MAX(cancelled_through,?) WHERE device_id=?",
                (d["intent_version"], d["intent_version"], d["id"]),
            )
            db.execute(
                "INSERT INTO simulated_devices VALUES (?,?,NULL) ON CONFLICT(device_id) DO UPDATE SET intent=excluded.intent,observation=NULL",
                (d["id"], d["intent_version"]),
            )
            Database.log(
                db,
                datetime.now(UTC).isoformat(),
                "Database restored",
                "Automation paused; watch plan and configuration restored",
                "maintenance",
                d["id"],
            )
        db.execute("UPDATE jobs SET state='cancelled' WHERE state='pending'")
        db.execute("UPDATE simulated_jobs SET state='cancelled' WHERE state IN ('accepted','navigating')")
        db.execute(
            "UPDATE executor_jobs SET cancel_requested=1,next_check=0 WHERE request IS NOT NULL AND cancel_requested!=2"
        )
        db.execute("DELETE FROM leases")
        db.execute("UPDATE content_status SET request_id=?,next_check=0", (str(uuid.uuid4()),))
        Database.set_meta(db, "simulated_executor_outage", False)
        Database.set_meta(db, "simulated_executor_disconnect_at", None)
        Database.set_meta(db, "maintenance_health", {"state": "starting"})
        Database.set_meta(db, "last_restore", {"at": datetime.now(UTC).isoformat(), "automation": "paused"})
        db.commit()


def retain_executor_history(stage, current):
    """A user-data restore must not roll back tokens, tombstones, or TV ownership.

    The current database is stopped and guarded by restore(). Its newer executor
    records are needed to stop playback even when the backup predates that token.
    """
    with closing(readonly(current)) as src, closing(sqlite3.connect(stage)) as dst:
        for table in ("executor_jobs", "executor_devices"):
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            for row in rows:
                columns = ",".join(row.keys())
                placeholders = ",".join("?" for _ in row.keys())
                dst.execute(f"INSERT OR REPLACE INTO {table}({columns}) VALUES ({placeholders})", tuple(row))
        for job in src.execute("SELECT token FROM executor_jobs"):
            dst.execute("DELETE FROM executor_actions WHERE token=?", (job[0],))
            for action in src.execute("SELECT * FROM executor_actions WHERE token=? ORDER BY id", (job[0],)):
                columns = [column for column in action.keys() if column != "id"]
                dst.execute(
                    "INSERT INTO executor_actions("
                    + ",".join(columns)
                    + ") VALUES ("
                    + ",".join("?" for _ in columns)
                    + ")",
                    [action[column] for column in columns],
                )
        dst.commit()


def restore(source, target, *, replace=False, expected_mode=None):
    source = Path(source).resolve(strict=True)
    target = Path(target).resolve()
    if source == target or (target.exists() and os.path.samefile(source, target)):
        raise ValueError("Restore source and target must be different files")
    with database_guard(target, exclusive=True):
        info = verify(source)
        if expected_mode and info["mode"] != expected_mode:
            raise ValueError(f"Backup mode is {info['mode']}; target mode is {expected_mode}")
        if target.exists() and not replace:
            raise FileExistsError("Target exists; use --replace to replace it and save a rollback backup")
        prior = {}
        if target.exists():
            current = verify(target)
            if current["mode"] != info["mode"]:
                raise ValueError("Source and target modes differ; use separate databases")
            with closing(readonly(target)) as db:
                if db.execute("SELECT 1 FROM leases WHERE expires>? LIMIT 1", (time.time(),)).fetchone():
                    raise RuntimeError(
                        "Controller leases are still active. Stop it and wait for lease expiry."
                    )
            prior = fence_values(target)
        stage = temporary(target.parent)
        rollback = None
        try:
            staged_info = snapshot(source, stage)
            if staged_info["mode"] != info["mode"]:
                raise ValueError("Source mode changed during restore; retry from a stable backup")
            if target.exists():
                retain_executor_history(stage, target)
            prepare_restore(stage, prior)
            verify(stage)
            sync_file(stage)
            if target.exists():
                suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
                rollback = str(target) + ".before-restore-" + suffix + ".sqlite3"
                publish_backup(target, rollback)
                # Retire WAL before replacing its main file. Cooperative clients are
                # excluded by the guard; SQLite busy results also stop replacement.
                with closing(sqlite3.connect(target, timeout=1)) as db:
                    if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                        raise RuntimeError("Database has active SQLite clients; restore was not applied")
                    db.execute("PRAGMA journal_mode=DELETE")
            elif any(Path(str(target) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise RuntimeError("Target has orphan SQLite sidecars; recover that database before restore")
            if replace:
                os.replace(stage, target)
            else:
                os.link(stage, target)  # Do not clobber an unexpectedly created target.
            sync_directory(target.parent)
            return {**verify(target), "rollback_backup": rollback, "automation": "paused"}
        finally:
            stage.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=os.getenv("CONTROLLER_DATABASE", "data/controller.sqlite3"))
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("backup", help="Create a consistent snapshot while the controller runs").add_argument(
        "path"
    )
    commands.add_parser(
        "verify", help="Check integrity, schema, and configuration without modifying a backup"
    ).add_argument("path")
    restore_parser = commands.add_parser("restore", help="Restore offline, with automation paused")
    restore_parser.add_argument("path", help="Backup file, or - to read SQLite bytes from standard input")
    restore_parser.add_argument(
        "--replace", action="store_true", help="Replace existing database after saving a rollback backup"
    )
    restore_parser.add_argument("--mode", choices=("demo", "teamarr"), default=os.getenv("CONTROLLER_MODE"))
    args = parser.parse_args()
    try:
        if args.operation == "backup":
            result = backup(args.database, args.path)
        elif args.operation == "verify":
            result = verify(args.path)
        else:
            with ExitStack() as stack:
                source = args.path
                if source == "-":
                    directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="dillflix-restore-"))
                    source = Path(directory) / "input.sqlite3"
                    with source.open("wb") as file:
                        shutil.copyfileobj(sys.stdin.buffer, file)
                    source.chmod(0o600)
                result = restore(source, args.database, replace=args.replace, expected_mode=args.mode)
        print(json.dumps(result, indent=2))
    except (OSError, sqlite3.Error, ValueError, RuntimeError, TimeoutError) as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
