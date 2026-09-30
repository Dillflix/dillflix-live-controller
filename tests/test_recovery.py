import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_controller import command, overview, scenario
from test_playback import jobs

from controller.database import encode
from controller.planner import parse_time
from controller.service import Controller


def edit_snapshot(service, content_id, edit):
    with service.db.transaction() as db:
        item = json.loads(db.execute("SELECT snapshot FROM contents WHERE id=?", (content_id,)).fetchone()[0])
        edit(item)
        db.execute("UPDATE contents SET snapshot=? WHERE id=?", (encode(item), content_id))
    return item


def edit_device(service, edit):
    with service.db.transaction() as db:
        device = service.db.device(db)
        edit(device)
        service.db.save_device(db, device)
    return device


def expire_observation(service):
    old = datetime.now(UTC) - timedelta(minutes=1)
    edit_device(
        service,
        lambda d: d["observed"].update(
            observed_at=old.isoformat(), valid_until=(old + timedelta(seconds=15)).isoformat()
        ),
    )


def expire_grace(service):
    edit_device(
        service,
        lambda d: d["recovery"].update(retry_after=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()),
    )


def withdraw_first(item):
    item["viewing_options"][0]["decision"] = "excluded"


def start_golf(rig):
    c, s, _ = rig
    command(c, {"type": "play_now", "content_id": "demo:golf"})
    s.tick()
    return overview(c)["device"]


def test_same_event_handoff_preserves_plan_timers_and_passes_all_current_options(rig):
    c, s, _ = rig
    before = start_golf(rig)
    snapshot = edit_snapshot(s, "demo:golf", withdraw_first)
    s.tick()
    after = overview(c)["device"]
    request = jobs(c)[0]["payload"]
    assert after["observed"]["content_id"] == "demo:golf"
    assert after["observed"]["request_id"] != before["observed"]["request_id"]
    assert after["observed"]["viewing_option_id"] == snapshot["viewing_options"][1]["id"]
    assert after["plan"] == before["plan"]
    assert after["revision"] == before["revision"]
    assert after["started_at"] == before["started_at"]
    assert after["last_switch_at"] == before["last_switch_at"]
    assert request["purpose"] == "route_handoff"
    assert request["content_snapshot"] == snapshot
    assert request["allowed_viewing_options"] == [snapshot["viewing_options"][1]]
    for _ in range(5):
        s.tick()
    assert len(jobs(c)) == 2


@pytest.mark.parametrize("change", ["reorder", "addition", "estimate", "display"])
def test_option_churn_and_elapsed_estimates_do_not_interrupt_healthy_playback(rig, change):
    c, s, _ = rig
    before = start_golf(rig)

    def edit(item):
        if change == "reorder":
            item["viewing_options"].reverse()
        elif change == "addition":
            item["viewing_options"].append({**item["viewing_options"][0], "id": "new-alternative"})
        elif change == "estimate":
            item["viewing_options"][0]["expected_end_time"] = "2000-01-01T00:00:00Z"
        else:
            item["title"] = "Updated event display name"

    edit_snapshot(s, "demo:golf", edit)
    for _ in range(3):
        s.tick()
    assert overview(c)["device"]["observed"]["request_id"] == before["observed"]["request_id"]
    assert len(jobs(c)) == 1


def test_changed_locator_with_same_option_id_requires_handoff(rig):
    c, s, _ = rig
    before = start_golf(rig)
    edit_snapshot(
        s, "demo:golf", lambda item: item["viewing_options"][0].update(channel="Replacement live channel")
    )
    s.tick()
    assert jobs(c)[0]["payload"]["purpose"] == "route_handoff"
    assert overview(c)["device"]["observed"]["request_id"] != before["observed"]["request_id"]


def test_withdrawal_fences_late_success_and_replaces_pending_request(rig):
    c, s, settings = rig
    object.__setattr__(settings, "simulation_delay", 100)
    command(c, {"type": "play_now", "content_id": "demo:golf"})
    s.tick()
    old = jobs(c)[0]
    s.playback.submit(old["payload"])
    report = s.playback.inspect(old["id"])
    snapshot = edit_snapshot(s, "demo:golf", withdraw_first)
    assert not s.receive_playback_report(old["id"], report)
    object.__setattr__(settings, "simulation_delay", 0)
    s.tick()
    assert overview(c)["device"]["observed"]["viewing_option_id"] == snapshot["viewing_options"][1]["id"]
    assert next(j for j in jobs(c) if j["id"] == old["id"])["state"] == "superseded"


def test_pending_request_is_replaced_before_delivery_when_a_route_is_withdrawn(rig):
    c, s, settings = rig
    object.__setattr__(settings, "simulation_delay", 100)
    command(c, {"type": "play_now", "content_id": "demo:golf"})
    s.tick()
    old = jobs(c)[0]
    edit_snapshot(s, "demo:golf", withdraw_first)
    object.__setattr__(settings, "simulation_delay", 0)
    s.tick()
    assert jobs(c)[0]["payload"]["purpose"] == "route_handoff"
    assert next(j for j in jobs(c) if j["id"] == old["id"])["delivery_attempts"] == 0
    assert s.playback.submit(old["payload"])["state"] == "cancelled"


