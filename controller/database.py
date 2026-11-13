import json
import logging
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .leagues import DEFAULT_LEAGUES
from .storage_lock import database_guard

log = logging.getLogger(__name__)


def stack_locations(frame):
    """Locations only: never retain frame locals, SQL values or credentials."""
    frames = []
    while frame is not None and len(frames) < 32:
        frames.append({"file": frame.f_code.co_filename, "line": frame.f_lineno,
                       "function": frame.f_code.co_name})
        frame = frame.f_back
    return frames


def encode(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def is_database_busy(error):
    """Only lock contention is retryable; corruption and SQL defects are not."""
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return str(error) in {"database is locked", "database table is locked", "database schema is locked"}


class PreparationChanged(RuntimeError):
    """Planning inputs kept changing; no mutation body has been executed."""


class TransactionConnection(sqlite3.Connection):
    """Allow domain code to mark a post-commit wake-up without an early signal."""

    def defer_after_commit(self, callback):
        if not hasattr(self, "commit_callbacks"):
            self.commit_callbacks = []
        self.commit_callbacks.append(callback)


class Database:
    # Older releases must not ignore completion, discovery, or source priority.
    SCHEMA_VERSION = 25

    def __init__(self, path):
        self.path = path
        self.before_commit = None
        self.after_commit = None
        self._writer_lock = threading.Lock()
        self._writer = None
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
            # Both the public test branch and Plex release used schema 9.
            # Ensure Plex tables exist when upgrading either lineage.
            if version < 10:
                db.execute("CREATE TABLE IF NOT EXISTS plex_settings (device_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS plex_runtime (device_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS plex_assets (digest TEXT PRIMARY KEY, data BLOB NOT NULL, mime TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS plex_commands (device_id TEXT NOT NULL, id TEXT NOT NULL, body_hash TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(device_id,id))")
            if version < 11:
                db.execute("""CREATE TABLE IF NOT EXISTS chooser_captures (
                    call_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, payload TEXT NOT NULL)""")
            if version < 12:
                db.execute("""CREATE TABLE IF NOT EXISTS event_entitlements (
                    device_id TEXT NOT NULL, content_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued', status TEXT NOT NULL DEFAULT 'unknown',
                    failures INTEGER NOT NULL DEFAULT 0, next_check REAL,
                    check_id TEXT, started_at REAL, checked_at TEXT, manual INTEGER NOT NULL DEFAULT 0,
                    evidence TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(device_id,content_id))""")
                db.execute("CREATE INDEX IF NOT EXISTS entitlement_due ON event_entitlements(device_id,state,next_check)")
            if version < 13:
                # Expand the previous default selection once, not custom or empty lists.
                # Explicit Teamarr routing overrides remain owned by Teamarr.
                previous_defaults = {
                    "nfl", "college-football", "nhl", "mlb", "nba", "cfl", "uefa.champions", "f1",
                }
                for device_id, payload in db.execute("SELECT id,payload FROM devices").fetchall():
                    device = json.loads(payload)
                    selected = device.get("preferences", {}).get("discovery_leagues")
                    if isinstance(selected, list) and set(selected) == previous_defaults:
                        device["preferences"]["discovery_leagues"] = selected + ["eng.2", "ger.1", "ger.2"]
                        db.execute(
                            "UPDATE devices SET payload=?, revision=revision+1 WHERE id=?",
                            (encode(device), device_id),
                        )
            if version < 14:
                previous_defaults = {
                    "nfl", "college-football", "nhl", "mlb", "nba", "cfl", "uefa.champions",
                    "eng.2", "ger.1", "ger.2", "f1",
                }
                for device_id, payload in db.execute("SELECT id,payload FROM devices").fetchall():
                    device = json.loads(payload)
                    selected = device.get("preferences", {}).get("discovery_leagues")
                    if isinstance(selected, list) and set(selected) == previous_defaults:
                        device["preferences"]["discovery_leagues"] = selected + [
                            league for league in DEFAULT_LEAGUES if league not in previous_defaults
                        ]
                        db.execute(
                            "UPDATE devices SET payload=?, revision=revision+1 WHERE id=?",
                            (encode(device), device_id),
                        )
            if version < 16:
                # Large retained model/catalogue bodies must be parsed only when
                # evidence changes, never by every browser/ticker/planner read.
                db.execute("""CREATE TABLE IF NOT EXISTS entitlement_read_models (
                    device_id TEXT NOT NULL, content_id TEXT NOT NULL, evidence TEXT NOT NULL,
                    PRIMARY KEY(device_id,content_id))""")
                projection = """json_object(
                    'identity',json_extract(evidence,'$.identity'),
                    'reason',json_extract(evidence,'$.reason'),
                    'error',json_extract(evidence,'$.error'),
                    'last_deferral',json_extract(evidence,'$.last_deferral'),
                    'audit',json_object(
                        'error',json_extract(evidence,'$.audit.error'),
                        'search_queries_exhausted',json(CASE json_type(evidence,'$.audit.search_queries_exhausted')
                            WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' ELSE 'null' END),
                        'match_status',json_extract(evidence,'$.audit.match_status'),
                        'search_attempts',json((SELECT json_group_array(json_object(
                            'state',json_extract(value,'$.state'),
                            'error',json_extract(value,'$.error')))
                            FROM json_each(evidence,'$.audit.search_attempts')))))"""
                db.execute(f"INSERT OR REPLACE INTO entitlement_read_models "
                           f"SELECT device_id,content_id,{projection} FROM event_entitlements")
                for event, name in (("INSERT", "insert"), ("UPDATE OF evidence", "update")):
                    db.execute(f"""CREATE TRIGGER IF NOT EXISTS entitlement_view_{name}
                        AFTER {event} ON event_entitlements BEGIN
                        INSERT OR REPLACE INTO entitlement_read_models
                        SELECT device_id,content_id,{projection} FROM event_entitlements
                        WHERE device_id=NEW.device_id AND content_id=NEW.content_id;
                        END""")
                db.execute("""CREATE TRIGGER IF NOT EXISTS entitlement_view_delete
                    AFTER DELETE ON event_entitlements BEGIN
                    DELETE FROM entitlement_read_models
                    WHERE device_id=OLD.device_id AND content_id=OLD.content_id;
                    END""")
            if version < 17:
                # One account catalogue replaces the duplicated per-device queues.
                # Older code must not reopen it and start seeding those queues again.
                db.execute("CREATE TABLE IF NOT EXISTS entitlement_migration_archive AS "
                           "SELECT * FROM event_entitlements WHERE 0")
                from .entitlement_store import share_account
                share_account(db)
            if version < 20:
                # Expand only the previous full selection; preserve custom lists and opt-outs.
                cup_leagues = ["eng.league_cup", "ger.dfb_pokal"]
                previous_defaults = {
                    "nfl", "college-football", "nhl", "mlb", "nba", "cfl", "uefa.champions",
                    "eng.2", "ger.1", "ger.2", "eng.3", "eng.4", "uefa.europa",
                    "uefa.europa.conf", "eng.1", "ita.1", "esp.1", "usa.1", "usa.nwsl",
                    "eng.fa", "wnba", "mens-college-basketball", "womens-college-basketball",
                    "pwhl", "chl", "ohl", "whl", "qmjhl", "mens-college-hockey",
                    "womens-college-hockey", "urc", "prem", "nrl", "afl", "pll", "f1",
                }
                rows = db.execute("SELECT id,payload FROM devices").fetchall() if version < 19 else []
                for device_id, payload in rows:
                    device = json.loads(payload)
                    selected = device.get("preferences", {}).get("discovery_leagues")
                    if isinstance(selected, list) and set(selected) == previous_defaults:
                        device["preferences"]["discovery_leagues"] = selected + cup_leagues
                        db.execute(
                            "UPDATE devices SET payload=?, revision=revision+1 WHERE id=?",
                            (encode(device), device_id),
                        )
                # Schema 19 updated device projections but omitted the canonical
                # shared plan. Repair only that exact mismatch, including on
                # databases whose first schema-19 startup already failed.
                row = db.execute("SELECT value FROM metadata WHERE key='fleet_plan'").fetchone()
                if row:
                    canonical = json.loads(row[0])
                    selected = canonical.get("preferences", {}).get("discovery_leagues")
                    if isinstance(selected, list) and set(selected) == previous_defaults:
                        from .fleet import planning_fields

                        canonical["preferences"]["discovery_leagues"] = selected + cup_leagues
                        projections = [planning_fields(json.loads(raw)) for (raw,) in
                                       db.execute("SELECT payload FROM devices").fetchall()]
                        if projections and all(fields == canonical for fields in projections):
                            db.execute("UPDATE metadata SET value=? WHERE key='fleet_plan'",
                                       (encode(canonical),))
            if version < 21:
                # State columns follow large request/report JSON in the table.
                # Polling history through (device_id,intent) repeatedly walks
                # those overflow pages even when no active work exists.
                db.execute("CREATE INDEX IF NOT EXISTS executor_queue ON executor_jobs "
                           "(device_id,cancel_requested,state,next_check,intent,token)")
                db.execute("CREATE INDEX IF NOT EXISTS executor_cancellations ON executor_jobs "
                           "(device_id,intent,next_check,token) WHERE cancel_requested=1")
                db.execute("CREATE INDEX IF NOT EXISTS executor_claims ON executor_jobs "
                           "(device_id,touched_device,retired_at,content_id)")
            if version < 22:
                from .acquisition import migrate_acquisition_timeout
                migrate_acquisition_timeout(db)
            if version < 23:
                db.execute("CREATE TABLE IF NOT EXISTS planning_versions (name TEXT PRIMARY KEY, revision INTEGER NOT NULL)")
                for table in ("contents", "content_status", "event_entitlements"):
                    db.execute("INSERT OR IGNORE INTO planning_versions VALUES (?,0)", (table,))
                    for event in ("INSERT", "UPDATE", "DELETE"):
                        db.execute(f"""CREATE TRIGGER IF NOT EXISTS planning_{table}_{event.lower()}
                            AFTER {event} ON {table} BEGIN
                            UPDATE planning_versions SET revision=revision+1 WHERE name='{table}';
                            END""")
            if version < 24:
                db.execute("CREATE TABLE IF NOT EXISTS planning_item_versions "
                           "(content_id TEXT PRIMARY KEY, revision INTEGER NOT NULL)")
                db.execute("INSERT OR IGNORE INTO planning_item_versions SELECT id,0 FROM contents")
                for table, column in (("contents", "id"), ("content_status", "content_id"),
                                      ("event_entitlements", "content_id")):
                    for event in ("INSERT", "UPDATE", "DELETE"):
                        aliases = ("NEW",) if event == "INSERT" else ("OLD",) if event == "DELETE" else ("OLD", "NEW")
                        changes = "\n".join(
                            "INSERT INTO planning_item_versions(content_id,revision) "
                            f"SELECT {alias}.{column},1 WHERE "
                            + (f"OLD.{column}!=NEW.{column}" if event == "UPDATE" and alias == "OLD" else "1")
                            + " ON CONFLICT(content_id) DO UPDATE SET revision=revision+1;" for alias in aliases)
                        db.execute(f"""CREATE TRIGGER IF NOT EXISTS planning_item_{table}_{event.lower()}
                            AFTER {event} ON {table} BEGIN {changes} END""")
                # Retain tombstones: deleting/reinserting an ID must never reuse
                # a revision while an older WAL reader still holds its entry.
            if version < 25:
                # Keep routine queue/card reads on a small covering index. The
                # table also holds large audit blobs and must not be walked by
                # every overview or planner just to read scheduling columns.
                db.execute("CREATE INDEX IF NOT EXISTS entitlement_summary ON event_entitlements "
                           "(device_id,content_id,state,status,checked_at,next_check,failures,started_at,manual,check_id)")
                # seen_at remains the actual import receipt. When the upstream
                # observation timestamp is unchanged and already precedes both
                # receipts, it cannot renew lifecycle evidence or its projection.
                # Conservatively invalidate within one second: SQLite's date
                # arithmetic is less precise than Python's microsecond clock.
                changed = {
                    "contents": """OLD.id IS NOT NEW.id OR OLD.source IS NOT NEW.source
                        OR OLD.active IS NOT NEW.active OR OLD.snapshot IS NOT NEW.snapshot
                        OR (OLD.seen_at IS NOT NEW.seen_at AND
                            (json_extract(NEW.snapshot,'$.status_received_at') IS NULL
                             OR julianday(json_extract(NEW.snapshot,'$.status_received_at')) IS NULL
                             OR julianday(OLD.seen_at) IS NULL OR julianday(NEW.seen_at) IS NULL
                             OR julianday(OLD.seen_at)<=julianday(json_extract(NEW.snapshot,'$.status_received_at'))+1.0/86400
                             OR julianday(NEW.seen_at)<=julianday(json_extract(NEW.snapshot,'$.status_received_at'))+1.0/86400))""",
                    "content_status": " OR ".join(f"OLD.{key} IS NOT NEW.{key}" for key in
                        ("content_id", "observation", "error", "last_attempt", "last_success", "next_check")),
                    "event_entitlements": " OR ".join(f"OLD.{key} IS NOT NEW.{key}" for key in
                        ("device_id", "content_id", "state", "status", "checked_at", "next_check", "failures",
                         "started_at", "manual", "check_id", "evidence")),
                }
                for table, condition in changed.items():
                    for name in (f"planning_{table}_update", f"planning_item_{table}_update"):
                        sql = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                                         (name,)).fetchone()[0]
                        db.execute('DROP TRIGGER "' + name + '"')
                        prefix, body = sql.split(" BEGIN", 1)
                        db.execute(prefix.split(" WHEN ", 1)[0] + f" WHEN {condition} BEGIN" + body)
            db.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def transaction(self, *, prepare=None, version=None):
        with database_guard(self.path):
            with self.connection_transaction(prepare=prepare, version=version) as db:
                yield db

    @contextmanager
    def snapshot(self):
        """Consistent WAL read without reserving the single writer slot."""
        with database_guard(self.path):
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            try:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                yield db
            finally:
                db.close()

    @contextmanager
    def connection_transaction(self, *, prepare=None, version=None):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None, factory=TransactionConnection)
        writer = None
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=10000")
            if prepare is not None:
                # Catalogue construction belongs to a WAL read snapshot, not
                # the single writer slot. Fence relevant commits between that
                # snapshot and the write claim. By default use the SAME
                # connection's data_version to fence every external commit.
                # Nothing in the transaction body (including callbacks) runs
                # until preparation is current. Never replay a mutation body.
                deadline = time.monotonic() + 10
                read_version = version or (lambda connection: connection.execute("PRAGMA data_version").fetchone()[0])
                while True:
                    db.execute("PRAGMA query_only=ON")
                    db.execute("BEGIN")
                    prepared_version = read_version(db)
                    prepared = prepare(db)
                    db.rollback()
                    db.execute("PRAGMA query_only=OFF")
                    db.execute("BEGIN IMMEDIATE")
                    if read_version(db) == prepared_version:
                        db.prepared = prepared
                        break
                    db.rollback()
                    if time.monotonic() >= deadline:
                        raise PreparationChanged("Planning inputs changed throughout the preparation budget")
            else:
                db.execute("BEGIN IMMEDIATE")
            writer = {"thread_id": threading.get_ident(), "started": time.monotonic(),
                      "phase": "transaction", "caller": stack_locations(sys._getframe(1))}
            with self._writer_lock:
                self._writer = writer
            yield db
            writer["phase"] = "before_commit"
            notify = bool(self.before_commit(db)) if db.total_changes and self.before_commit else False
            writer["phase"] = "commit"
            db.commit()
            self._finish_writer(writer)
            writer = None
            for callback in getattr(db, "commit_callbacks", ()):
                callback()
            if notify and self.after_commit:
                self.after_commit()
        except BaseException as exc:
            if isinstance(exc, PreparationChanged):
                log.warning("Planning preparation deferred after concurrent input changes; no mutation executed")
            if is_database_busy(exc):
                # The blocked caller's traceback alone never identifies the
                # holder. Capture its current stack while it is still blocked.
                log.warning("SQLite writer contention: %s", encode(self.writer_diagnostic()))
            db.rollback()
            raise
        finally:
            if writer is not None:
                self._finish_writer(writer)
            db.close()

    def writer_diagnostic(self):
        with self._writer_lock:
            writer = dict(self._writer) if self._writer else None
        if writer is None:
            return {"holder": "not_observed", "scope": "this_database_instance"}
        writer["held_seconds"] = round(time.monotonic() - writer.pop("started"), 3)
        writer["frames"] = stack_locations(sys._current_frames().get(writer["thread_id"]))
        return writer

    def _finish_writer(self, writer):
        elapsed = time.monotonic() - writer["started"]
        with self._writer_lock:
            if self._writer is writer:
                self._writer = None
        if elapsed >= 1:
            log.warning("Slow SQLite write transaction: %s", encode({
                "held_seconds": round(elapsed, 3), "phase": writer["phase"], "caller": writer["caller"],
            }))

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
