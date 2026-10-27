"""Bound historical storage without deleting live intent or recovery obligations."""

import json
import time
from datetime import UTC, datetime, timedelta

from .database import Database

TABLES = (
    "devices",
    "contents",
    "jobs",
    "commands",
    "activity",
    "edit_history",
    "team_directory",
    "content_status",
    "event_entitlements",
    "entitlement_migration_archive",
    "simulated_jobs",
    "simulated_cancellations",
    "executor_jobs",
    "executor_actions",
    "executor_devices",
    # Training evidence has independent, indefinite retention; prune never deletes it.
    "chooser_captures",
)


def counts(db):
    return {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in TABLES}


def policy(settings):
    return {
        "jobs_per_device": settings.job_history_limit,
        "receipts_per_device": settings.command_history_limit,
        "inactive_catalog_days": settings.catalog_retention_days,
        "interval_seconds": settings.maintenance_interval,
    }


def prune(database, settings, owner):
    with database.transaction() as db:
        if not database.lease(db, "maintenance", owner, time.time(), 60):
            return None
        devices = [database.device(db, row[0]) for row in db.execute("SELECT id FROM devices")]
        before = counts(db)
        cutoff = (datetime.now(UTC) - timedelta(days=settings.catalog_retention_days)).timestamp()
        # Undo is useful briefly, but must not pin old event snapshots forever.
        db.execute("DELETE FROM edit_history WHERE unixepoch(created_at)<?", (cutoff,))
        contents, requests, commands = set(), set(), set()
        for d in devices:
            contents.update(p["content_id"] for p in d["plan"])
            contents.add(d.get("desired"))
            observed = d.get("observed") or {}
            contents.add(observed.get("content_id"))
            requests.add(observed.get("request_id"))
        for row in db.execute("SELECT * FROM edit_history"):
            commands.add((row["device_id"], row["command_id"]))
            if row["undone_by"]:
                commands.add((row["device_id"], row["undone_by"]))
            before_edit = json.loads(row["before_payload"])
            after_edit = json.loads(row["after_payload"])
            before_completed = before_edit.get("manual_completions", {})
            after_completed = after_edit.get("manual_completions", {})
            # Whole-state undo snapshots copy older completion markers. Only
            # completions changed by this edit need their source event retained.
            contents.update(key for key in before_completed.keys() | after_completed.keys()
                            if before_completed.get(key) != after_completed.get(key))
            for payload in (before_edit, after_edit):
                contents.update(p["content_id"] for p in payload.get("plan", []))
        for row in db.execute("SELECT observation FROM simulated_devices WHERE observation IS NOT NULL"):
            observed = json.loads(row[0])
            requests.add(observed.get("request_id"))
            contents.add(observed.get("content_id"))
        for row in db.execute("SELECT request_id,content_id FROM executor_jobs WHERE request IS NOT NULL"):
            requests.add(row["request_id"])
            contents.add(row["content_id"])
        # Let a bounded catalogue request settle before removing its source row.
        contents.update(row[0] for row in db.execute(
            "SELECT content_id FROM event_entitlements WHERE state='checking' AND started_at>?",
            (time.time() - 180,),
        ))
        # A last request remains useful to the UI even after completion.
        requests.update(
            row[0]
            for row in db.execute(
                "SELECT id FROM jobs WHERE rowid IN (SELECT MAX(rowid) FROM jobs GROUP BY device_id)"
            )
        )
        contents.update(
            row[0]
            for row in db.execute(
                "SELECT content_id FROM jobs WHERE state='pending' OR (state!='verified' AND cancel_sent=0)"
            )
        )
        db.execute("CREATE TEMP TABLE keep_contents (id TEXT PRIMARY KEY)")
        db.execute("CREATE TEMP TABLE keep_requests (id TEXT PRIMARY KEY)")
        db.execute("CREATE TEMP TABLE keep_commands (device_id TEXT, id TEXT, PRIMARY KEY(device_id,id))")
        db.executemany("INSERT INTO keep_contents VALUES (?)", [(x,) for x in contents if x])
        db.executemany("INSERT INTO keep_requests VALUES (?)", [(x,) for x in requests if x])
        db.executemany("INSERT INTO keep_commands VALUES (?,?)", list(commands))
        # A lower simulator intent is permanently fenced even after its payload and
        # cancellation receipt are removed. Never prune the highest-intent evidence.
        retired = [
            r[0]
            for r in db.execute(
                "SELECT id FROM (SELECT j.*, ROW_NUMBER() OVER (PARTITION BY j.device_id ORDER BY j.rowid DESC) AS n "
                "FROM jobs j WHERE state!='pending') j WHERE n>? "
                "AND (state='verified' OR cancel_sent=1) "
                "AND id NOT IN (SELECT id FROM keep_requests) "
                "AND intent < (SELECT intent FROM simulated_devices WHERE device_id=j.device_id)",
                (settings.job_history_limit,),
            )
        ]
        db.executemany("DELETE FROM simulated_cancellations WHERE id=?", [(x,) for x in retired])
        db.executemany("DELETE FROM jobs WHERE id=?", [(x,) for x in retired])
        # Orphan simulator history can arise from late, already-fenced submissions.
        db.execute(
            "DELETE FROM simulated_cancellations WHERE id IN (SELECT s.id FROM simulated_jobs s "
            "WHERE s.id NOT IN (SELECT id FROM jobs) AND s.id NOT IN (SELECT id FROM keep_requests) "
            "AND s.intent < (SELECT intent FROM simulated_devices WHERE device_id=s.device_id))"
        )
        db.execute(
            "DELETE FROM simulated_jobs AS s WHERE id NOT IN (SELECT id FROM jobs) "
            "AND id NOT IN (SELECT id FROM keep_requests) "
            "AND intent < (SELECT intent FROM simulated_devices WHERE device_id=s.device_id)"
        )
        db.execute(
            "DELETE FROM commands WHERE rowid IN (SELECT rid FROM (SELECT rowid AS rid, device_id, id, "
            "ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY rowid DESC) AS n FROM commands) "
            "WHERE n>? AND (device_id,id) NOT IN (SELECT device_id,id FROM keep_commands))",
            (settings.command_history_limit,),
        )
        db.execute(
            "DELETE FROM contents WHERE active=0 AND unixepoch(seen_at)<? "
            "AND id NOT IN (SELECT id FROM keep_contents)",
            (cutoff,),
        )
        db.execute(
            "DELETE FROM content_status WHERE content_id NOT IN (SELECT id FROM contents) "
            "AND content_id NOT IN (SELECT id FROM keep_contents)"
        )
        # Deduplicate reimports only for the bounded catalogue retention window.
        db.execute("DELETE FROM event_entitlements WHERE content_id NOT IN (SELECT id FROM contents) "
                   "AND content_id NOT IN (SELECT id FROM keep_contents)")
        db.execute("DELETE FROM entitlement_migration_archive WHERE content_id NOT IN (SELECT id FROM contents) "
                   "AND content_id NOT IN (SELECT id FROM keep_contents)")
        known = {r[0] for r in db.execute("SELECT id FROM contents")}
        for row in db.execute("SELECT id,before_payload,after_payload FROM edit_history").fetchall():
            payloads = []
            for field in ("before_payload", "after_payload"):
                payload = json.loads(row[field])
                if "manual_completions" in payload:
                    payload["manual_completions"] = {
                        key: value for key, value in payload["manual_completions"].items() if key in known
                    }
                payloads.append(json.dumps(payload))
            db.execute("UPDATE edit_history SET before_payload=?,after_payload=? WHERE id=?",
                       (*payloads, row["id"]))
        for d in devices:
            d["failures"] = {key: value for key, value in d["failures"].items() if key in known}
            d["manual_completions"] = {
                key: value for key, value in d.get("manual_completions", {}).items() if key in known
            }
            database.save_device(db, d)
        after = counts(db)
        result = {
            "state": "ok",
            "last_run": datetime.now(UTC).isoformat(),
            "policy": policy(settings),
            "removed": {name: before[name] - after[name] for name in TABLES},
            "counts": after,
        }
        Database.set_meta(db, "maintenance_health", result)
        return result
