import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .leagues import DEFAULT_LEAGUES
from .storage_lock import database_guard


def encode(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class Database:
    # Older releases must not ignore completion, discovery, or source priority.
    SCHEMA_VERSION = 9

    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with database_guard(path):
            self.initialize(path)

    def initialize(self, path):
        db = sqlite3.connect(path, timeout=10, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > self.SCHEMA_VERSION:
                raise ValueError("This database requires a newer controller version")
            if version == 0:
                schema = """
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS contents (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, active INTEGER NOT NULL,
                    seen_at TEXT NOT NULL, snapshot TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL, intent INTEGER NOT NULL,
                    content_id TEXT NOT NULL, state TEXT NOT NULL, ready_at REAL NOT NULL,
                    payload TEXT NOT NULL, error TEXT);
                CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(state,device_id);
                CREATE TABLE IF NOT EXISTS commands (
                    device_id TEXT NOT NULL, id TEXT NOT NULL, body_hash TEXT NOT NULL,
                    result TEXT NOT NULL, PRIMARY KEY(device_id,id));
                CREATE TABLE IF NOT EXISTS activity (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
                    at TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leases (
                    resource TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
                """
                for statement in schema.split(";"):
                    if statement.strip():
                        db.execute(statement)
            if version < 2:
                db.execute("""CREATE TABLE IF NOT EXISTS team_directory (
                    key TEXT PRIMARY KEY, league TEXT NOT NULL,
                    payload TEXT NOT NULL, seen_at TEXT NOT NULL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS edit_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
                    command_id TEXT NOT NULL, description TEXT NOT NULL, created_at TEXT NOT NULL,
                    before_payload TEXT NOT NULL, after_payload TEXT NOT NULL, undone_by TEXT)""")
                db.execute("CREATE INDEX IF NOT EXISTS history_device ON edit_history(device_id,id)")
            if version < 3:
                # Column checks also tolerate a version-1 recovery database with later additive columns.
                columns = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
                for name, sql_type in {
                    "deadline_at": "REAL",
                    "executor_job_id": "TEXT",
                    "progress": "TEXT",
                    "cancel_sent": "INTEGER NOT NULL DEFAULT 0",
                    "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
                }.items():
                    if name not in columns:
                        db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}")
                db.execute("""CREATE TABLE IF NOT EXISTS simulated_jobs (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL, intent INTEGER NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL, submitted_at REAL NOT NULL,
                    observation TEXT, error TEXT)""")
                db.execute("""CREATE TABLE IF NOT EXISTS simulated_devices (
                    device_id TEXT PRIMARY KEY, intent INTEGER NOT NULL, observation TEXT)""")
                db.execute("CREATE TABLE IF NOT EXISTS simulated_cancellations (id TEXT PRIMARY KEY)")
                # The old inline simulator already recorded verified playback. Adopt that
                # simulator state without changing the user's device/configuration records.
                for device_id, payload in db.execute("SELECT id,payload FROM devices").fetchall():
                    device = json.loads(payload)
                    observation = device.get("observed")
                    if not observation or not observation.get("simulated"):
                        continue
                    job = db.execute(
                        "SELECT id,intent,payload FROM jobs WHERE id=?", (observation.get("request_id"),)
                    ).fetchone()
                    if job is None:
                        continue
                    observation = {**observation, "device_id": device_id, "health": "healthy"}
                    db.execute(
                        "INSERT OR IGNORE INTO simulated_jobs(id,device_id,intent,payload,state,submitted_at,observation) VALUES (?,?,?,?,'playing_verified',0,?)",
                        (job[0], device_id, job[1], job[2], encode(observation)),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO simulated_devices VALUES (?,?,?)",
                        (device_id, device["intent_version"], encode(observation)),
                    )
            if version < 4:
                db.execute("""CREATE TABLE IF NOT EXISTS content_status (
                    content_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                    observation TEXT, last_attempt TEXT, last_success TEXT,
                    error TEXT, failures INTEGER NOT NULL DEFAULT 0,
                    next_check REAL NOT NULL DEFAULT 0)""")
            # The deployed 0.8.1 hotfix used schema 6 without executor tables.
            # Ensure both schema-6 variants upgrade safely, preserving all records.
            if version < 7:
                db.execute("""CREATE TABLE IF NOT EXISTS executor_jobs (
                    token TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    device_id TEXT NOT NULL, intent INTEGER NOT NULL, content_id TEXT NOT NULL,
                    request TEXT, request_hash TEXT NOT NULL, report TEXT NOT NULL,
                    state TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    touched_device INTEGER NOT NULL DEFAULT 0, retired_at REAL,
                    next_check REAL NOT NULL DEFAULT 0, completion_candidate TEXT,
                    UNIQUE(device_id,intent))""")
                db.execute("""CREATE TABLE IF NOT EXISTS executor_devices (
                    device_id TEXT PRIMARY KEY, highest_intent INTEGER NOT NULL DEFAULT -1,
                    cancelled_through INTEGER NOT NULL DEFAULT -1, current_token TEXT)""")
                db.execute("""CREATE TABLE IF NOT EXISTS executor_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL,
                    at TEXT NOT NULL, action TEXT NOT NULL, state TEXT NOT NULL,
                    evidence_id TEXT, error TEXT)""")
                db.execute("CREATE INDEX IF NOT EXISTS executor_actions_token ON executor_actions(token,id)")
            db.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with database_guard(self.path):
            with self.connection_transaction() as db:
                yield db

    @contextmanager
    def connection_transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def meta(db, key, default=None):
        row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def set_meta(db, key, value):
        db.execute(
            "INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, encode(value)),
        )

    @staticmethod
    def device(db, device_id="living-room"):
        row = db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise KeyError(device_id)
        device = {
            "manual_completions": {},
            "id": row["id"],
            "revision": row["revision"],
            **json.loads(row["payload"]),
        }
        device["preferences"].setdefault("discovery_leagues", list(DEFAULT_LEAGUES))
        device.setdefault("public_access", {"play_now": False, "add_to_plan": False})
        from .planner import ordered_plan

        device["plan"] = ordered_plan(device)
        return device

    @staticmethod
    def save_device(db, device):
        db.execute(
            "INSERT INTO devices VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "revision=excluded.revision,payload=excluded.payload",
            (
                device["id"],
                device["revision"],
                encode({k: v for k, v in device.items() if k not in {"id", "revision"}}),
            ),
        )

    @staticmethod
    def upsert_team(db, team, seen_at):
        existing = db.execute("SELECT payload FROM team_directory WHERE key=?", (team["key"],)).fetchone()
        merged = json.loads(existing[0]) if existing else {}
        # Cached team lists do not contain city/nickname. Preserve richer feed metadata.
        merged.update({k: v for k, v in team.items() if v is not None or k not in merged})
        db.execute(
            "INSERT INTO team_directory VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "league=excluded.league,payload=excluded.payload,seen_at=excluded.seen_at",
            (team["key"], team["league"], encode(merged), seen_at),
        )

    @staticmethod
    def teams(db):
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload FROM team_directory ORDER BY league,json_extract(payload,'$.full_name'),key"
            )
        ]

    @staticmethod
    def undo_entry(db, device_id):
        return db.execute(
            "SELECT * FROM edit_history WHERE device_id=? AND undone_by IS NULL ORDER BY id DESC LIMIT 1",
            (device_id,),
        ).fetchone()

    @staticmethod
    def lease(db, resource, owner, now, seconds=15):
        db.execute(
            "INSERT INTO leases VALUES (?,?,?) ON CONFLICT(resource) DO UPDATE SET "
            "owner=excluded.owner,expires=excluded.expires WHERE leases.expires<=? OR leases.owner=?",
            (resource, owner, now + seconds, now, owner),
        )
        return db.execute("SELECT owner FROM leases WHERE resource=?", (resource,)).fetchone()[0] == owner

    @staticmethod
    def log(db, now, message, detail="", kind="decision", device="living-room"):
        db.execute(
            "INSERT INTO activity(device_id,at,kind,message,detail) VALUES (?,?,?,?,?)",
            (device, now, kind, message, detail),
        )
        # Retain the most recent 2,000 decisions without accumulating screenshots.
        db.execute(
            "DELETE FROM activity WHERE sequence <= (SELECT COALESCE(MAX(sequence),0)-2000 FROM activity)"
        )