def test_all_routes_lost_falls_back_then_returns_when_manual_route_is_restored(rig):
    c, s, _ = rig
    before = start_golf(rig)
    edit_snapshot(s, "demo:golf", lambda item: item.update(viewing_options=[]))
    s.tick()
    assert overview(c)["device"]["observed"]["content_id"] == "demo:redzone"
    assert overview(c)["device"]["plan"] == before["plan"]
    original = jobs(c)[1]["payload"]["content_snapshot"]["viewing_options"]
    edit_snapshot(s, "demo:golf", lambda item: item.update(viewing_options=original))
    s.tick()
    assert overview(c)["device"]["observed"]["content_id"] == "demo:golf"


def test_missing_observation_waits_then_recovers_same_event_with_backoff(rig, monkeypatch):
    c, s, settings = rig
    before = start_golf(rig)
    original_observe = s.playback.observe
    monkeypatch.setattr(s.playback, "observe", lambda _: None)
    expire_observation(s)
    s.tick()
    first = overview(c)["device"]["recovery"]
    assert (
        parse_time(first["retry_after"]) - parse_time(first["since"])
    ).total_seconds() == settings.playback_recovery_grace
    for _ in range(5):
        s.tick()
    assert len(jobs(c)) == 1
    expire_grace(s)
    s.tick()
    assert jobs(c)[0]["payload"]["purpose"] == "recovery"
    assert overview(c)["device"]["started_at"] == before["started_at"]
    expire_observation(s)
    s.tick()
    second = overview(c)["device"]["recovery"]
    assert second["attempts"] == 1
    assert (
        parse_time(second["retry_after"]) - parse_time(second["since"])
    ).total_seconds() == settings.playback_recovery_grace * 2
    monkeypatch.setattr(s.playback, "observe", original_observe)
    s.tick()
    edit_device(
        s,
        lambda d: d["recovery"].update(stable_since=(datetime.now(UTC) - timedelta(seconds=40)).isoformat()),
    )
    s.tick()
    assert overview(c)["device"]["recovery"] is None
    assert len(jobs(c)) == 2


def test_brief_observation_gap_and_future_plan_edit_do_not_restart_playback(rig, monkeypatch):
    c, s, _ = rig
    before = start_golf(rig)
    observe = s.playback.observe
    monkeypatch.setattr(s.playback, "observe", lambda _: None)
    expire_observation(s)
    s.tick()
    command(c, {"type": "add", "content_id": "demo:jays"})
    s.tick()
    assert len(jobs(c)) == 1
    monkeypatch.setattr(s.playback, "observe", observe)
    s.tick()
    assert overview(c)["device"]["observed"]["request_id"] == before["observed"]["request_id"]
    assert overview(c)["device"]["playback_state"] == "verified"


def test_explicit_play_now_can_retry_during_observation_grace(rig, monkeypatch):
    c, s, _ = rig
    start_golf(rig)
    monkeypatch.setattr(s.playback, "observe", lambda _: None)
    expire_observation(s)
    s.tick()
    command(c, {"type": "play_now", "content_id": "demo:golf"})
    s.tick()
    assert len(jobs(c)) == 2
    assert jobs(c)[0]["payload"]["purpose"] == "recovery"


def test_healthy_play_now_does_not_leave_a_future_grace_bypass(rig, monkeypatch):
    c, s, _ = rig
    start_golf(rig)
    command(c, {"type": "play_now", "content_id": "demo:golf"})
    s.tick()
    monkeypatch.setattr(s.playback, "observe", lambda _: None)
    expire_observation(s)
    s.tick()
    assert len(jobs(c)) == 1


async def test_offline_backoff_survives_restart_and_only_latest_intent_is_played(rig):
    c, s, settings = rig
    before = start_golf(rig)
    c.post("/api/v1/simulation", json={"action": "disconnect"})
    expire_observation(s)
    s.tick()
    first = overview(c)["device"]["executor_health"]
    assert first["state"] == "offline"
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    for _ in range(5):
        s.tick()
    assert overview(c)["device"]["executor_health"] == first
    assert len(jobs(c)) == 1
    assert overview(c)["device"]["intent_version"] == before["intent_version"]
    await s.stop()
    restarted = Controller(settings)
    restarted.tick()
    assert restarted.overview()["device"]["executor_health"] == first
    edit_device(restarted, lambda d: d["executor_health"].update(next_probe_at=None))
    restarted.tick()
    assert restarted.overview()["device"]["executor_health"]["failures"] == 2
    with restarted.db.transaction() as db:
        restarted.db.set_meta(db, "simulated_executor_outage", False)
    edit_device(restarted, lambda d: d["executor_health"].update(next_probe_at=None))
    restarted.tick()
    after = restarted.overview()["device"]
    assert after["executor_health"]["state"] == "ok"
    assert after["observed"]["content_id"] == "demo:lions"
    assert [p["content_id"] for p in after["plan"]][:2] == ["demo:lions", "demo:golf"]
    assert len(jobs(c)) == 2


