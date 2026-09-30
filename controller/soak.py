"""Accelerated, isolated recovery exercise. Never connects to Teamarr or a TV."""

import argparse
import asyncio
import json
import tempfile
import time
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from . import content_status, coordinator, maintenance, ops, playback, recovery, service
from .config import Settings
from .database import encode
from .fixtures import BASE, fixtures
from .models import AutomationUpdate, Command
from .planner import parse_time


class Clock(datetime):
    instant = BASE

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)


def day_catalog(day):
    entries, _ = fixtures()

    def shift(value):
        if isinstance(value, dict):
            return {
                key: (parse_time(item) + timedelta(days=day)).isoformat()
                if key in {"start_time", "expected_end_time", "actual_end_time"} and item
                else shift(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [shift(item) for item in value]
        return value

    for entry in entries:
        entry["id"] += f":day-{day}"
    return [shift(entry) for entry in entries]


async def run_soak(days=30):
    if not 1 <= days <= 365:
        raise ValueError("Use between 1 and 365 simulated days")
    started = time.monotonic()
    counters = {"ticks": 0, "restarts": 0, "restores": 0, "handoffs": 0, "recovery_requests": 0}
    seen_jobs = set()
    removed = {name: 0 for name in maintenance.TABLES}
    missing_heartbeat = False
    original_observe = playback.SimulatedPlaybackAdapter.observe

    def observe(adapter, device_id):
        return None if missing_heartbeat else original_observe(adapter, device_id)

    with tempfile.TemporaryDirectory(prefix="dillflix-soak-") as temporary, ExitStack() as stack:
        Clock.instant = BASE
        for module in (content_status, coordinator, maintenance, ops, playback, recovery, service):
            stack.enter_context(patch.object(module, "datetime", Clock))
        stack.enter_context(patch("time.time", lambda: Clock.instant.timestamp()))
        stack.enter_context(patch.object(playback.SimulatedPlaybackAdapter, "observe", observe))
        settings = Settings(
            database=str(Path(temporary) / "controller.sqlite3"),
            simulation_delay=0,
            job_history_limit=20,
            command_history_limit=40,
            catalog_retention_days=3,
        )
        controller = service.Controller(settings)

        def plan(action):
            d = controller.overview()["device"]
            controller.plan_command(
                d["id"],
                Command(
                    command_id=f"command-{d['revision']}", expected_revision=d["revision"], action=action
                ),
            )

        def automation(mode):
            d = controller.overview()["device"]
            controller.automation_command(
                d["id"],
                AutomationUpdate(
                    command_id=f"command-{d['revision']}", expected_revision=d["revision"], mode=mode
                ),
            )

        def check(expected):
            data = controller.overview()
            d = data["device"]
            assert {p["content_id"] for p in d["plan"]} == expected, "Manual commitment lost"
            if d["playback_state"] == "verified":
                assert d["observed"]["verified"] and d["observed"]["content_id"] == d["desired"]
                assert parse_time(d["observed"]["valid_until"]) > Clock.now(UTC)
                assert d["observed"]["presentation"] == "live"
            with controller.db.transaction() as db:
                assert db.execute("SELECT COUNT(*) FROM jobs WHERE state='pending'").fetchone()[0] <= 1
                for row in db.execute("SELECT id,payload FROM jobs"):
                    if row["id"] not in seen_jobs:
                        seen_jobs.add(row["id"])
                        purpose = json.loads(row["payload"]).get("purpose")
                        counters["handoffs"] += purpose == "route_handoff"
                        counters["recovery_requests"] += purpose == "recovery"

        try:
            for day in range(days):
                Clock.instant = BASE + timedelta(days=day)
                entries = day_catalog(day)
                for entry in list(controller.overview()["device"]["plan"]):
                    plan({"type": "remove", "entry_id": entry["id"]})
                with controller.db.transaction() as db:
                    controller.replace_catalog(db, entries, "demo")
                    controller.db.set_meta(db, "demo_now", Clock.instant.isoformat())
                golf, lions = f"demo:golf:day-{day}", f"demo:lions:day-{day}"
                for content_id in (golf, lions):
                    plan({"type": "add", "content_id": content_id})
                expected = {golf, lions}
                for minute in range(0, 1440, 15):
                    Clock.instant = BASE + timedelta(days=day, minutes=minute)
                    missing_heartbeat = minute in {735, 750}
                    with controller.db.transaction() as db:
                        controller.db.set_meta(db, "demo_now", Clock.instant.isoformat())
                        controller.db.set_meta(db, "simulated_executor_outage", 780 <= minute < 840)
                        for row in db.execute(
                            "SELECT id,snapshot FROM contents WHERE active=1 OR id=?", (golf,)
                        ).fetchall():
                            snapshot = json.loads(row["snapshot"])
                            snapshot["_simulation"]["status_error"] = minute in {960, 975}
                            if row["id"] == golf and minute >= 855:
                                snapshot["viewing_options"][0]["decision"] = "excluded"
                            db.execute(
                                "UPDATE contents SET snapshot=? WHERE id=?", (encode(snapshot), row["id"])
                            )
                        db.execute(
                            "UPDATE contents SET active=? WHERE id=?", (int(not 870 <= minute < 900), golf)
                        )
                    await controller.refresh_status(force=True)
                    if minute == 810 and day % 2:
                        plan({"type": "play_now", "content_id": lions})
                    if minute in {900, 930}:
                        automation("paused" if minute == 900 else "active")
                    if minute == 855:
                        controller.settings = replace(settings, simulation_delay=30)
                    controller.tick()
                    counters["ticks"] += 1
                    check(expected)
                    if minute in {810, 855}:
                        await controller.stop()
                        controller = service.Controller(settings)
                        counters["restarts"] += 1
                        if minute == 855:
                            Clock.instant += timedelta(seconds=35)
                        controller.tick()
                        counters["ticks"] += 1
                        check(expected)
                    if minute % 60 == 0:
                        result = maintenance.prune(controller.db, settings, controller.owner)
                        for name, value in result["removed"].items():
                            removed[name] += value
                if (day + 1) % 7 == 0 or day == days - 1:
                    source = Path(temporary) / f"day-{day}.sqlite3"
                    ops.backup(settings.database, source)
                    await controller.stop()
                    ops.restore(source, settings.database, replace=True)
                    controller = service.Controller(settings)
                    assert controller.overview()["device"]["automation"] == "paused"
                    assert controller.overview()["device"]["observed"] is None
                    check(expected)
                    automation("active")
                    counters["restores"] += 1
            result = maintenance.prune(controller.db, settings, controller.owner)
            final_counts = result["counts"]
            assert final_counts["jobs"] <= settings.job_history_limit + 2
            assert final_counts["commands"] <= settings.command_history_limit + 100
            assert final_counts["contents"] <= 125
            assert final_counts["activity"] <= 2000
            assert counters["handoffs"] > 0 and counters["recovery_requests"] > 0
            ops.verify(settings.database)
            return {
                "ok": True,
                "accelerated": True,
                "simulated_days": days,
                "wall_seconds": round(time.monotonic() - started, 3),
                **counters,
                "playback_requests": len(seen_jobs),
                "removed": removed,
                "final_counts": final_counts,
                "note": "Isolated simulator with accelerated clocks; not a real-time endurance or device test.",
            }
        finally:
            await controller.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_soak(args.days)), indent=2))


if __name__ == "__main__":
    main()
