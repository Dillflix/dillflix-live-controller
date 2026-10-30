import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from controller.api import create_app
from controller.broadcast_overlay import broadcast_overlay
from controller.config import DeviceSettings, Settings
from controller.database import encode

URL = "/api/v1/devices/living-room/broadcast-overlay"


def project(service, now=None):
    return broadcast_overlay(service, service.device_id, now=now).model_dump()


def device_update(service, **fields):
    with service.db.transaction() as db:
        device = service.db.device(db)
        device.update(fields)
        service.db.save_device(db, device)


@pytest.fixture
def playing(rig):
    client, service, _ = rig
    assert client.post("/api/v1/devices/living-room/watch-plan", json={
        "command_id": "play-overlay", "expected_revision": 0,
        "action": {"type": "play_now", "content_id": "demo:lions"},
    }).status_code == 200
    service.tick()
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["observed"]["simulated"] = False
        service.db.save_device(db, device)
    return rig


@pytest.fixture
def pending(rig):
    _, service, _ = rig
    now = datetime.now(UTC)
    deadline = now + timedelta(minutes=2)
    report = {
        "device_id": "living-room", "request_id": "overlay-job", "intent_version": 10,
        "content_id": "demo:lions", "cancellation": {"state": "none"},
        "operation": {"state": "navigating", "phase": "verifying", "deadline_at": deadline.isoformat()},
        "prime_player": {
            "session_id": "session", "attempt_id": "attempt", "network_context_id": "context",
            "selected": {"content_id": "gti:game", "viewing_option_id": "option"},
            "launch_outcome": {
                "session_id": "session", "attempt_id": "attempt", "requested_id": "gti:game",
                "network_context_id": "context", "resolved_id": "gti:resolved", "state": "launch_requested",
                "evidence": {"resolution": {"playbackClass": "live_watch_now"}},
            },
        },
    }
    with service.db.transaction() as db:
        device = service.db.device(db)
        device.update(intent_version=10, desired="demo:lions", playback_state="navigating")
        service.db.save_device(db, device)
        db.execute("INSERT INTO jobs(id,device_id,intent,content_id,state,ready_at,payload,deadline_at,progress) "
                   "VALUES ('overlay-job','living-room',10,'demo:lions','pending',0,?,?,'verifying')",
                   (encode({"allowed_viewing_options": [{"id": "option"}]}), deadline.timestamp()))
        db.execute("INSERT INTO executor_jobs(token,request_id,device_id,intent,content_id,request,request_hash,report,state) "
                   "VALUES ('overlay-token','overlay-job','living-room',10,'demo:lions','{}','digest',?,'navigating')",
                   (encode(report),))
        db.execute("INSERT INTO executor_devices(device_id,highest_intent,cancelled_through,current_token) "
                   "VALUES ('living-room',10,-1,'overlay-token')")
    return rig, report, now


def save_report(service, report):
    with service.db.transaction() as db:
        db.execute("UPDATE executor_jobs SET report=? WHERE token='overlay-token'", (encode(report),))


