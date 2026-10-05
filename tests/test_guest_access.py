from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_public_access import ADMIN, DEVICE, admin_command, permissions, public_command, revision

from controller.access import GUEST
from controller.api import create_app
from controller.config import Settings


@pytest.fixture(params=["", "test-proxy-secret"])
def guest_rig(tmp_path, request):
    settings = Settings(
        database=str(tmp_path / "guest.sqlite"),
        proxy_secret=request.param,
        public_auth_mode="guest",
        simulation_delay=0,
    )
    app = create_app(settings, start_workers=False)
    with TestClient(app) as client:
        yield client, app.state.controller, settings


def test_guest_browse_requires_no_credentials_and_ignores_supplied_identity(guest_rig):
    client, service, _ = guest_rig
    for headers in ({}, ADMIN, {**ADMIN, "x-dillflix-proxy-key": "spoofed", "x-dillflix-user": "someone"}):
        response = client.get("/api/public/v1/overview", headers=headers)
        assert response.status_code == 200
        assert response.json()["viewer"] == {"name": "Guest", "guest": True}
        assert response.json()["permissions"] == {"play_now": False, "add_to_plan": False}
    assert service.overview()["meta"]["public_auth_mode"] == "guest"


@pytest.mark.parametrize("path", ["/", "/api/v1/overview", "/docs", "/openapi.json", DEVICE + "/diagnostics"])
def test_anonymous_guest_cannot_read_admin_routes(guest_rig, path):
    client, _, _ = guest_rig
    assert client.get(path).status_code in {401, 403}
    assert client.get(path, headers={**ADMIN, "x-dillflix-proxy-key": "wrong"}).status_code in {401, 403}


def test_guest_cannot_write_admin_routes_or_open_websockets(guest_rig):
    client, _, _ = guest_rig
    for path in (
        DEVICE + "/watch-plan",
        DEVICE + "/control",
        DEVICE + "/completions",
        DEVICE + "/automation",
    ):
        assert client.post(path, json={}).status_code in {401, 403}
    assert client.put(DEVICE + "/public-access", json={}).status_code in {401, 403}
    for path in (DEVICE + "/screen/stream", DEVICE + "/control/input"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path):
                pytest.fail("Anonymous admin socket was accepted")


def test_container_liveness_does_not_expose_administrative_health(guest_rig):
    client, _, _ = guest_rig
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/api/health").status_code in {401, 403}


def test_guest_actions_obey_switches_pause_and_admin_priority(guest_rig):
    client, service, settings = guest_rig
    origin = {"origin": "http://testserver"}
    assert public_command(client, service, headers=origin).status_code == 403
    if settings.proxy_secret:
        permissions(client, service)
    else:
        # Represents permissions persisted by an admin before removing the proxy.
        with service.db.transaction() as db:
            device = service.db.device(db)
            device["public_access"] = {"play_now": True, "add_to_plan": True}
            service.db.save_device(db, device)
    response = public_command(client, service, headers=origin)
    assert response.status_code == 200, response.text
    service.tick()
    device = service.overview()["device"]
    assert device["observed"]["content_id"] == "demo:lions"
    assert next(p for p in device["plan"] if p["content_id"] == "demo:lions")["actor"] == GUEST
    if settings.proxy_secret:
        admin_command(client, service)
        service.tick()
        assert public_command(client, service, headers=origin).status_code == 200
        service.tick()
        assert service.overview()["device"]["observed"]["content_id"] == "demo:golf"
        permissions(client, service, play=False)
        assert public_command(client, service, headers=origin).status_code == 403
        assert (
            public_command(client, service, content="demo:chiefs", action="add", headers=origin).status_code
            == 200
        )
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["public_access"]["play_now"] = True
        device["automation"] = "paused"
        service.db.save_device(db, device)
    assert public_command(client, service, headers=origin).status_code == 409


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": ""},
        {"origin": "null"},
        {"origin": "https://evil.example"},
        {"origin": "http://testserver", "sec-fetch-site": "cross-site"},
        {"origin": "https://testserver", "x-forwarded-proto": "https"},
    ],
)
def test_guest_writes_retain_origin_checks(guest_rig, headers):
    client, service, _ = guest_rig
    assert public_command(client, service, headers=headers).status_code == 403


def test_guest_command_receipt_is_idempotent_and_cannot_claim_admin_identity(guest_rig):
    client, service, _ = guest_rig
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["public_access"]["add_to_plan"] = True
        service.db.save_device(db, device)
    body = {
        "command_id": str(uuid4()),
        "expected_revision": revision(service),
        "action": "add",
        "content_id": "demo:chiefs",
    }
    first = client.post("/api/public/v1/watch-plan", headers={"origin": "http://testserver"}, json=body)
    assert first.status_code == 200
    assert client.post("/api/public/v1/watch-plan", headers=ADMIN, json=body).json() == first.json()
    assert service.overview()["device"]["plan"][-1]["actor"] == GUEST
    assert (
        client.post("/api/public/v1/watch-plan", headers=ADMIN, json={**body, "actor": ADMIN}).status_code
        == 422
    )


def test_guest_assets_and_page_are_accessible_without_exposing_admin(tmp_path):
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "public.html").write_text("public page")
    (frontend / "index.html").write_text("admin page")
    (frontend / "assets" / "public.js").write_text("public script")
    app = create_app(
        Settings(database=str(tmp_path / "test.sqlite"), frontend=frontend, public_auth_mode="guest"),
        start_workers=False,
    )
    with TestClient(app) as client:
        for path in ("/public", "/public/"):
            assert client.get(path).text == "public page"
        assert client.get("/assets/public.js").text == "public script"
        assert client.get("/").status_code == 403


def test_mode_is_explicit_and_invalid_configuration_is_rejected(monkeypatch):
    monkeypatch.delenv("CONTROLLER_PUBLIC_AUTH_MODE", raising=False)
    monkeypatch.setenv("CONTROLLER_MODE", "demo")
    assert Settings.from_env().public_auth_mode == "proxy"
    monkeypatch.setenv("CONTROLLER_PUBLIC_AUTH_MODE", "guest")
    assert Settings.from_env().public_auth_mode == "guest"
    monkeypatch.setenv("CONTROLLER_PUBLIC_AUTH_MODE", "disabled")
    with pytest.raises(ValueError, match="proxy or guest"):
        Settings.from_env()
    with pytest.raises(ValueError, match="proxy or guest"):
        Settings(public_auth_mode="typo")


async def test_switching_back_to_proxy_mode_requires_login_and_keeps_guest_plan(guest_rig):
    client, service, settings = guest_rig
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["public_access"]["add_to_plan"] = True
        service.db.save_device(db, device)
    assert (
        public_command(client, service, action="add", headers={"origin": "http://testserver"}).status_code
        == 200
    )
    await service.stop()
    protected = create_app(
        replace(settings, public_auth_mode="proxy", proxy_secret="test-proxy-secret"), start_workers=False
    )
    with TestClient(protected) as secured:
        assert secured.get("/api/public/v1/overview").status_code == 401
        assert protected.state.controller.overview()["device"]["plan"][-1]["actor"] == GUEST
