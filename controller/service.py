import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from .coordinator import PlaybackCoordinator
from .database import Database, encode
from .fixtures import default_device, fixtures, make_team
from .planner import content_view, parse_time, preview_plan, priority, team_key
from .playback import PlaybackAdapter, SimulatedPlaybackAdapter
from .teamarr import TeamarrClient

log = logging.getLogger(__name__)


class Controller(PlaybackCoordinator):
    CONFIG_FIELDS = ("rules", "team_ranks", "preferences")

    def __init__(self, settings, playback: PlaybackAdapter | None = None):
        self.settings = settings
        self.db = Database(settings.database)
        self.playback = playback or SimulatedPlaybackAdapter(self.db, settings.observation_ttl)
        self.owner = str(uuid.uuid4())
        self.client = TeamarrClient(settings.teamarr_url, settings.teamarr_token)
        self.tasks = []
        self.initialize()

    def initialize(self):
        with self.db.transaction() as db:
            existing = self.db.meta(db, "mode")
            if existing and existing != self.settings.mode:
                raise ValueError("Use a separate CONTROLLER_DATABASE when changing demo/teamarr mode")
            self.db.set_meta(db, "mode", self.settings.mode)
            if not db.execute("SELECT 1 FROM devices").fetchone():
                device = default_device(self.settings.mode)
                if self.settings.mode == "demo":
                    entries, now = fixtures()
                    self.replace_catalog(db, entries, "demo")
                    self.db.set_meta(db, "demo_now", now)
                    self.db.set_meta(db, "scenario", "normal")
                    device["plan"] = [self.entry("demo:canadiens")]
                self.db.save_device(db, device)
                self.db.log(db, self.now(db).isoformat(), "Controller started", "Playback adapter: simulator")
            # Populate the new directory when upgrading an existing version-1 database.
            for row in db.execute("SELECT snapshot,seen_at FROM contents").fetchall():
                self.remember_teams(db, json.loads(row["snapshot"]), row["seen_at"])
            if self.settings.mode == "demo":
                team = {**make_team("Vancouver", "Canucks", "VAN"), "league": "nhl"}
                team["key"] = team_key(team, team["league"])
                self.db.upsert_team(db, team, datetime.now(UTC).isoformat())

    def remember_teams(self, db, item, fetched):
        event = item.get("event") or {}
        league = event.get("league") or item.get("competition")
        for raw in (event.get("away_team_details"), event.get("home_team_details")):
            if raw and raw.get("id") and raw.get("provider") and league:
                self.db.upsert_team(db, {**raw, "league": league, "key": team_key(raw, league)}, fetched)

    def now(self, db):
        return parse_time(self.db.meta(db, "demo_now")) if self.settings.mode == "demo" else datetime.now(UTC)

    @staticmethod
    def entry(content_id):
        return {
            "id": str(uuid.uuid4()),
            "content_id": content_id,
            "created_at": datetime.now(UTC).isoformat(),
        }

    def replace_catalog(self, db, entries, source):
        fetched = datetime.now(UTC).isoformat()
        db.execute("UPDATE contents SET active=0 WHERE source=?", (source,))
        for item in entries:
            db.execute(
                "INSERT INTO contents VALUES (?,?,1,?,?) ON CONFLICT(id) DO UPDATE SET "
                "active=1,seen_at=excluded.seen_at,snapshot=excluded.snapshot,source=excluded.source",
                (item["id"], source, fetched, encode(item)),
            )
            self.remember_teams(db, item, fetched)
        self.db.set_meta(
            db,
            "feed_health",
            {
                "state": "ok",
                "last_success": fetched,
                "count": len(entries),
                "error": None,
                "provider_freshness": "unknown",
            },
        )

    def items(self, db):
        now, real = self.now(db), datetime.now(UTC)
        return [
            content_view(
                json.loads(r["snapshot"]),
                r["seen_at"],
                r["active"],
                now,
                real,
                self.settings.mode,
                self.settings.status_ttl,
            )
            for r in db.execute("SELECT * FROM contents ORDER BY id")
        ]

    def overview(self, device_id="living-room"):
        with self.db.transaction() as db:
            device = self.db.device(db, device_id)
            items = self.items(db)
            plan_ids = {p["content_id"] for p in device["plan"]}
            retained = {device.get("desired"), (device.get("observed") or {}).get("content_id")}
            cards = []
            for item in items:
                if not item["active"] and item["content_id"] not in plan_ids | retained:
                    continue
                card = {k: v for k, v in item.items() if k != "snapshot"}
                p = next((p for p in device["plan"] if p["content_id"] == item["content_id"]), None)
                card["watch_entry_id"] = p["id"] if p else None
                card["priority"] = priority(device, item)
                failed = device["failures"].get(item["content_id"])
                card["failure"] = failed
                cards.append(card)
            cards.sort(key=lambda c: (c["start_time"], c["content_id"]))
            activity = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM activity WHERE device_id=? ORDER BY sequence DESC LIMIT 100", (device_id,)
                )
            ]
            latest_job = db.execute(
                "SELECT id,content_id,state,progress,deadline_at,delivery_attempts,error FROM jobs WHERE device_id=? ORDER BY rowid DESC LIMIT 1",
                (device_id,),
            ).fetchone()
            return {
                "device": device,
                "playback_job": dict(latest_job) if latest_job else None,
                "teams": self.db.teams(db),
                "team_directory_health": self.db.meta(
                    db,
                    "team_directory_health",
                    {"state": "demo" if self.settings.mode == "demo" else "starting"},
                ),
                "undo": self.undo_summary(db, device),
                "events": cards,
                "plan_preview": preview_plan(device, items, self.now(db)),
                "activity": activity,
                "health": self.db.meta(db, "feed_health", {"state": "starting"}),
                "meta": {
                    "mode": self.settings.mode,
                    "playback_adapter": "simulator",
                    "status_simulated": True,
                    "now": self.now(db).isoformat(),
                    "server_time": datetime.now(UTC).isoformat(),
                    "scenario": self.db.meta(db, "scenario"),
                    "schema_version": 1,
                },
            }

    def mutate(self, device_id, command_id, revision, payload, apply, *, history=None):
        digest = hashlib.sha256(encode(payload).encode()).hexdigest()
        with self.db.transaction() as db:
            prior = db.execute(
                "SELECT * FROM commands WHERE device_id=? AND id=?", (device_id, command_id)
            ).fetchone()
            if prior:
                if prior["body_hash"] != digest:
                    raise HTTPException(409, "Command ID was already used with a different payload")
                return json.loads(prior["result"])
            device = self.db.device(db, device_id)
            if device["revision"] != revision:
                raise HTTPException(
                    409,
                    {
                        "message": "Configuration changed. Refresh and try again.",
                        "current_revision": device["revision"],
                    },
                )
            before = encode({k: device[k] for k in history[0]}) if history else None
            apply(db, device)
            if history:
                after = encode({k: device[k] for k in history[0]})
                if before != after:
                    db.execute(
                        "INSERT INTO edit_history(device_id,command_id,description,created_at,"
                        "before_payload,after_payload) VALUES (?,?,?,?,?,?)",
                        (device_id, command_id, history[1], datetime.now(UTC).isoformat(), before, after),
                    )
                    db.execute(
                        "DELETE FROM edit_history WHERE device_id=? AND id NOT IN "
                        "(SELECT id FROM edit_history WHERE device_id=? ORDER BY id DESC LIMIT 50)",
                        (device_id, device_id),
                    )
            device["revision"] += 1
            self.db.save_device(db, device)
            result = {"command_id": command_id, "revision": device["revision"], "accepted": True}
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)", (device_id, command_id, digest, encode(result))
            )
            return result

    def undo_summary(self, db, device):
        row = self.db.undo_entry(db, device["id"])
        if row is None:
            return None
        after = json.loads(row["after_payload"])
        if any(device.get(key) != value for key, value in after.items()):
            return None
        return {"id": row["id"], "description": row["description"], "created_at": row["created_at"]}

    def undo_command(self, device_id, command):
        def apply(db, device):
            summary = self.undo_summary(db, device)
            if summary is None or summary["id"] != command.history_id:
                raise HTTPException(409, "This edit can no longer be undone. Refresh the current state.")
            row = self.db.undo_entry(db, device_id)
            before = json.loads(row["before_payload"])
            device.update(before)
            if "plan" in before:
                device["force_switch"] = True
            db.execute("UPDATE edit_history SET undone_by=? WHERE id=?", (command.command_id, row["id"]))
            self.db.log(db, self.now(db).isoformat(), "Edit undone", row["description"], "undo", device_id)

        return self.mutate(
            device_id, command.command_id, command.expected_revision, command.model_dump(), apply
        )

    def export_configuration(self, device_id):
        with self.db.transaction() as db:
            d = self.db.device(db, device_id)
            return {
                "format": "dillflix-controller-config",
                "schema_version": 1,
                "source_mode": self.settings.mode,
                "exported_at": datetime.now(UTC).isoformat(),
                "configuration": {k: d[k] for k in self.CONFIG_FIELDS},
            }

    def import_preview(self, device_id, request):
        with self.db.transaction() as db:
            d = self.db.device(db, device_id)
            if d["revision"] != request.expected_revision:
                raise HTTPException(409, "Configuration changed. Refresh the import preview.")
            config = request.document.configuration.model_dump()
            known = {t["key"] for t in self.db.teams(db)}
            referenced = {r["team_id"] for r in config["rules"] if r["team_id"]}
            referenced.update(key for keys in config["team_ranks"].values() for key in keys)
            warnings = []
            if request.document.source_mode != self.settings.mode:
                warnings.append(
                    "This file was exported from a different mode. Demo and real team identities differ."
                )
            missing = referenced - known
            if missing:
                warnings.append(
                    f"{len(missing)} team identities are not in the directory yet; they will be preserved."
                )
            return {
                "revision": d["revision"],
                "configuration": config,
                "warnings": warnings,
                "summary": {
                    "current_rules": len(d["rules"]),
                    "imported_rules": len(config["rules"]),
                    "ranked_teams": sum(len(v) for v in config["team_ranks"].values()),
                },
            }

    def import_configuration(self, device_id, request):
        def apply(db, d):
            d.update(request.document.configuration.model_dump())
            self.db.log(
                db,
                self.now(db).isoformat(),
                "Configuration imported",
                "Priorities, team rankings, and preferences replaced; watch plan retained",
                "settings",
                device_id,
            )

        return self.mutate(
            device_id,
            request.command_id,
            request.expected_revision,
            request.model_dump(mode="json"),
            apply,
            history=(self.CONFIG_FIELDS, "Import configuration"),
        )

    def apply_plan(self, db, device, action, *, preview=False):
        items = {i["content_id"]: i for i in self.items(db)}
        op = action["type"]
        content_id = action.get("content_id")
        if op in {"add", "play_now"}:
            if content_id not in items:
                raise HTTPException(404, "Content is not in the catalog")
            if items[content_id]["lifecycle"]["state"] in {"ended", "cancelled"}:
                raise HTTPException(422, "This event has finished or was cancelled")
            if op == "play_now" and not items[content_id]["playable"]:
                raise HTTPException(422, "Play now requires confirmed live status and a valid viewing option")
            entry = next((p for p in device["plan"] if p["content_id"] == content_id), self.entry(content_id))
            device["plan"] = [p for p in device["plan"] if p["content_id"] != content_id]
            if op == "play_now" or action.get("priority") == "first":
                device["plan"].insert(0, entry)
            else:
                device["plan"].append(entry)
            if op == "play_now":
                device["automation"] = "active"
                device["failures"].pop(content_id, None)
        elif op == "remove":
            entry_id = action.get("entry_id")
            if entry_id not in {p["id"] for p in device["plan"]}:
                raise HTTPException(404, "Watch-plan entry not found")
            device["plan"] = [p for p in device["plan"] if p["id"] != entry_id]
        elif op == "reorder":
            order = action.get("ordered_entry_ids") or []
            mapped = {p["id"]: p for p in device["plan"]}
            if len(order) != len(mapped) or set(order) != set(mapped):
                raise HTTPException(422, "Supply each current watch-plan entry exactly once")
            device["plan"] = [mapped[key] for key in order]
        device["force_switch"] = True
        if not preview:
            self.db.log(db, self.now(db).isoformat(), "Watch plan updated", op, "manual", device["id"])

    def plan_command(self, device_id, command):
        payload = command.model_dump()
        description = {
            "add": "Add event to watch plan",
            "play_now": "Play now selection",
            "reorder": "Reorder watch plan",
            "remove": "Remove event from watch plan",
        }[payload["action"]["type"]]
        return self.mutate(
            device_id,
            command.command_id,
            command.expected_revision,
            payload,
            lambda db, d: self.apply_plan(db, d, payload["action"]),
            history=(("plan",), description),
        )

    def preview(self, device_id, command):
        with self.db.transaction() as db:
            device = self.db.device(db, device_id)
            if command.expected_revision != device["revision"]:
                raise HTTPException(409, "The watch plan changed. Refresh the preview.")
            self.apply_plan(db, device, command.action.model_dump(), preview=True)
            return {
                "revision": device["revision"],
                "entries": device["plan"],
                **preview_plan(device, self.items(db), self.now(db)),
            }

    def rules_command(self, device_id, update):
        # Keep the original serialized field order so version-1 command receipts
        # still recognize retries after RulesUpdate gained shared configuration fields.
        data = {
            "command_id": update.command_id,
            "expected_revision": update.expected_revision,
            **update.model_dump(exclude={"command_id", "expected_revision"}),
        }

        def apply(db, device):
            if len({r["id"] for r in data["rules"]}) != len(data["rules"]):
                raise HTTPException(422, "Rule IDs must be unique")
            if any(len(ranks) != len(set(ranks)) for ranks in data["team_ranks"].values()):
                raise HTTPException(422, "Team rankings must not contain duplicate identities")
            device.update({key: data[key] for key in ("rules", "team_ranks", "preferences")})
            self.db.log(
                db,
                self.now(db).isoformat(),
                "Priorities and settings saved",
                "Manual order preserved",
                "settings",
                device_id,
            )

        return self.mutate(
            device_id,
            update.command_id,
            update.expected_revision,
            data,
            apply,
            history=(self.CONFIG_FIELDS, "Edit priorities and settings"),
        )

    def automation_command(self, device_id, update):
        def apply(db, d):
            d["automation"] = update.mode
            d["force_switch"] = True
            if update.mode == "paused":
                d["intent_version"] += 1
                db.execute(
                    "UPDATE jobs SET state='cancelled' WHERE device_id=? AND state='pending'", (device_id,)
                )
                d["desired"] = (d.get("observed") or {}).get("content_id")
                observed = d.get("observed")
                d["playback_state"] = (
                    ("verified" if observed.get("verified") else "unverified") if observed else "waiting"
                )
            self.db.log(
                db,
                self.now(db).isoformat(),
                f"Automation {update.mode}",
                "Watch plan retained",
                "automation",
                device_id,
            )

        return self.mutate(device_id, update.command_id, update.expected_revision, update.model_dump(), apply)

    def simulation(self, request):
        if self.settings.mode != "demo":
            raise HTTPException(409, "Demo scenarios are unavailable in Teamarr mode")
        with self.db.transaction() as db:
            if request.action == "advance":
                self.db.set_meta(
                    db, "demo_now", (self.now(db) + timedelta(minutes=request.minutes)).isoformat()
                )
            else:
                entries, now = fixtures(request.scenario)
                self.replace_catalog(db, entries, "demo")
                self.db.set_meta(db, "demo_now", now)
                self.db.set_meta(db, "scenario", request.scenario)
                d = self.db.device(db)
                d.update(
                    {
                        "failures": {},
                        "observed": None,
                        "desired": None,
                        "playback_state": "waiting",
                        "started_at": None,
                        "last_switch_at": None,
                        "force_switch": True,
                    }
                )
                d["intent_version"] += 1
                d["revision"] += 1
                d["plan"] = [self.entry("demo:canadiens")]
                if request.scenario in {"overlap", "overtime"}:
                    d["plan"].append(self.entry("demo:jays"))
                db.execute("UPDATE jobs SET state='superseded' WHERE state='pending'")
                db.execute("DELETE FROM edit_history WHERE device_id=?", (d["id"],))
                self.db.save_device(db, d)
            self.db.log(db, self.now(db).isoformat(), "Simulation updated", request.action, "simulation")
        return {"accepted": True}

    async def refresh_catalog(self):
        if self.settings.mode != "teamarr":
            return
        try:
            entries, schema = await self.client.fetch_snapshot()
            with self.db.transaction() as db:
                self.replace_catalog(db, entries, "teamarr")
                self.db.set_meta(db, "feed_schema_version", schema)
        except Exception as exc:
            log.warning("Teamarr refresh failed: %s", type(exc).__name__)
            with self.db.transaction() as db:
                health = self.db.meta(db, "feed_health", {})
                health.update(
                    {
                        "state": "degraded",
                        "error": f"{type(exc).__name__}: feed refresh failed",
                        "last_attempt": datetime.now(UTC).isoformat(),
                    }
                )
                self.db.set_meta(db, "feed_health", health)

    async def refresh_team_directory(self):
        if self.settings.mode != "teamarr":
            return
        with self.db.transaction() as db:
            primary = {"nfl", "nhl", "mlb", "nba"}
            leagues = sorted(primary) + sorted({t["league"] for t in self.db.teams(db)} - primary)[:16]
            health = self.db.meta(db, "team_directory_health", {"leagues": {}})
        for league in leagues:
            try:
                teams = await self.client.fetch_teams(league)
                fetched = datetime.now(UTC).isoformat()
                with self.db.transaction() as db:
                    for team in teams:
                        self.db.upsert_team(db, team, fetched)
                health["leagues"][league] = {
                    "state": "ok" if teams else "empty",
                    "count": len(teams),
                    "last_success": fetched,
                }
            except Exception as exc:
                previous = health["leagues"].get(league, {})
                health["leagues"][league] = {
                    **previous,
                    "state": "degraded",
                    "error": f"{type(exc).__name__}: team directory refresh failed",
                }
        with self.db.transaction() as db:
            health["state"] = (
                "degraded" if any(v["state"] == "degraded" for v in health["leagues"].values()) else "ok"
            )
            health["last_attempt"] = datetime.now(UTC).isoformat()
            health["count"] = db.execute("SELECT COUNT(*) FROM team_directory").fetchone()[0]
            self.db.set_meta(db, "team_directory_health", health)
            self.db.log(
                db,
                self.now(db).isoformat(),
                "Team directory refreshed",
                f"{health['count']} known teams · {health['state']}",
                "directory",
            )

    async def run_worker(self):
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("Controller tick failed; will retry")
            await asyncio.sleep(self.settings.worker_interval)

    async def run_feed(self):
        while True:
            with self.db.transaction() as db:
                owns = self.db.lease(db, "catalog", self.owner, time.time(), self.settings.feed_interval + 60)
            if owns:
                await self.refresh_catalog()
            await asyncio.sleep(self.settings.feed_interval)

    async def run_team_directory(self):
        while True:
            try:
                with self.db.transaction() as db:
                    owns = self.db.lease(
                        db,
                        "team-directory",
                        self.owner,
                        time.time(),
                        self.settings.team_directory_interval + 60,
                    )
                if owns:
                    await self.refresh_team_directory()
            except Exception:
                log.exception("Team directory refresh failed; will retry")
            await asyncio.sleep(self.settings.team_directory_interval)

    def start(self):
        self.tasks = [asyncio.create_task(self.run_worker())]
        if self.settings.mode == "teamarr":
            self.tasks.append(asyncio.create_task(self.run_feed()))
            self.tasks.append(asyncio.create_task(self.run_team_directory()))

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        with self.db.transaction() as db:
            db.execute("DELETE FROM leases WHERE owner=?", (self.owner,))
