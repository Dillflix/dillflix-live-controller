import asyncio
import json
import threading
from uuid import uuid4

from test_controller import command, overview
from test_playback import jobs

from controller.database import encode
from controller.maintenance import counts, prune
from controller.storage_lock import database_guard


def small_limits(settings):
    object.__setattr__(settings, "job_history_limit", 2)
    object.__setattr__(settings, "command_history_limit", 3)
    object.__setattr__(settings, "catalog_retention_days", 1)


def make_jobs(c, s, count=8):
    for number in range(count):
        command(c, {"type": "play_now", "content_id": "demo:golf" if number % 2 else "demo:lions"})
        s.tick()


def test_retention_bounds_history_and_old_playback_cannot_return(rig):
    c, s, settings = rig
    small_limits(settings)
    make_jobs(c, s)
    old = jobs(c)[-1]
    before = overview(c)["device"]
    result = prune(s.db, settings, s.owner)
    assert result["counts"]["jobs"] == 2
    assert result["counts"]["simulated_jobs"] == 2
    assert result["removed"]["jobs"] == 6
    assert overview(c)["device"] == before
    assert s.playback.submit(old["payload"])["state"] == "superseded"
    s.tick()
    assert overview(c)["device"]["observed"]["request_id"] == before["observed"]["request_id"]
    prune(s.db, settings, s.owner)
    assert s.playback.inspect(old["id"]) is None
    response = c.get("/api/v1/maintenance")
    assert response.status_code == 200
    assert response.json()["counts"]["jobs"] == 2


def test_pending_and_unacknowledged_cancellation_are_never_pruned(rig):
    c, s, settings = rig
    small_limits(settings)
    make_jobs(c, s)
    old = jobs(c)[-1]
    with s.db.transaction() as db:
        db.execute("UPDATE jobs SET state='failed',cancel_sent=0 WHERE id=?", (old["id"],))
    object.__setattr__(settings, "simulation_delay", 100)
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.stage_playback()
    pending = jobs(c)[0]
    assert pending["state"] == "pending"
    observed_request = overview(c)["device"]["observed"]["request_id"]
    prune(s.db, settings, s.owner)
    retained = {j["id"] for j in jobs(c)}
    assert {old["id"], pending["id"], observed_request} <= retained
    assert next(j for j in jobs(c) if j["id"] == old["id"])["cancel_sent"] == 0


def test_highest_intent_and_cancel_before_submit_remain_fenced_after_cleanup(rig):
    c, s, settings = rig
    small_limits(settings)
    object.__setattr__(settings, "job_history_limit", 1)
    make_jobs(c, s, 2)
    object.__setattr__(settings, "simulation_delay", 100)
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    old = jobs(c)[0]
    for mode in ("paused", "active"):
        d = overview(c)["device"]
        c.post(
            "/api/v1/devices/living-room/automation",
            json={"command_id": str(uuid4()), "expected_revision": d["revision"], "mode": mode},
        )
        if mode == "paused":
            s.tick()
    # Newest cancelled intent has not reached the simulator: its tombstone matters.
    prune(s.db, settings, s.owner)
    with s.db.transaction() as db:
        assert db.execute("SELECT 1 FROM simulated_cancellations WHERE id=?", (old["id"],)).fetchone()
    object.__setattr__(settings, "simulation_delay", 0)
    s.tick()
    current = overview(c)["device"]["observed"]
    prune(s.db, settings, s.owner)
    with s.db.transaction() as db:
        assert not db.execute("SELECT 1 FROM simulated_cancellations WHERE id=?", (old["id"],)).fetchone()
    assert s.playback.submit(old["payload"])["state"] == "superseded"
    s.tick()
    assert overview(c)["device"]["observed"]["request_id"] == current["request_id"]