def test_reconnection_reconciles_existing_playback_without_a_new_request(rig):
    c, s, _ = rig
    before = start_golf(rig)
    c.post("/api/v1/simulation", json={"action": "disconnect"})
    expire_observation(s)
    s.tick()
    expire_grace(s)
    for _ in range(5):
        s.tick()
    c.post("/api/v1/simulation", json={"action": "reconnect"})
    s.tick()
    assert overview(c)["device"]["observed"]["request_id"] == before["observed"]["request_id"]
    assert len(jobs(c)) == 1


def test_cold_start_offline_waits_without_navigation(rig):
    c, s, _ = rig
    c.post("/api/v1/simulation", json={"action": "disconnect"})
    s.tick()
    assert jobs(c) == []
    assert overview(c)["device"]["executor_health"]["state"] == "offline"
    c.post("/api/v1/simulation", json={"action": "reconnect"})
    s.tick()
    assert overview(c)["device"]["observed"]["content_id"] == "demo:redzone"


def test_unknown_status_and_missing_playback_wait_then_fall_back_without_completion(rig, monkeypatch):
    c, s, _ = rig
    before = start_golf(rig)
    edit_snapshot(s, "demo:golf", lambda item: item["_simulation"].update(override="unknown"))
    monkeypatch.setattr(s.playback, "observe", lambda _: None)
    expire_observation(s)
    s.tick()
    assert len(jobs(c)) == 1
    assert overview(c)["device"]["desired"] == "demo:golf"
    expire_grace(s)
    s.tick()
    assert overview(c)["device"]["observed"]["content_id"] == "demo:redzone"
    assert overview(c)["device"]["plan"] == before["plan"]
    assert (
        next(e for e in overview(c)["events"] if e["content_id"] == "demo:golf")["lifecycle"]["state"]
        == "unknown"
    )


def test_no_confirmed_fallback_waits_without_repeated_requests(rig, monkeypatch):
    c, s, _ = rig
    before = start_golf(rig)
    for item in overview(c)["events"]:
        edit_snapshot(s, item["content_id"], lambda entry: entry["_simulation"].update(override="unknown"))
    monkeypatch.setattr(s.playback, "observe", lambda _: None)
    expire_observation(s)
    s.tick()
    expire_grace(s)
    for _ in range(5):
        s.tick()
    assert overview(c)["device"]["playback_state"] == "waiting"
    assert overview(c)["device"]["plan"] == before["plan"]
    assert len(jobs(c)) == 1


def test_manual_selection_preempts_the_recovery_grace(rig, monkeypatch):
    c, s, _ = rig
    start_golf(rig)
    monkeypatch.setattr(s.playback, "observe", lambda _: None)
    expire_observation(s)
    s.tick()
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    assert overview(c)["device"]["observed"]["content_id"] == "demo:lions"


def test_old_observation_loss_does_not_reverse_an_approved_switch(rig, monkeypatch):
    c, s, settings = rig
    before = start_golf(rig)
    command(c, {"type": "remove", "entry_id": before["plan"][0]["id"]})
    object.__setattr__(settings, "simulation_delay", 100)
    s.tick()
    request = jobs(c)[0]
    assert request["content_id"] == "demo:redzone"
    monkeypatch.setattr(s.playback, "observe", lambda _: None)
    expire_observation(s)
    s.tick()
    assert overview(c)["device"]["desired"] == "demo:redzone"
    assert jobs(c)[0]["id"] == request["id"]
    with s.db.transaction() as db:
        db.execute("UPDATE jobs SET ready_at=0 WHERE id=?", (request["id"],))
    s.tick()
    assert overview(c)["device"]["observed"]["content_id"] == "demo:redzone"


def test_pause_prevents_handoff_until_resume(rig):
    c, s, _ = rig
    before = start_golf(rig)
    response = c.post(
        "/api/v1/devices/living-room/automation",
        json={"command_id": str(uuid4()), "expected_revision": before["revision"], "mode": "paused"},
    )
    assert response.status_code == 200
    edit_snapshot(s, "demo:golf", withdraw_first)
    s.tick()
    assert len(jobs(c)) == 1
    d = overview(c)["device"]
    c.post(
        "/api/v1/devices/living-room/automation",
        json={"command_id": str(uuid4()), "expected_revision": d["revision"], "mode": "active"},
    )
    s.tick()
    assert jobs(c)[0]["payload"]["purpose"] == "route_handoff"


def test_coverage_switch_demo_uses_explicit_withdrawal_and_preserves_event(rig):
    c, s, _ = rig
    scenario(c, "coverage_switch")
    s.tick()
    before = overview(c)["device"]
    c.post("/api/v1/simulation", json={"action": "advance", "minutes": 15})
    s.tick()
    after = overview(c)["device"]
    assert after["observed"]["content_id"] == before["observed"]["content_id"] == "demo:golf"
    assert after["observed"]["viewing_option_id"] != before["observed"]["viewing_option_id"]
    assert after["plan"] == before["plan"]