def test_overlay_is_compact_read_only_retained_snapshot(rig, monkeypatch):
    client, service, _ = rig
    with service.db.snapshot() as db:
        before = list(db.iterdump())
    snapshots, statements = [], []
    original = service.db.snapshot

    @contextmanager
    def snapshot():
        snapshots.append(True)
        with original() as db:
            db.set_trace_callback(statements.append)
            yield db

    def forbidden(*args, **kwargs):
        pytest.fail("Overlay must not build the catalogue, mutate state, or contact a device")

    monkeypatch.setattr(service.db, "snapshot", snapshot)
    for obj, name in ((service.db, "transaction"), (service, "items"), (service, "tick"),
                      (service.playback, "observe"), (service.client, "fetch_snapshot")):
        monkeypatch.setattr(obj, name, forbidden)
    response = client.get(URL)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    view = response.json()
    assert set(view) == {"schema_version", "device_id", "device_name", "generated_at", "valid_until",
                         "visible", "state", "headline", "event_title"}
    assert view["visible"] is True and view["state"] == "waiting"
    assert view["headline"] == "Waiting for live sports" and view["event_title"] is None
    assert datetime.fromisoformat(view["valid_until"]) - datetime.fromisoformat(view["generated_at"]) == timedelta(seconds=5)
    assert len(snapshots) == 1
    assert not any("FROM contents" in query and "WHERE id=" not in query for query in statements)
    with original() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("condition,hidden", [
    ("fresh", True), ("switching", True), ("paused", True), ("offline", True),
    ("expired", False), ("simulated", False), ("wrong_device", False),
    ("wrong_request", False), ("manual", False), ("handoff", False), ("completed", False),
])
def test_real_verified_playback_suppresses_overlay_even_when_switching_or_paused(playing, condition, hidden):
    _, service, _ = playing
    with service.db.transaction() as db:
        device = service.db.device(db)
        if condition == "switching":
            device.update(desired="demo:jays", playback_state="navigating", intent_version=device["intent_version"] + 1)
        elif condition == "paused":
            device["automation"] = "paused"
        elif condition == "offline":
            device["executor_health"] = {"state": "offline"}
        elif condition == "expired":
            device["observed"]["valid_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        elif condition == "simulated":
            device["observed"]["simulated"] = True
        elif condition == "wrong_device":
            device["observed"]["device_id"] = "den"
        elif condition == "wrong_request":
            device["observed"]["request_id"] = "different-job"
        elif condition in {"manual", "handoff"}:
            device["manual_control" if condition == "manual" else "input_handoff"] = {"secret": "not-public"}
        elif condition == "completed":
            device["manual_completions"]["demo:lions"] = datetime.now(UTC).isoformat()
        service.db.save_device(db, device)
    view = project(service)
    assert view["visible"] is not hidden
    assert view["event_title"] is None
    if hidden:
        assert view["state"] == "hidden" and view["headline"] is None


@pytest.mark.parametrize("outcome", ["launch_requested", "observing", "playing", "unknown"])
def test_actual_launch_ack_or_unknown_delivery_suppresses_during_verification(pending, outcome):
    (_, service, _), report, now = pending
    report["prime_player"]["launch_outcome"]["state"] = outcome
    if outcome == "unknown":
        report["prime_player"]["launch_outcome"]["evidence"] = {"launch": {"disposition": "unknown"}}
    if outcome == "playing":
        report["operation"].update(state="playing_verified", finished_at=now.isoformat())
    save_report(service, report)
    assert project(service)["visible"] is False
    assert project(service, now + timedelta(minutes=3))["visible"] is True


@pytest.mark.parametrize("phase", ["queued", "accepted", "searching", "launching", "verifying", "verified"])
def test_phase_and_attempt_id_alone_never_suppress_overlay(pending, phase):
    (_, service, _), report, _ = pending
    report["operation"]["phase"] = phase
    report["prime_player"]["launch_outcome"] = None
    save_report(service, report)
    view = project(service)
    assert view["visible"] and view["state"] == "switching"
    assert view["headline"] == "Switching live coverage"
    assert view["event_title"] == "Lions at Bills"


@pytest.mark.parametrize("action_state", ["dispatching", "uncertain", "acknowledged"])
def test_journaled_play_with_lost_response_hides_only_until_original_deadline(pending, action_state):
    (_, service, _), report, now = pending
    report["prime_player"]["launch_outcome"] = None
    save_report(service, report)
    with service.db.transaction() as db:
        db.execute("INSERT INTO executor_actions(token,at,action,state) VALUES ('overlay-token',?,'PRIME_PLAY',?)",
                   (now.isoformat(), action_state))
    assert project(service)["visible"] is False
    view = project(service, now + timedelta(seconds=119))
    assert view["valid_until"] == (now + timedelta(minutes=2)).isoformat()
    assert project(service, now + timedelta(minutes=3))["visible"] is True
    report["prime_player"]["launch_outcome"] = {
        "session_id": "session", "attempt_id": "attempt", "requested_id": "gti:game",
        "network_context_id": "context", "state": "failed", "evidence": {"launch": {"disposition": "not_invoked"}},
    }
    save_report(service, report)
    assert project(service)["visible"] is True


@pytest.mark.parametrize("condition", [
    "device_intent", "report_device", "executor_device", "report_request", "report_intent", "report_content",
    "owner", "cancelled", "retired", "executor_failed", "operation_failed", "finished", "deadline", "job_deadline",
    "session", "attempt", "requested", "network", "option", "non_live", "not_invoked", "cancelled_outcome",
])
def test_stale_foreign_failed_or_refused_launch_never_hides_overlay(pending, condition):
    (_, service, _), report, now = pending
    outcome = report["prime_player"]["launch_outcome"]
    with service.db.transaction() as db:
        if condition == "device_intent":
            device = service.db.device(db)
            device["intent_version"] += 1
            service.db.save_device(db, device)
        elif condition == "executor_device":
            db.execute("UPDATE executor_jobs SET device_id='other'")
        elif condition == "owner":
            db.execute("UPDATE executor_devices SET highest_intent=11")
        elif condition == "cancelled":
            db.execute("UPDATE executor_jobs SET cancel_requested=1")
        elif condition == "retired":
            db.execute("UPDATE executor_jobs SET retired_at=1")
        elif condition == "executor_failed":
            db.execute("UPDATE executor_jobs SET state='failed'")
        elif condition == "job_deadline":
            db.execute("UPDATE jobs SET deadline_at=?", ((now - timedelta(seconds=1)).timestamp(),))
    if condition.startswith("report_"):
        report[{"report_device": "device_id", "report_request": "request_id", "report_intent": "intent_version",
                "report_content": "content_id"}[condition]] = "different"
    elif condition in {"session", "attempt", "requested", "network"}:
        outcome[{"session": "session_id", "attempt": "attempt_id", "requested": "requested_id", "network": "network_context_id"}[condition]] = "different"
    elif condition == "option":
        report["prime_player"]["selected"]["viewing_option_id"] = "different"
    elif condition == "operation_failed":
        report["operation"]["state"] = "failed"
    elif condition == "finished":
        report["operation"]["finished_at"] = now.isoformat()
    elif condition == "deadline":
        report["operation"]["deadline_at"] = (now - timedelta(seconds=1)).isoformat()
    elif condition == "non_live":
        outcome["evidence"]["resolution"]["playbackClass"] = "replay"
    elif condition == "not_invoked":
        outcome["evidence"]["launch"] = {"disposition": "not_invoked"}
    elif condition == "cancelled_outcome":
        outcome["state"] = "cancelled"
    save_report(service, report)
    assert project(service)["visible"] is True


@pytest.mark.parametrize("fields,state", [
    ({"automation": "paused"}, "standby"), ({"manual_control": {"id": "owner"}}, "standby"),
    ({"input_handoff": {"error": "secret"}}, "standby"), ({"player_recovery": {"reason": "secret"}}, "recovering"),
    ({"prime_network": {"state": "recovering", "target": "montreal"}}, "recovering"),
    ({"executor_health": {"state": "offline", "error": "secret"}}, "recovering"),
    ({"playback_state": "failed", "reason": "secret"}, "waiting"),
    ({"reason": "Prime feed is upcoming; catalogue retries in 60 seconds"}, "waiting"),
])
def test_public_copy_never_leaks_operational_detail(rig, fields, state):
    _, service, _ = rig
    device_update(service, **fields)
    view = project(service)
    assert view["state"] == state
    assert view["event_title"] is None
    assert not any(word in encode(view) for word in ("secret", "montreal", "catalogue", "Prime", "owner"))


def test_endpoint_auth_and_stream_identity(tmp_path):
    app = create_app(Settings(database=str(tmp_path / "auth.db"), proxy_secret="proxy-secret",
                              devices=(DeviceSettings("living-room", "Dillflix 1"), DeviceSettings("den", "Dillflix 2"))),
                     start_workers=False)
    admin = {"x-dillflix-proxy-key": "proxy-secret", "x-dillflix-user": "owner", "x-dillflix-role": "admin"}
    with TestClient(app) as client:
        assert client.get(URL).status_code == 401
        assert client.get(URL, headers={**admin, "x-dillflix-role": "user"}).status_code == 403
        result = client.get("/api/v1/devices/den/broadcast-overlay", headers=admin)
        assert result.status_code == 200
        assert result.json()["device_id"] == "den" and result.json()["device_name"] == "Dillflix 2"
        assert client.get("/api/v1/devices/missing/broadcast-overlay", headers=admin).status_code == 404


@pytest.mark.parametrize("terminal", ["manual", "ended", "cancelled"])
def test_completed_target_cannot_hide_or_be_advertised_while_coordinator_catches_up(pending, terminal):
    (_, service, _), _, now = pending
    with service.db.transaction() as db:
        if terminal == "manual":
            device = service.db.device(db)
            device["manual_completions"]["demo:lions"] = now.isoformat()
            service.db.save_device(db, device)
        else:
            row = db.execute("SELECT snapshot FROM contents WHERE id='demo:lions'").fetchone()
            snapshot = json.loads(row[0])
            snapshot["_simulation"]["override"] = terminal
            snapshot["status"] = terminal
            db.execute("UPDATE contents SET snapshot=? WHERE id='demo:lions'", (encode(snapshot),))
            db.execute("DELETE FROM content_status WHERE content_id='demo:lions'")
    view = project(service)
    assert view["visible"] is True and view["event_title"] is None


@pytest.mark.parametrize("runtime_state,expected", [
    ("observing", "recovering"), ("discovering", "recovering"),
    ("validating", "recovering"), ("backoff", "recovering"),
    ("contract_incompatible", "standby"), ("ready", "waiting"),
])
def test_runtime_adaptation_without_managed_network_has_public_state(rig, runtime_state, expected):
    _, service, _ = rig
    device_update(service, prime_runtime_adaptation={
        "state": runtime_state, "session_id": "new-session", "reason": "secret operational detail",
    })
    view = project(service)
    assert view["visible"] and view["state"] == expected
    assert view["event_title"] is None
    assert "secret" not in encode(view)


@pytest.mark.parametrize("field,value,hidden", [
    ("prime_network_scope", {"context_id": "context"}, True),
    ("prime_network_scope", {"context_id": "replacement-context"}, False),
    ("prime_network_scope", {"context_id": None}, False),
    ("prime_runtime_adaptation", {"state": "validating", "session_id": "session"}, True),
    ("prime_runtime_adaptation", {"state": "validating", "session_id": "replacement-session"}, False),
])
@pytest.mark.parametrize("acknowledged", [True, False])
def test_latest_runtime_scope_fences_retained_launch_acknowledgement_and_uncertain_dispatch(
    pending, field, value, hidden, acknowledged,
):
    (_, service, _), report, now = pending
    device_update(service, **{field: value}, prime_network={"state": "transitioning"})
    if not acknowledged:
        report["prime_player"]["launch_outcome"] = None
        save_report(service, report)
        with service.db.transaction() as db:
            db.execute("INSERT INTO executor_actions(token,at,action,state) "
                       "VALUES ('overlay-token',?,'PRIME_PLAY','uncertain')", (now.isoformat(),))
    view = project(service)
    assert view["visible"] is not hidden
    assert view["state"] == ("hidden" if hidden else "switching")


@pytest.mark.parametrize("outcome", ["waiting_for_feed", "feeds_unavailable", "feeds_locked", "no_matching_feed", "access_unknown"])
@pytest.mark.parametrize("playback_state", ["waiting", "failed"])
def test_provider_outcome_or_exhausted_retry_budget_is_waiting_not_reconnection(pending, outcome, playback_state):
    (_, service, _), report, now = pending
    report["operation"].update(state=outcome, phase=outcome, finished_at=now.isoformat())
    report["prime_player"].update(readiness=outcome, nontechnical_failure=outcome in {
        "feeds_unavailable", "feeds_locked", "no_matching_feed",
    })
    save_report(service, report)
    with service.db.transaction() as db:
        db.execute("UPDATE jobs SET state=?,progress=? WHERE id='overlay-job'", (outcome, outcome))
        db.execute("UPDATE executor_jobs SET state=? WHERE token='overlay-token'", (outcome,))
        device = service.db.device(db)
        device.update(playback_state=playback_state,
                      prime_access={"demo:lions": {"state": outcome, "reason": "provider detail"}},
                      event_attempts={"demo:lions": {"attempts": 2, "state": outcome}})
        service.db.save_device(db, device)
    view = project(service)
    assert view["visible"] and view["state"] == "waiting"
    assert view["headline"] == "Waiting for live sports" and view["event_title"] is None


@pytest.mark.parametrize("field,value,expected", [
    ("prime_network", {"state": "transitioning"}, "switching"),
    ("prime_network", {"state": "recovering"}, "recovering"),
    ("prime_runtime_adaptation", {"state": "observing"}, "switching"),
    ("prime_runtime_adaptation", {"state": "discovering"}, "switching"),
    ("prime_runtime_adaptation", {"state": "validating"}, "switching"),
    ("prime_runtime_adaptation", {"state": "backoff"}, "recovering"),
    ("prime_runtime_adaptation", {"state": "contract_incompatible"}, "standby"),
    ("player_recovery", {"state": "pending"}, "recovering"),
    ("executor_health", {"state": "offline"}, "recovering"),
])
def test_normal_request_preparation_is_switching_and_actual_recovery_is_reconnecting(pending, field, value, expected):
    (_, service, _), report, _ = pending
    report["prime_player"]["launch_outcome"] = None
    save_report(service, report)
    device_update(service, **{field: value})
    assert project(service)["state"] == expected


def test_explicit_recovery_request_is_reconnecting(pending):
    (_, service, _), report, _ = pending
    report["prime_player"]["launch_outcome"] = None
    save_report(service, report)
    with service.db.transaction() as db:
        payload = json.loads(db.execute("SELECT payload FROM jobs WHERE id='overlay-job'").fetchone()[0])
        payload["purpose"] = "recovery"
        db.execute("UPDATE jobs SET payload=? WHERE id='overlay-job'", (encode(payload),))
    assert project(service)["state"] == "recovering"


@pytest.mark.parametrize("invalid", [None, "superseded", "completed", "simulated", "foreign", "missing"])
def test_interrupted_event_title_survives_gap_before_recovery_job(playing, invalid):
    _, service, _ = playing
    with service.db.transaction() as db:
        device = service.db.device(db)
        observed = device["observed"]
        observed["verified"] = False
        device["recovery"] = {"content_id": observed["content_id"], "since": datetime.now(UTC).isoformat()}
        if invalid == "superseded":
            device["desired"] = "another-event"
        elif invalid == "completed":
            device["manual_completions"][observed["content_id"]] = datetime.now(UTC).isoformat()
        elif invalid == "simulated":
            observed["simulated"] = True
        elif invalid == "foreign":
            db.execute("UPDATE jobs SET device_id='other-stream' WHERE id=?", (observed["request_id"],))
        elif invalid == "missing":
            observed["request_id"] = "missing-request"
        service.db.save_device(db, device)
        expected = json.loads(db.execute("SELECT snapshot FROM contents WHERE id=?", (observed["content_id"],)).fetchone()[0])["title"]
    view = project(service)
    assert view["state"] == "recovering"
    assert view["headline"] == "Reconnecting live coverage"
    assert view["event_title"] == (expected if invalid is None else None)
