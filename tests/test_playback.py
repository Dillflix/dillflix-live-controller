import copy
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_controller import command, overview

from controller.database import encode
from controller.service import Controller


def jobs(client):
    return client.get("/api/v1/devices/living-room/jobs").json()["items"]


def pending(rig):
    c, s, settings = rig
    object.__setattr__(settings, "simulation_delay", 100)
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    return jobs(c)[0]


def successful_report(service, job):
    service.playback.submit(job["payload"])
    return service.playback.inspect(job["id"])


def issued_launch_with_unknown_status(rig, monkeypatch):
    _, service, _ = rig
    job = pending(rig)
    success = successful_report(service, job)
    assert service.receive_playback_report(job["id"], {**success, "state": "navigating", "observation": None})
    with service.db.transaction() as db:
        item = next(i for i in service.items(db) if i["content_id"] == job["content_id"])
    item = copy.deepcopy(item)
    item["playable"] = False
    item["lifecycle"]["state"] = "unknown"
    monkeypatch.setattr(service, "items", lambda *args, **kwargs: [item])
    return job, success, item


def test_issued_launch_survives_unknown_status_and_accepts_verified_result(rig, monkeypatch):
    client, service, _ = rig
    job, success, _ = issued_launch_with_unknown_status(rig, monkeypatch)
    service.stage_playback()
    retained = jobs(client)[0]
    assert retained["id"] == job["id"] and retained["state"] == "pending"
    assert retained["deadline_at"] == job["deadline_at"]
    assert service.receive_playback_report(job["id"], success)
    assert jobs(client)[0]["state"] == "verified"
    assert len(jobs(client)) == 1


@pytest.mark.parametrize(
    "change", ["ended", "cancelled", "route_withdrawn", "expired", "new_intent", "queued"]
)
def test_unknown_status_does_not_preserve_invalid_launch(rig, monkeypatch, change):
    client, service, _ = rig
    job, success, item = issued_launch_with_unknown_status(rig, monkeypatch)
    if change in {"ended", "cancelled"}:
        item["lifecycle"]["state"] = change
    elif change == "route_withdrawn":
        item["viewing_options"] = []
    else:
        with service.db.transaction() as db:
            if change == "expired":
                db.execute("UPDATE jobs SET deadline_at=0 WHERE id=?", (job["id"],))
            elif change == "queued":
                db.execute("UPDATE jobs SET progress='queued' WHERE id=?", (job["id"],))
            else:
                device = service.db.device(db)
                device["intent_version"] += 1
                service.db.save_device(db, device)
    assert not service.receive_playback_report(job["id"], success)
    assert jobs(client)[0]["state"] != "verified"


def test_unknown_status_never_substitutes_for_playback_proof(rig, monkeypatch):
    client, service, _ = rig
    job, success, _ = issued_launch_with_unknown_status(rig, monkeypatch)
    success["observation"]["verified"] = False
    service.receive_playback_report(job["id"], success)
    assert jobs(client)[0]["state"] == "rejected"


def test_lost_ack_is_reconciled_without_duplicate_delivery(rig, monkeypatch):
    c, s, _ = rig
    submit = s.playback.submit

    def lose_ack(request):
        submit(request)
        raise TimeoutError("acknowledgement lost after acceptance")

    monkeypatch.setattr(s.playback, "submit", lose_ack)
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    job = jobs(c)[0]
    assert job["progress"] == "retrying"
    assert overview(c)["device"]["observed"] is None
    with s.db.transaction() as db:
        db.execute("UPDATE jobs SET ready_at=0 WHERE id=?", (job["id"],))
    s.tick()
    assert jobs(c)[0]["delivery_attempts"] == 1
    assert overview(c)["device"]["observed"]["request_id"] == job["id"]
    with s.db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM simulated_jobs").fetchone()[0] == 1


