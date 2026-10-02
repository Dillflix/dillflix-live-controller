import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from .content_status import ContentStatusAdapter, ContentStatusCoordinator, SimulatedContentStatusAdapter
from .coordinator import PlaybackCoordinator
from .database import Database, encode
from .fixtures import default_device, fixtures, make_team
from .leagues import LEAGUE_NAMES
from .maintenance import policy, prune
from .manual_control import ManualControl
from .planner import content_view, parse_time, preview_plan, priority, team_key
from .playback import PlaybackAdapter, SimulatedPlaybackAdapter
from .storage_lock import database_guard
from .teamarr import TeamarrClient

log = logging.getLogger(__name__)


class Controller(ManualControl, PlaybackCoordinator, ContentStatusCoordinator):
    CONFIG_FIELDS = ("rules", "team_ranks", "preferences")

    def __init__(
        self, settings, playback: PlaybackAdapter | None = None, status: ContentStatusAdapter | None = None
    ):
        self.settings = settings
        self._database_guard = database_guard(settings.database)
        self._database_guard.__enter__()
        try:
            self.db = Database(settings.database)
            self.executor = None
            settings.executor.validate()
            if settings.executor.mode != "simulator":
                if settings.mode != "teamarr":
                    raise ValueError("Real playback requires Teamarr mode; demo fixtures cannot control a TV")
                from .executor.integration import ExecutorContentStatusAdapter, IntegratedPlaybackAdapter
                from .executor.runtime import PlaybackExecutor

                self.executor = PlaybackExecutor(self.db, settings)
                playback = playback or IntegratedPlaybackAdapter(self.executor)
                status = status or ExecutorContentStatusAdapter(self.executor)
            self.playback = playback or SimulatedPlaybackAdapter(self.db, settings.observation_ttl)
            self.status_adapter = status or SimulatedContentStatusAdapter(settings.mode, settings.status_ttl)
            self.owner = str(uuid.uuid4())
            self.client = TeamarrClient(settings.teamarr_url, settings.teamarr_token)
            self.tasks = []
            # Serialize playback calls with manual handoff, never on the event loop.
            self.playback_lock = threading.RLock()
            self.initialize()
        except BaseException:
            self._database_guard.__exit__(None, None, None)
            raise

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
                self.db.log(
                    db,
                    self.now(db).isoformat(),
                    "Controller started",
                    f"Playback adapter: {self.settings.executor.mode}",
                )
            previous_adapter = self.db.meta(db, "playback_adapter", "simulator")
            if previous_adapter != self.settings.executor.mode:
                if (
                    self.settings.executor.mode == "simulator"
                    and db.execute(
                        "SELECT 1 FROM executor_jobs WHERE cancel_requested!=2 AND retired_at IS NULL LIMIT 1"
                    ).fetchone()
                ):
                    raise ValueError("Cancel real playback before switching to the simulator")
                device = self.db.device(db)
                device.update(
                    desired=None,
                    observed=None,
                    playback_state="waiting",
                    started_at=None,
                    last_switch_at=None,
                    recovery=None,
                    executor_health={"state": "starting"},
                    force_switch=True,
                )
                device["intent_version"] += 1
                db.execute("UPDATE jobs SET state='cancelled',cancel_sent=1 WHERE state='pending'")
                db.execute("DELETE FROM content_status")
                self.db.save_device(db, device)
            self.db.set_meta(db, "playback_adapter", self.settings.executor.mode)
            if self.executor:
                device = self.db.device(db)
                if device.get("observed"):
                    device["observed"]["verified"] = False
                    device["playback_state"] = "unverified"
                    self.db.save_device(db, device)
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

    def items(self, db, device_id="living-room"):
        now, real = self.now(db), datetime.now(UTC)
        checks = {row["content_id"]: row for row in db.execute("SELECT * FROM content_status")}
        pins = self.pinned_content_ids(db, device_id=device_id)
        completions = self.db.device(db, device_id)["manual_completions"]
        leagues = set(self.db.device(db, device_id)["preferences"]["discovery_leagues"])
        return [
            content_view(
                snapshot,
                r["active"] and (
                    snapshot.get("source") != "games"
                    or snapshot.get("competition") in leagues or r["id"] in pins
                ),
                self.content_lifecycle(
                    r, checks.get(r["id"]), now, real, r["id"] in pins, completions.get(r["id"])
                ),
            )
            for r in db.execute("SELECT * FROM contents ORDER BY id")
            for snapshot in [json.loads(r["snapshot"])]
        ]

    def overview(self, device_id="living-room"):
        with self.db.transaction() as db:
            device = self.db.device(db, device_id)
            items = self.items(db, device_id)
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
                "SELECT id,content_id,state,progress,deadline_at,delivery_attempts,error,payload FROM jobs WHERE device_id=? ORDER BY rowid DESC LIMIT 1",
                (device_id,),
            ).fetchone()
            latest_job = dict(latest_job) if latest_job else None
            if latest_job:
                latest_job["purpose"] = json.loads(latest_job.pop("payload")).get("purpose", "selection")
            return {
                "device": device,
                "playback_job": latest_job,
                "status_health": self.status_health(db, items, device_id),
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
                    "league_choices": LEAGUE_NAMES,
                    "mode": self.settings.mode,
                    "playback_adapter": self.settings.executor.mode,
                    "status_simulated": self.executor is None,
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
            if "plan" in before or "manual_completions" in before:
                device["force_switch"] = True
            if "manual_completions" in before:
                # Undo restores eligibility, never stale playback claims or evidence.
                db.execute("UPDATE content_status SET next_check=0")
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
        payload = request.model_dump(mode="json")
        if "discovery_leagues" not in request.document.configuration.preferences.model_fields_set:
            payload["document"]["configuration"]["preferences"].pop("discovery_leagues")

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
            payload,
            apply,
            history=(self.CONFIG_FIELDS, "Import configuration"),
        )

    def apply_plan(self, db, device, action, *, preview=False):
        items = {i["content_id"]: i for i in self.items(db, device["id"])}
        op = action["type"]
        content_id = action.get("content_id")
        if op == "play_now" and device.get("manual_control"):
            raise HTTPException(
                409, "End manual control before using Play now. You can still add events to the plan."
            )
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
                device["retry_playback"] = content_id
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
                **preview_plan(device, self.items(db, device_id), self.now(db)),
            }

    def rules_command(self, device_id, update):
        # Keep the original serialized field order so version-1 command receipts
        # still recognize retries after RulesUpdate gained shared configuration fields.
        data = {
            "command_id": update.command_id,
            "expected_revision": update.expected_revision,
            **update.model_dump(exclude={"command_id", "expected_revision"}),
        }
        # Existing clients/receipts predate discovery settings. Hash their
        # original payload and preserve today's selection when the field is absent.
        if "discovery_leagues" not in update.preferences.model_fields_set:
            data["preferences"].pop("discovery_leagues")

        def apply(db, device):
            if len({r["id"] for r in data["rules"]}) != len(data["rules"]):
                raise HTTPException(422, "Rule IDs must be unique")
            if any(len(ranks) != len(set(ranks)) for ranks in data["team_ranks"].values()):
                raise HTTPException(422, "Team rankings must not contain duplicate identities")
            device.update(
                rules=data["rules"], team_ranks=data["team_ranks"],
                preferences={
                    **data["preferences"],
                    "discovery_leagues": data["preferences"].get(
                        "discovery_leagues", device["preferences"]["discovery_leagues"]
                    ),
                },
            )
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

    def completion_command(self, device_id, command):
        def apply(db, device):
            content_id = command.content_id
            row = db.execute("SELECT snapshot FROM contents WHERE id=?", (content_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Content is not in the catalog")
            device["manual_completions"].setdefault(content_id, datetime.now(UTC).isoformat())
            db.execute(
                "UPDATE jobs SET state='superseded' WHERE device_id=? AND content_id=? AND state='pending'",
                (device_id, content_id),
            )
            was_desired = device.get("desired") == content_id
            was_observed = (device.get("observed") or {}).get("content_id") == content_id
            if was_desired:
                device["intent_version"] += 1
                device["desired"] = None
            if was_observed:
                device["observed"] = None
                device["started_at"] = device["last_switch_at"] = None
            if (device.get("recovery") or {}).get("content_id") == content_id:
                device["recovery"] = None
            if device.get("retry_playback") == content_id:
                device["retry_playback"] = None
            if (device.get("next_candidate") or {}).get("content_id") == content_id:
                device["next_candidate"] = None
            if was_desired or was_observed:
                device["reason"] = "Event marked finished manually. Watch plan retained."
                if not device.get("desired"):
                    device["playback_state"] = "waiting"
            self.db.log(
                db,
                self.now(db).isoformat(),
                "Event marked finished",
                json.loads(row["snapshot"])["title"],
                "completion",
                device_id,
            )

        return self.mutate(
            device_id,
            command.command_id,
            command.expected_revision,
            command.model_dump(),
            apply,
            history=(("manual_completions",), "Mark event finished"),
        )

    def automation_command(self, device_id, update):
        def apply(db, d):
            if d.get("manual_control"):
                raise HTTPException(
                    409, "End manual control using the device remote before changing automation."
                )
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
            if request.action == "scenario" and self.db.device(db).get("manual_control"):
                raise HTTPException(409, "End manual control before resetting the demo scenario.")
            if request.action == "advance":
                self.db.set_meta(
                    db, "demo_now", (self.now(db) + timedelta(minutes=request.minutes)).isoformat()
                )
                db.execute("UPDATE content_status SET next_check=0")
                for row in db.execute("SELECT id,snapshot FROM contents").fetchall():
                    snapshot = json.loads(row["snapshot"])
                    switch_at = snapshot.get("_simulation", {}).get("route_switch_at")
                    if switch_at and self.now(db) >= parse_time(switch_at):
                        snapshot["viewing_options"][0].update(
                            decision="excluded", reasons=["simulated_coverage_withdrawn"]
                        )
                        db.execute("UPDATE contents SET snapshot=? WHERE id=?", (encode(snapshot), row["id"]))
            elif request.action in {"disconnect", "reconnect"}:
                self.db.set_meta(db, "simulated_executor_outage", request.action == "disconnect")
                self.db.set_meta(db, "simulated_executor_disconnect_at", None)
                d = self.db.device(db)
                if d.get("executor_health"):
                    d["executor_health"]["next_probe_at"] = None
                self.db.save_device(db, d)
            else:
                self.db.set_meta(db, "simulated_executor_outage", False)
                self.db.set_meta(
                    db,
                    "simulated_executor_disconnect_at",
                    time.time() + 3 if request.scenario == "device_outage" else None,
                )
                db.execute("DELETE FROM content_status")
                entries, now = fixtures(request.scenario)
                self.replace_catalog(db, entries, "demo")
                self.db.set_meta(db, "demo_now", now)
                self.db.set_meta(db, "scenario", request.scenario)
                d = self.db.device(db)
                d.update(
                    {
                        "failures": {},
                        "manual_completions": {},
                        "observed": None,
                        "desired": None,
                        "playback_state": "waiting",
                        "started_at": None,
                        "last_switch_at": None,
                        "force_switch": True,
                        "recovery": None,
                        "executor_health": {"state": "starting"},
                        "retry_playback": None,
                    }
                )
                d["intent_version"] += 1
                d["revision"] += 1
                d["plan"] = [self.entry("demo:canadiens")]
                if request.scenario in {"overlap", "overtime"}:
                    d["plan"].append(self.entry("demo:jays"))
                if request.scenario == "outside_feed":
                    db.execute("UPDATE contents SET active=0 WHERE id='demo:canadiens'")
                if request.scenario == "coverage_switch":
                    d["plan"] = [self.entry("demo:golf")]
                db.execute("UPDATE jobs SET state='superseded' WHERE state='pending'")
                db.execute("DELETE FROM edit_history WHERE device_id=?", (d["id"],))
                self.db.save_device(db, d)
            self.db.log(db, self.now(db).isoformat(), "Simulation updated", request.action, "simulation")
        return {"accepted": True}

    def requested_leagues(self, db):
        leagues = set()
        for row in db.execute("SELECT id FROM devices"):
            leagues.update(self.db.device(db, row["id"])["preferences"]["discovery_leagues"])
        # A discovery setting must not orphan saved commitments or current playback.
        pins = self.pinned_content_ids(db, for_lookup=True)
        for row in db.execute("SELECT id,snapshot FROM contents"):
            if row["id"] in pins:
                snapshot = json.loads(row["snapshot"])
                if snapshot.get("source") == "games":
                    leagues.add(snapshot["competition"])
        return sorted(leagues)

    async def refresh_catalog(self):
        if self.settings.mode != "teamarr":
            return
        try:
            with self.db.transaction() as db:
                leagues = self.requested_leagues(db)
            # Batch extra leagues retained by other devices/commitments within
            # Teamarr's query limit, then publish the complete catalog atomically.
            entries, schema = {}, None
            for group in [leagues[i:i + 20] for i in range(0, len(leagues), 20)] or [[]]:
                page, schema = await self.client.fetch_snapshot(leagues=group)
                entries.update((item["id"], item) for item in page)
            with self.db.transaction() as db:
                if leagues != self.requested_leagues(db):
                    return  # Settings changed in flight; wait for a fresh snapshot.
                self.replace_catalog(db, list(entries.values()), "teamarr")
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
            primary = set(self.requested_leagues(db)) - {"f1"}
            leagues = sorted(primary) + sorted({t["league"] for t in self.db.teams(db)} - primary - {"f1"})[:16]
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
                await self.playback_work(self.tick)
            except Exception:
                log.exception("Controller tick failed; will retry")
            await asyncio.sleep(self.settings.worker_interval)

    async def playback_work(self, function, *args):
        """Drain synchronous adapter work before cancellation releases our DB guard.

        Adapters must impose finite network timeouts; cancelling to_thread cannot
        terminate the underlying call. One worker awaits each tick before the next.
        """
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def run_maintenance(self):
        while True:
            try:
                task = asyncio.create_task(asyncio.to_thread(prune, self.db, self.settings, self.owner))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    # A writer failure during shutdown must not swallow cancellation
                    # and restart the hourly loop while stop() is waiting for it.
                    await asyncio.gather(task, return_exceptions=True)
                    raise
            except Exception as exc:
                log.exception("Database retention failed; will retry")
                with self.db.transaction() as db:
                    health = self.db.meta(db, "maintenance_health", {})
                    health.update(state="error", error=f"{type(exc).__name__}: retention failed")
                    self.db.set_meta(db, "maintenance_health", health)
            await asyncio.sleep(self.settings.maintenance_interval)

    def maintenance_status(self):
        with self.db.transaction() as db:
            return {
                **self.db.meta(db, "maintenance_health", {"state": "starting"}),
                "policy": policy(self.settings),
            }

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
        self.tasks = [
            asyncio.create_task(self.run_worker()),
            asyncio.create_task(self.run_status()),
            asyncio.create_task(self.run_maintenance()),
        ]
        if self.settings.mode == "teamarr":
            self.tasks.append(asyncio.create_task(self.run_feed()))
            self.tasks.append(asyncio.create_task(self.run_team_directory()))

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        try:
            if self.executor:
                await self.executor.close()
            with self.db.transaction() as db:
                db.execute("DELETE FROM leases WHERE owner=?", (self.owner,))
        finally:
            self._database_guard.__exit__(None, None, None)
