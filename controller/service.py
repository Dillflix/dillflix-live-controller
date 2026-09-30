import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from .database import Database, encode
from .fixtures import default_device, fixtures
from .planner import choose, content_view, parse_time, preview_plan, priority
from .teamarr import TeamarrClient

log = logging.getLogger(__name__)


class Controller:
    def __init__(self, settings):
        self.settings = settings
        self.db = Database(settings.database)
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
            return {
                "device": device,
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

    def mutate(self, device_id, command_id, revision, payload, apply):
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
            apply(db, device)
            device["revision"] += 1
            self.db.save_device(db, device)
            result = {"command_id": command_id, "revision": device["revision"], "accepted": True}
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)", (device_id, command_id, digest, encode(result))
            )
            return result

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
        return self.mutate(
            device_id,
            command.command_id,
            command.expected_revision,
            payload,
            lambda db, d: self.apply_plan(db, d, payload["action"]),
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
        data = update.model_dump()

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

        return self.mutate(device_id, update.command_id, update.expected_revision, data, apply)

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
                d["playback_state"] = "verified" if d.get("observed") else "waiting"
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

    def tick(self):
        real = datetime.now(UTC)
        with self.db.transaction() as db:
            if not self.db.lease(db, "device:living-room", self.owner, time.time()):
                return
            d = self.db.device(db)
            if d["automation"] == "paused":
                return
            items = self.items(db)
            indexed = {i["content_id"]: i for i in items}
            now = self.now(db)
            # Reevaluate before accepting a pending observation so schedule/user changes fence it.
            decision = choose(d, items, now, real)
            d["force_switch"] = False
            target = decision["content_id"]
            d["reason"] = decision["reason"]
            d["next_candidate"] = decision.get("next_candidate")
            if target != d.get("desired") or (target and d["playback_state"] in {"waiting", "failed"}):
                d["intent_version"] += 1
                d["desired"] = target
                db.execute(
                    "UPDATE jobs SET state='superseded' WHERE device_id=? AND state='pending'", (d["id"],)
                )
                if target:
                    item = indexed[target]
                    request_id = str(uuid.uuid4())
                    payload = {
                        "schema_version": 1,
                        "request_id": request_id,
                        "device_id": d["id"],
                        "intent_version": d["intent_version"],
                        "content_id": target,
                        "mode": "live",
                        "content_snapshot_schema_version": 1,
                        "content_snapshot": item["snapshot"],
                        "allowed_viewing_options": item["viewing_options"],
                    }
                    db.execute(
                        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,NULL)",
                        (
                            request_id,
                            d["id"],
                            d["intent_version"],
                            target,
                            "pending",
                            time.time() + self.settings.simulation_delay,
                            encode(payload),
                        ),
                    )
                    d["playback_state"] = "navigating"
                    self.db.log(
                        db, now.isoformat(), f"Opening {item['title']}", decision["reason"], "navigation"
                    )
                else:
                    d["playback_state"] = "waiting"
                    d["observed"] = None
                    self.db.log(db, now.isoformat(), "Waiting for live sports", decision["reason"])
            for job in db.execute(
                "SELECT * FROM jobs WHERE device_id=? AND state='pending' AND ready_at<=?",
                (d["id"], time.time()),
            ).fetchall():
                if job["intent"] != d["intent_version"] or job["content_id"] != d["desired"]:
                    db.execute("UPDATE jobs SET state='superseded' WHERE id=?", (job["id"],))
                    continue
                payload = json.loads(job["payload"])
                item = indexed[job["content_id"]]
                if item["snapshot"].get("_simulation", {}).get("fail_playback"):
                    prior = d["failures"].get(job["content_id"], {})
                    attempts = prior.get("attempts", 0) + 1
                    wait = 5 if attempts == 1 else 15 if attempts == 2 else 300
                    d["failures"][job["content_id"]] = {
                        "attempts": attempts,
                        "retry_after": (real + timedelta(seconds=wait)).isoformat(),
                        "reason": "Simulated route failure",
                    }
                    db.execute(
                        "UPDATE jobs SET state='failed',error=? WHERE id=?",
                        ("All simulated routes failed", job["id"]),
                    )
                    d["playback_state"] = "failed"
                    self.db.log(
                        db,
                        now.isoformat(),
                        f"Playback failed · {item['title']}",
                        f"Manual commitment retained. Retry after {wait} seconds.",
                        "failure",
                    )
                else:
                    # This adapter never performs Fire TV I/O. Unknown observations are not real verification.
                    d["observed"] = {
                        "content_id": job["content_id"],
                        "presentation": "live",
                        "verified": True,
                        "simulated": True,
                        "observed_at": real.isoformat(),
                        "viewing_option_id": payload["allowed_viewing_options"][0]["id"],
                        "request_id": job["id"],
                        "intent_version": job["intent"],
                    }
                    d["playback_state"] = "verified"
                    d["started_at"] = now.isoformat()
                    d["last_switch_at"] = real.isoformat()
                    d["failures"].pop(job["content_id"], None)
                    db.execute("UPDATE jobs SET state='verified' WHERE id=?", (job["id"],))
                    self.db.log(
                        db,
                        now.isoformat(),
                        f"Simulated live playback · {item['title']}",
                        "Requested content matched the simulated observation",
                        "verified",
                    )
            if d.get("observed") and d["playback_state"] == "verified":
                d["observed"]["observed_at"] = real.isoformat()
            self.db.save_device(db, d)

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

    def start(self):
        self.tasks = [asyncio.create_task(self.run_worker())]
        if self.settings.mode == "teamarr":
            self.tasks.append(asyncio.create_task(self.run_feed()))

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        with self.db.transaction() as db:
            db.execute("DELETE FROM leases WHERE owner=?", (self.owner,))