async def test_restart_adopts_playback_verified_before_the_coordinator_saved_it(rig):
    c, first, settings = rig
    job = pending(rig)
    successful_report(first, job)
    await first.stop()
    restarted = Controller(settings)
    with restarted.db.transaction() as db:
        db.execute("UPDATE jobs SET ready_at=0 WHERE id=?", (job["id"],))
        # Old job success must be accompanied by a fresh device observation.
        old = json.loads(
            db.execute("SELECT observation FROM simulated_jobs WHERE id=?", (job["id"],)).fetchone()[0]
        )
        old["observed_at"] = old["valid_until"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        db.execute("UPDATE simulated_jobs SET observation=? WHERE id=?", (encode(old), job["id"]))
    restarted.tick()
    assert restarted.overview()["device"]["observed"]["request_id"] == job["id"]
    assert len(jobs(c)) == 1


def test_navigation_deadline_keeps_manual_intent_and_uses_live_fallback(rig):
    c, s, settings = rig
    job = pending(rig)
    request = job["payload"]
    request["content_snapshot"]["_simulation"]["stall_navigation"] = True
    with s.db.transaction() as db:
        db.execute("UPDATE jobs SET payload=?,ready_at=0 WHERE id=?", (encode(request), job["id"]))
    s.tick()
    assert jobs(c)[0]["progress"] == "navigating"
    assert overview(c)["device"]["observed"] is None
    with s.db.transaction() as db:
        db.execute("UPDATE jobs SET deadline_at=0 WHERE id=?", (job["id"],))
    s.tick()
    assert jobs(c)[0]["state"] == "timed_out"
    assert s.playback.inspect(job["id"])["state"] == "cancelled"
    object.__setattr__(settings, "simulation_delay", 0)
    s.tick()
    d = overview(c)["device"]
    assert d["observed"]["content_id"] == "demo:redzone"
    assert d["plan"][0]["content_id"] == "demo:lions"


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_id", "wrong-event"),
        ("device_id", "another-tv"),
        ("request_id", "old-request"),
        ("intent_version", -1),
        ("presentation", "replay"),
        ("viewing_option_id", "excluded-route"),
        ("health", "buffering"),
        ("verified", False),
        ("observed_at", "2000-01-01T00:00:00Z"),
        ("observed_at", "2999-01-01T00:00:00Z"),
        ("valid_until", None),
    ],
)
def test_invalid_observation_never_becomes_verified_playback(rig, field, value):
    c, s, _ = rig
    job = pending(rig)
    report = successful_report(s, job)
    report["observation"][field] = value
    assert not s.receive_playback_report(job["id"], report)
    assert jobs(c)[0]["state"] == "rejected"
    assert overview(c)["device"]["observed"] is None
    assert overview(c)["device"]["plan"][0]["content_id"] == "demo:lions"


def test_late_result_after_a_new_manual_command_is_rejected_before_next_tick(rig):
    c, s, _ = rig
    job = pending(rig)
    report = successful_report(s, job)
    command(c, {"type": "play_now", "content_id": "demo:golf"})
    assert not s.receive_playback_report(job["id"], report)
    assert overview(c)["device"]["observed"] is None
    assert jobs(c)[0]["state"] == "superseded"


def test_pause_rejects_late_results_and_preserves_newer_playback(rig):
    c, s, _ = rig
    job = pending(rig)
    report = successful_report(s, job)
    d = overview(c)["device"]
    c.post(
        "/api/v1/devices/living-room/automation",
        json={"command_id": str(uuid4()), "expected_revision": d["revision"], "mode": "paused"},
    )
    assert not s.receive_playback_report(job["id"], report)
    s.tick()
    assert jobs(c)[0]["cancel_sent"] == 1
    assert overview(c)["device"]["observed"] is None


def test_expired_observation_is_not_refreshed_without_executor_evidence(rig, monkeypatch):
    c, s, _ = rig
    s.tick()
    observe = s.playback.observe
    with s.db.transaction() as db:
        d = s.db.device(db)
        d["observed"]["observed_at"] = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        d["observed"]["valid_until"] = (datetime.now(UTC) - timedelta(minutes=4)).isoformat()
        s.db.save_device(db, d)
    monkeypatch.setattr(s.playback, "observe", lambda _: None)
    s.tick()
    stale = overview(c)["device"]
    assert stale["playback_state"] == "unverified"
    assert stale["observed"]["verified"] is False
    assert stale["observed"]["observed_at"] == d["observed"]["observed_at"]
    monkeypatch.setattr(s.playback, "observe", observe)
    s.tick()
    assert overview(c)["device"]["playback_state"] == "verified"
    assert len(jobs(c)) == 1


def test_lost_lease_cannot_accept_a_result(rig):
    c, s, _ = rig
    job = pending(rig)
    report = successful_report(s, job)
    with s.db.transaction() as db:
        db.execute("UPDATE leases SET owner='other-coordinator',expires=?", (time.time() + 60,))
    assert not s.receive_playback_report(job["id"], report)
    assert overview(c)["device"]["observed"] is None


