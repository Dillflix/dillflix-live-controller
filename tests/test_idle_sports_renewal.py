"""Only new native guide evidence renews idle availability across handoff/monitor."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import time

import pytest

from controller.database import encode
from controller.planner import content_view
from controller.prime_player.catalogue import key, signature, renew_idle_evidence
from test_idle_sports_integration import candidate, seed, EventPlayer
from test_prime_linear import LinearPlayer
from test_prime_workflow import cleanup, rig, row


def player(workflow, request, value):
    workflow.player = EventPlayer(value) if value["kind"] == "event" else LinearPlayer(request)
    if value["kind"] == "linear_program":
        workflow.player.guide["station_summary"]["stations"][0]["airings"][0]["synopsis"] = value["synopsis"]


def expire_source(controller, cid):
    old = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
    with controller.db.transaction() as db:
        snapshot = json.loads(db.execute("SELECT snapshot FROM contents WHERE id=?", (cid,)).fetchone()[0])
        snapshot["prime_idle"]["observed_at"] = old
        db.execute("UPDATE contents SET snapshot=?,seen_at=? WHERE id=?", (encode(snapshot), old, cid))
        device = controller.db.device(db)
        device["idle_sports"]["expires_at"] = time.time() - 1
        controller.db.save_device(db, device)
    return old


def stored(controller, cid):
    with controller.db.snapshot() as db:
        return controller.db.device(db), json.loads(db.execute("SELECT snapshot FROM contents WHERE id=?", (cid,)).fetchone()[0])


@pytest.mark.parametrize("kind", ["event", "linear_program"])
async def test_preflight_survives_old_nomination_expiry_and_hands_off_refreshed_evidence_once(tmp_path, kind):
    controller, workflow = rig(tmp_path)
    try:
        value = candidate(kind)
        request = seed(controller, value)
        player(workflow, request, value)
        controller.stage_playback()
        with controller.db.snapshot() as db:
            probe = controller.db.meta(db, key(controller.device_id))
        old = expire_source(controller, value["candidate_id"])
        controller.stage_playback()
        with controller.db.snapshot() as db:
            assert not db.execute("SELECT 1 FROM jobs WHERE state='pending'").fetchone()
            assert workflow.catalogue_current(probe)
        await workflow.observe_catalogue(probe)
        device, snapshot = stored(controller, value["candidate_id"])
        assert snapshot["prime_idle"]["observed_at"] != old
        assert device["idle_sports"]["expires_at"] > time.time()
        controller.stage_playback()
        with controller.db.snapshot() as db:
            job = dict(db.execute("SELECT * FROM jobs WHERE state='pending'").fetchone())
            body = json.loads(job["payload"])
            body["deadline_at"] = datetime.fromtimestamp(job["deadline_at"], UTC).isoformat()
        report, _ = workflow.store.submit(body)
        await workflow.navigate(row(workflow, report["token"]))
        assert workflow.store.report(report["token"])["operation"]["state"] == "playing_verified"
        assert len([c for c in workflow.player.calls if c[0] == "play"]) == 1
        # A changed observation timestamp does not discard the prepared match.
        assert workflow.store.report(report["token"])["prime_player"]["catalogue_probe_id"] == probe["id"]
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("kind", ["event", "linear_program"])
async def test_monitor_renews_source_and_nomination_without_replaying_or_extending_request(tmp_path, kind):
    controller, workflow = rig(tmp_path)
    try:
        value = candidate(kind)
        request = seed(controller, value)
        player(workflow, request, value)
        report, _ = workflow.store.submit(request)
        token = report["token"]
        await workflow.navigate(row(workflow, token))
        before = row(workflow, token)["request"]
        old = expire_source(controller, value["candidate_id"])
        await workflow.monitor(row(workflow, token))
        device, snapshot = stored(controller, value["candidate_id"])
        assert snapshot["prime_idle"]["observed_at"] != old
        assert device["idle_sports"]["expires_at"] > time.time()
        assert row(workflow, token)["request"] == before
        assert workflow.store.report(token)["observation"]["verified"]
        assert len([c for c in workflow.player.calls if c[0] == "play"]) == 1
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("case", ["stale", "changed_program", "changed_genre", "manual", "changed_nomination", "slot_ended"])
async def test_invalid_native_recheck_never_renews_old_source(tmp_path, case):
    controller, workflow = rig(tmp_path)
    try:
        value = candidate()
        request = seed(controller, value)
        player(workflow, request, value)
        guide = deepcopy(workflow.player.guide)
        from controller.prime_player.linear import choose_airing
        selected = choose_airing(request, guide)[0]
        before = stored(controller, value["candidate_id"])[1]
        if case == "stale":
            guide["observed_at"] = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
        if case == "changed_program":
            guide["station_summary"]["stations"][0]["airings"][0]["synopsis"] = "Movie"
        if case == "changed_genre":
            guide["station_summary"]["stations"][0]["airings"][0]["genres"] = ["Fiction"]
        if case == "slot_ended":
            guide["station_summary"]["stations"][0]["airings"][0]["ends_at"] = datetime.now(UTC).isoformat()
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            if case == "manual": device["manual_control"] = {"session": "remote"}
            if case == "changed_nomination": device["idle_sports"]["content_id"] = "replacement"
            assert renew_idle_evidence(db, controller.db, device, request, guide, selected) is None
        assert stored(controller, value["candidate_id"])[1] == before
    finally:
        await cleanup(controller)


def test_signature_ignores_only_native_observation_age():
    value = candidate()
    from controller.idle_sports import content_snapshot
    item = content_view(content_snapshot(value), True, {"state": "live"})
    device = {"revision": 1, "intent_version": 1}
    original = signature(device, item)
    item["snapshot"]["prime_idle"]["observed_at"] = datetime.now(UTC).isoformat()
    assert signature(device, item) == original
    item["snapshot"]["prime_idle"]["synopsis"] = "Different program"
    assert signature(device, item) != original


@pytest.mark.parametrize("case", ["accepted", "expired_deadline", "unaccepted", "slot_ended", "disabled", "manual"])
def test_accepted_idle_hold_is_bounded_and_never_reopens_playback(case):
    from controller.coordinator import PlaybackCoordinator
    from controller.idle_sports import content_snapshot
    value = candidate()
    item = content_view(content_snapshot(value), True, {"state": "unknown"})
    device = {"intent_version": 3, "desired": value["candidate_id"], "automation": "active",
              "preferences": {"idle_sports_enabled": True}, "idle_sports": {
                  "state": "ready", "content_id": value["candidate_id"], "expires_at": time.time() - 1}}
    job = {"state": "pending", "executor_job_id": "accepted-token", "intent": 3,
           "content_id": value["candidate_id"], "deadline_at": time.time() + 30,
           "payload": json.dumps({"allowed_viewing_options": item["viewing_options"]})}
    if case == "expired_deadline": job["deadline_at"] = time.time() - 1
    if case == "unaccepted": job["executor_job_id"] = None
    if case == "slot_ended": item["expected_end_time"] = datetime.now(UTC).isoformat()
    if case == "disabled": device["preferences"]["idle_sports_enabled"] = False
    if case == "manual": device["manual_control"] = {"session": "remote"}
    assert PlaybackCoordinator.keep_pending_launch(device, job, item) is (case == "accepted")


@pytest.mark.parametrize("preempt", ["schedule", "manual"])
async def test_accepted_idle_request_survives_expiry_but_yields_to_new_intent(tmp_path, monkeypatch, preempt):
    controller, workflow = rig(tmp_path)
    try:
        value = candidate()
        request = seed(controller, value)
        player(workflow, request, value)
        controller.stage_playback()
        with controller.db.snapshot() as db:
            probe = controller.db.meta(db, key(controller.device_id))
        await workflow.observe_catalogue(probe)
        controller.stage_playback()
        with controller.db.transaction() as db:
            job = dict(db.execute("SELECT * FROM jobs WHERE state='pending'").fetchone())
            db.execute("UPDATE jobs SET executor_job_id='accepted-token' WHERE id=?", (job["id"],))
        expire_source(controller, value["candidate_id"])
        controller.stage_playback()
        with controller.db.snapshot() as db:
            retained = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())
            assert retained["state"] == "pending" and retained["deadline_at"] == job["deadline_at"]
            assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
            items = controller.items(db)
        if preempt == "manual":
            with controller.db.transaction() as db:
                device = controller.db.device(db)
                device["manual_control"] = {"session_id": "remote", "expires_at": time.time() + 60}
                controller.db.save_device(db, device)
        else:
            ordinary = deepcopy(next(item for item in items if item["content_id"] == value["candidate_id"]))
            ordinary.update(content_id="scheduled-now", source="games", kind="event", active=True,
                            playable=True, lifecycle={"state": "live"}, entitlement={"status": "entitled"})
            ordinary["snapshot"].update(id="scheduled-now", source="games", kind="event")
            monkeypatch.setattr(controller, "items", lambda *args, **kwargs: items + [ordinary])
        assert not controller.receive_playback_report(job["id"], {
            "state": "accepted", "request_id": job["id"], "device_id": job["device_id"],
            "intent_version": job["intent"], "content_id": job["content_id"], "executor_job_id": "accepted-token",
        })
        with controller.db.snapshot() as db:
            assert db.execute("SELECT state FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] == "superseded"
    finally:
        await cleanup(controller)
