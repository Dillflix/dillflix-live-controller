from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from controller.api import create_app
from controller.config import Settings
from controller.planner import choose, preview_plan
from controller.service import Controller

ADMIN = {
    "x-dillflix-proxy-key": "test-proxy-secret",
    "x-dillflix-role": "admin",
    "x-dillflix-user": "owner",
    "origin": "http://testserver",
}
USER = {**ADMIN, "x-dillflix-role": "user", "x-dillflix-user": "viewer"}
DEVICE = "/api/v1/devices/living-room"


@pytest.fixture
def public_rig(tmp_path):
    settings = Settings(
        database=str(tmp_path / "public.sqlite"), proxy_secret="test-proxy-secret", simulation_delay=0
    )
    app = create_app(settings, start_workers=False)
    with TestClient(app) as client:
        yield client, app.state.controller, settings


def revision(service):
    return service.overview()["device"]["revision"]


def permissions(client, service, play=True, add=True):
    response = client.put(
        DEVICE + "/public-access",
        headers=ADMIN,
        json={
            "command_id": str(uuid4()),
            "expected_revision": revision(service),
            "play_now": play,
            "add_to_plan": add,
        },
    )
    assert response.status_code == 200, response.text
    return response


def public_command(client, service, content="demo:lions", action="play_now", headers=None, **overrides):
    return client.post(
        "/api/public/v1/watch-plan",
        headers=headers or USER,
        json={
            "command_id": str(uuid4()),
            "expected_revision": revision(service),
            "content_id": content,
            "action": action,
            **overrides,
        },
    )


def admin_command(client, service, content="demo:golf", action="play_now", **extra):
    response = client.post(
        DEVICE + "/watch-plan",
        headers=ADMIN,
        json={
            "command_id": str(uuid4()),
            "expected_revision": revision(service),
            "action": {"type": action, "content_id": content, **extra},
        },
    )
    assert response.status_code == 200, response.text
    return response


def test_public_is_closed_without_proxy_and_rejects_forged_headers(rig, public_rig):
    client, _, _ = rig
    assert client.get("/api/public/v1/overview", headers=USER).status_code == 503
    client, _, _ = public_rig
    for headers in ({}, {**USER, "x-dillflix-proxy-key": "wrong"}, {**USER, "x-dillflix-user": ""}):
        assert client.get("/api/public/v1/overview", headers=headers).status_code == 401
        assert client.get("/api/v1/overview", headers=headers).status_code == 401
    assert client.get("/api/v1/overview", headers={**USER, "x-dillflix-role": "owner"}).status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/overview",
        DEVICE + "/state",
        DEVICE + "/screen",
        DEVICE + "/jobs",
        DEVICE + "/diagnostics",
        "/api/v1/updates",
        "/openapi.json",
        "/docs",
        "/",
    ],
)
def test_user_cannot_read_admin_surfaces(public_rig, path):
    client, _, _ = public_rig
    assert client.get(path, headers=USER).status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        DEVICE + "/watch-plan",
        DEVICE + "/automation",
        DEVICE + "/control",
        DEVICE + "/undo",
        DEVICE + "/completion",
        "/api/v1/simulation",
    ],
)
def test_user_cannot_mutate_admin_surfaces(public_rig, path):
    client, _, _ = public_rig
    assert client.post(path, headers=USER, json={}).status_code == 403


def test_user_cannot_enable_actions_or_open_admin_sockets(public_rig):
    client, _, _ = public_rig
    assert client.put(DEVICE + "/public-access", headers=USER, json={}).status_code == 403
    for path in (DEVICE + "/screen/stream", DEVICE + "/control/input"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path, headers=USER):
                pytest.fail("User reached admin socket")


@pytest.mark.parametrize(
    "origin", ["", "null", "https://evil.example", "http://sibling.testserver", "https://testserver"]
)
def test_cross_origin_public_mutations_are_rejected(public_rig, origin):
    client, service, _ = public_rig
    permissions(client, service)
    assert public_command(client, service, headers={**USER, "origin": origin}).status_code == 403