def test_retained_receipt_retries_and_pruned_receipt_is_a_stale_revision(rig):
    c, s, settings = rig
    small_limits(settings)
    requests = []
    for number in range(10):
        body = {
            "command_id": str(uuid4()),
            "expected_revision": overview(c)["device"]["revision"],
            "mode": "paused" if number % 2 else "active",
        }
        response = c.post("/api/v1/devices/living-room/automation", json=body)
        assert response.status_code == 200
        requests.append((body, response.json()))
    result = prune(s.db, settings, s.owner)
    assert result["counts"]["commands"] == 3
    before = overview(c)["device"]
    newest = c.post("/api/v1/devices/living-room/automation", json=requests[-1][0])
    assert newest.json() == requests[-1][1]
    old = c.post("/api/v1/devices/living-room/automation", json=requests[0][0])
    assert old.status_code == 409
    assert overview(c)["device"] == before


def test_old_catalog_is_retained_for_plan_and_undo_without_manufacturing_completion(rig):
    c, s, settings = rig
    small_limits(settings)
    command(c, {"type": "add", "content_id": "demo:jays"})
    entry = next(p for p in overview(c)["device"]["plan"] if p["content_id"] == "demo:jays")
    command(c, {"type": "remove", "entry_id": entry["id"]})
    before = overview(c)["device"]
    with s.db.transaction() as db:
        db.execute("UPDATE contents SET active=0,seen_at='2000-01-01T00:00:00Z'")
    result = prune(s.db, settings, s.owner)
    with s.db.transaction() as db:
        ids = {r[0] for r in db.execute("SELECT id FROM contents")}
        assert ids == {"demo:canadiens", "demo:jays"}
    assert result["removed"]["contents"] == 5
    assert overview(c)["device"] == before
    undo = overview(c)["undo"]
    response = c.post(
        "/api/v1/devices/living-room/undo",
        json={"command_id": str(uuid4()), "expected_revision": before["revision"], "history_id": undo["id"]},
    )
    assert response.status_code == 200
    assert "demo:jays" in {p["content_id"] for p in overview(c)["device"]["plan"]}


def test_undo_receipts_and_referenced_content_survive_small_receipt_limit(rig):
    c, s, settings = rig
    small_limits(settings)
    object.__setattr__(settings, "command_history_limit", 1)
    receipt = command(c, {"type": "add", "content_id": "demo:jays"}).json()
    for _ in range(5):
        d = overview(c)["device"]
        c.post(
            "/api/v1/devices/living-room/automation",
            json={"command_id": str(uuid4()), "expected_revision": d["revision"], "mode": "active"},
        )
    prune(s.db, settings, s.owner)
    with s.db.transaction() as db:
        assert db.execute("SELECT 1 FROM commands WHERE id=?", (receipt["command_id"],)).fetchone()
        assert counts(db)["commands"] == 2


def test_current_simulator_observation_is_retained_during_unacknowledged_switch(rig):
    c, s, settings = rig
    small_limits(settings)
    make_jobs(c, s, 6)
    current = overview(c)["device"]["observed"]
    first = jobs(c)[-1]
    # Simulate a separate executor still observing an older request while later
    # coordinator history exists. Its verification payload must remain available.
    with s.db.transaction() as db:
        evidence = json.loads(
            db.execute("SELECT observation FROM simulated_jobs WHERE id=?", (first["id"],)).fetchone()[0]
        )
        db.execute("UPDATE simulated_devices SET observation=?", (encode(evidence),))
    prune(s.db, settings, s.owner)
    assert {first["id"], current["request_id"]} <= {j["id"] for j in jobs(c)}
    assert s.playback.inspect(first["id"]) is not None


def test_other_maintenance_owner_prevents_cleanup(rig):
    _, s, settings = rig
    with s.db.transaction() as db:
        db.execute("INSERT INTO leases VALUES ('maintenance','another',9999999999)")
    assert prune(s.db, settings, s.owner) is None


async def test_shutdown_drains_a_failed_retention_write_and_releases_restore_guard(rig, monkeypatch):
    _, s, settings = rig
    entered, release = threading.Event(), threading.Event()

    def failing_cleanup(*_):
        entered.set()
        assert release.wait(2)
        raise RuntimeError("Simulated disk error during shutdown")

    monkeypatch.setattr("controller.service.prune", failing_cleanup)
    s.tasks = [asyncio.create_task(s.run_maintenance())]
    assert await asyncio.to_thread(entered.wait, 1)
    stopping = asyncio.create_task(s.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    await asyncio.wait_for(stopping, timeout=1)
    with database_guard(settings.database, exclusive=True):
        pass