def test_simulator_fences_old_intent_and_cancel_does_not_stop_new_content(rig):
    _, s, _ = rig
    job = pending(rig)
    s.playback.submit(job["payload"])
    newer = copy.deepcopy(job["payload"])
    newer.update(request_id=str(uuid4()), intent_version=job["intent"] + 1)
    s.playback.submit(newer)
    assert s.playback.inspect(job["id"])["state"] == "superseded"
    s.playback.inspect(newer["request_id"])
    s.playback.cancel(job["id"])
    assert s.playback.observe("living-room")["request_id"] == newer["request_id"]
    assert s.playback.submit(newer)["state"] == "playing_verified"


def test_cancel_before_submit_remains_cancelled_across_restart(rig):
    _, s, settings = rig
    job = pending(rig)
    s.playback.cancel(job["id"])
    restarted = Controller(settings)
    assert restarted.playback.submit(job["payload"])["state"] == "cancelled"
    assert restarted.playback.inspect(job["id"])["observation"] is None


async def test_unacknowledged_cancellation_is_retried_after_restart(rig, monkeypatch):
    c, s, settings = rig
    job = pending(rig)
    s.playback.submit(job["payload"])

    def unavailable(_):
        raise ConnectionError("Executor temporarily unavailable")

    monkeypatch.setattr(s.playback, "cancel", unavailable)
    c.post(
        "/api/v1/devices/living-room/automation",
        json={
            "command_id": str(uuid4()),
            "expected_revision": overview(c)["device"]["revision"],
            "mode": "paused",
        },
    )
    s.tick()
    assert jobs(c)[0]["cancel_sent"] == 0
    await s.stop()
    restarted = Controller(settings)
    restarted.tick()
    assert jobs(c)[0]["cancel_sent"] == 1
    assert restarted.playback.inspect(job["id"])["state"] == "cancelled"
    assert overview(c)["device"]["plan"][0]["content_id"] == "demo:lions"


def test_approved_switch_is_not_reversed_by_the_previous_events_dwell_timer(rig):
    c, s, settings = rig
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    d = overview(c)["device"]
    command(c, {"type": "remove", "entry_id": d["plan"][0]["id"]})
    object.__setattr__(settings, "simulation_delay", 100)
    s.tick()
    job = jobs(c)[0]
    assert overview(c)["device"]["desired"] == "demo:redzone"
    s.tick()
    assert overview(c)["device"]["desired"] == "demo:redzone"
    with s.db.transaction() as db:
        db.execute("UPDATE jobs SET ready_at=0 WHERE id=?", (job["id"],))
    s.tick()
    assert overview(c)["device"]["observed"]["content_id"] == "demo:redzone"
    assert jobs(c)[0]["id"] == job["id"]


def test_pause_does_not_reverify_an_expired_observation(rig, monkeypatch):
    c, s, _ = rig
    s.tick()
    with s.db.transaction() as db:
        d = s.db.device(db)
        d["observed"]["observed_at"] = d["observed"]["valid_until"] = "2000-01-01T00:00:00Z"
        s.db.save_device(db, d)
    monkeypatch.setattr(s.playback, "observe", lambda _: None)
    s.tick()
    c.post(
        "/api/v1/devices/living-room/automation",
        json={
            "command_id": str(uuid4()),
            "expected_revision": overview(c)["device"]["revision"],
            "mode": "paused",
        },
    )
    s.tick()
    assert overview(c)["device"]["playback_state"] == "unverified"
    assert overview(c)["device"]["observed"]["verified"] is False


def test_v2_upgrade_adopts_existing_simulated_playback_and_preserves_settings(rig):
    c, s, settings = rig
    s.tick()
    before = overview(c)["device"]
    with s.db.transaction() as db:
        for table in ("simulated_jobs", "simulated_devices", "simulated_cancellations"):
            db.execute(f"DROP TABLE {table}")
        for col in ("deadline_at", "executor_job_id", "progress", "cancel_sent", "delivery_attempts"):
            db.execute(f"ALTER TABLE jobs DROP COLUMN {col}")
        db.execute("PRAGMA user_version=2")
    restarted = Controller(settings)
    assert restarted.overview()["device"] == before
    observed = restarted.playback.observe("living-room")
    assert observed["content_id"] == before["observed"]["content_id"]
    assert observed["presentation"] == "live"
    assert len(jobs(c)) == 1