def test_public_projection_hides_private_state_and_routes(public_rig):
    client, service, _ = public_rig
    service.tick()
    response = client.get("/api/public/v1/overview", headers=USER)
    assert response.status_code == 200
    data = response.json()
    assert data["viewer"] == {"name": "viewer", "guest": False}
    assert data["now_playing"]["event"]["content_id"] == "demo:redzone"
    assert data["permissions"] == {"play_now": False, "add_to_plan": False}
    for private in (
        "viewing_options",
        "snapshot",
        "rules",
        "team_ranks",
        "manual_control",
        "owner_token",
        "listing_url",
        "activity",
        "actor",
        "proxy-secret",
        "prime_access",
    ):
        assert private not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_permission_switches_are_independent_and_checked_on_server(public_rig):
    client, service, _ = public_rig
    assert public_command(client, service).status_code == 403
    permissions(client, service, play=False)
    assert public_command(client, service).status_code == 403
    assert public_command(client, service, action="add").status_code == 200
    permissions(client, service, play=True, add=False)
    assert public_command(client, service, content="demo:golf", action="add").status_code == 403
    assert public_command(client, service).status_code == 200
    permissions(client, service, play=False, add=False)
    assert public_command(client, service).status_code == 403
    assert any(p["content_id"] == "demo:lions" for p in service.overview()["device"]["plan"])


def test_admin_preempts_user_and_user_cannot_preempt_admin(public_rig):
    client, service, _ = public_rig
    permissions(client, service)
    assert public_command(client, service).status_code == 200
    service.tick()
    assert service.overview()["device"]["observed"]["content_id"] == "demo:lions"
    admin_command(client, service)
    service.tick()
    assert service.overview()["device"]["observed"]["content_id"] == "demo:golf"
    assert public_command(client, service).status_code == 200
    service.tick()
    device = service.overview()["device"]
    assert device["observed"]["content_id"] == "demo:golf"
    assert [p["actor"]["type"] for p in device["plan"]] == ["admin", "admin", "user"]
    assert device["plan"][-1]["actor"]["name"] == "viewer"
    assert device.get("retry_playback") is None


def test_same_event_request_cannot_demote_admin_and_admin_can_promote_user(public_rig):
    client, service, _ = public_rig
    permissions(client, service)
    admin_command(client, service)
    before = service.overview()["device"]["plan"]
    assert public_command(client, service, content="demo:golf").status_code == 200
    assert service.overview()["device"]["plan"] == before
    assert public_command(client, service, action="add").status_code == 200
    admin_command(client, service, content="demo:lions", action="add")
    plan = service.overview()["device"]["plan"]
    assert all(p["actor"]["type"] == "admin" for p in plan)
    assert next(p for p in plan if p["content_id"] == "demo:lions")["actor"]["name"] == "owner"


def test_public_app_records_user_source_even_for_admin_identity(public_rig):
    client, service, _ = public_rig
    permissions(client, service)
    assert public_command(client, service, headers=ADMIN).status_code == 200
    entry = service.overview()["device"]["plan"][-1]
    assert entry["actor"] == {"type": "user", "id": "plexsso:owner", "name": "owner"}


def test_pause_and_manual_control_cannot_be_overridden(public_rig):
    client, service, _ = public_rig
    permissions(client, service)
    for mode in ("pause", "manual", "handoff"):
        with service.db.transaction() as db:
            device = service.db.device(db)
            device["automation"] = "paused" if mode == "pause" else "active"
            device["manual_control"] = {"session_id": "admin-session"} if mode == "manual" else None
            device["input_handoff"] = {"through_intent_version": 1} if mode == "handoff" else None
            service.db.save_device(db, device)
        assert public_command(client, service).status_code == 409
        assert public_command(client, service, content="demo:chiefs", action="add").status_code == 200
        data = client.get("/api/public/v1/overview", headers=USER).json()
        assert data["permissions"] == {"play_now": False, "add_to_plan": True}


def test_receipts_are_bound_to_actor_and_stale_revisions_do_not_write(public_rig):
    client, service, _ = public_rig
    permissions(client, service)
    original_revision = revision(service)
    key = str(uuid4())
    first = public_command(client, service, command_id=key, expected_revision=original_revision)
    repeated = public_command(client, service, command_id=key, expected_revision=original_revision)
    assert first.json() == repeated.json()
    assert (
        public_command(
            client,
            service,
            command_id=key,
            expected_revision=original_revision,
            headers={**USER, "x-dillflix-user": "someone-else"},
        ).status_code
        == 409
    )
    assert (
        public_command(client, service, content="demo:golf", expected_revision=original_revision).status_code
        == 409
    )


@pytest.mark.parametrize(
    "extra", [{"actor": {"type": "admin"}}, {"action": "remove"}, {"priority": "first"}, {"role": "admin"}]
)
def test_public_body_cannot_supply_privilege_or_admin_operations(public_rig, extra):
    client, service, _ = public_rig
    permissions(client, service)
    assert public_command(client, service, **extra).status_code == 422


def test_user_resumes_after_admin_event_finishes(public_rig):
    client, service, _ = public_rig
    permissions(client, service)
    public_command(client, service)
    admin_command(client, service)
    service.tick()
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["manual_completions"]["demo:golf"] = service.now(db).isoformat()
        service.db.save_device(db, device)
    service.tick()
    assert service.overview()["device"]["observed"]["content_id"] == "demo:lions"


async def test_public_permissions_and_actor_survive_restart(public_rig):
    client, service, settings = public_rig
    permissions(client, service)
    public_command(client, service)
    before = service.overview()["device"]
    await service.stop()
    restarted = Controller(settings)
    try:
        after = restarted.overview()["device"]
        assert after["plan"] == before["plan"]
        assert after["public_access"] == before["public_access"]
    finally:
        await restarted.stop()


def test_priority_applies_to_preview_and_unordered_legacy_records(public_rig):
    client, service, _ = public_rig
    permissions(client, service)
    public_command(client, service)
    admin_command(client, service)
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["plan"].reverse()
        for entry in device["plan"]:
            if entry["content_id"] == "demo:golf":
                entry.pop("actor")  # Upgraded legacy commitments are admin-owned.
        now = service.now(db)
        items = service.items(db)
        assert choose(device, items, now, now)["content_id"] == "demo:golf"
        assert preview_plan(device, items, now)["segments"][0]["content_id"] == "demo:golf"


def test_admin_add_to_plan_preempts_live_user_request(public_rig):
    client, service, _ = public_rig
    permissions(client, service)
    public_command(client, service)
    service.tick()
    admin_command(client, service, action="add")
    service.tick()
    assert service.overview()["device"]["observed"]["content_id"] == "demo:golf"


def test_admin_supersedes_a_pending_user_launch(public_rig):
    client, service, settings = public_rig
    permissions(client, service)
    object.__setattr__(settings, "simulation_delay", 100)
    public_command(client, service)
    service.tick()
    admin_command(client, service)
    service.tick()
    device = service.overview()["device"]
    assert device["desired"] == "demo:golf"
    with service.db.transaction() as db:
        jobs = [dict(row) for row in db.execute("SELECT content_id,state FROM jobs ORDER BY rowid")]
    assert jobs == [
        {"content_id": "demo:lions", "state": "superseded"},
        {"content_id": "demo:golf", "state": "pending"},
    ]


def test_user_cannot_interrupt_admin_during_status_outage_or_retry(public_rig):
    client, service, _ = public_rig
    permissions(client, service)
    admin_command(client, service)
    service.tick()
    with service.db.transaction() as db:
        device = service.db.device(db)
        now = service.now(db)
        items = service.items(db)
        golf = next(i for i in items if i["content_id"] == "demo:golf")
        # This read-only projection exercises the planner's stale-evidence hold.
        golf["lifecycle"]["state"] = "unknown"
        golf["playable"] = False
        device["plan"].insert(
            0, service.entry("demo:lions", {"type": "user", "id": "viewer", "name": "viewer"})
        )
        assert choose(device, items, now, datetime.now(UTC))["content_id"] == "demo:golf"
        golf["lifecycle"]["state"] = "live"
        golf["playable"] = True
        device["failures"]["demo:golf"] = {
            "retry_after": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        }
        assert choose(device, items, now, datetime.now(UTC))["content_id"] == "demo:golf"


def test_public_now_playing_expires_evidence_even_without_workers(public_rig):
    client, service, _ = public_rig
    service.tick()
    assert client.get("/api/public/v1/overview", headers=USER).json()["now_playing"]["verified"]
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["observed"]["valid_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        service.db.save_device(db, device)
    data = client.get("/api/public/v1/overview", headers=USER).json()
    assert not data["now_playing"]["verified"]
    assert data["now_playing"]["event"]["content_id"] == "demo:redzone"


def test_permission_updates_are_revisioned_idempotent_and_undoable(public_rig):
    client, service, _ = public_rig
    body = {
        "command_id": str(uuid4()),
        "expected_revision": revision(service),
        "play_now": True,
        "add_to_plan": False,
    }
    first = client.put(DEVICE + "/public-access", headers=ADMIN, json=body)
    assert client.put(DEVICE + "/public-access", headers=ADMIN, json=body).json() == first.json()
    assert (
        client.put(
            DEVICE + "/public-access", headers=ADMIN, json={**body, "command_id": str(uuid4())}
        ).status_code
        == 409
    )
    overview = service.overview()
    response = client.post(
        DEVICE + "/undo",
        headers=ADMIN,
        json={
            "command_id": str(uuid4()),
            "expected_revision": revision(service),
            "history_id": overview["undo"]["id"],
        },
    )
    assert response.status_code == 200
    assert service.overview()["device"]["public_access"] == {"play_now": False, "add_to_plan": False}
