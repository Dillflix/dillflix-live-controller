from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_public_access import ADMIN, DEVICE, public_command, revision

from controller.access import GUEST, LEGACY_ADMIN, ProxyAccess
from controller.api import create_app
from controller.config import Settings

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture(params=["", "test-proxy-secret"])
def lan_rig(tmp_path, request):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("admin page")
    (frontend / "public.html").write_text("public page")
    settings = Settings(
        database=str(tmp_path / "lan.sqlite"),
        frontend=frontend,
        proxy_secret=request.param,
        public_auth_mode="guest",
        admin_auth_mode="trusted-lan",
        simulation_delay=0,
    )
    app = create_app(settings, start_workers=False)
    with TestClient(app) as client:
        yield client, app.state.controller, settings


def set_permissions(client, service, enabled):
    return client.put(
        DEVICE + "/public-access",
        headers=ORIGIN,
        json={
            "command_id": str(uuid4()),
            "expected_revision": revision(service),
            "play_now": enabled,
            "add_to_plan": enabled,
        },
    )


def test_direct_pages_switches_attribution_and_admin_priority(lan_rig):
    client, service, _ = lan_rig
    assert client.get("/").text == "admin page"
    assert client.get("/public/").text == "public page"
    assert client.get("/api/v1/overview").status_code == 200
    assert public_command(client, service, headers=ORIGIN).status_code == 403
    assert set_permissions(client, service, True).status_code == 200
    # Even valid admin proxy headers cannot turn public requests into admin actions.
    assert public_command(client, service, headers=ADMIN).status_code == 200
    service.tick()
    assert service.overview()["device"]["observed"]["content_id"] == "demo:lions"
    plan = service.overview()["device"]["plan"]
    assert next(p for p in plan if p["content_id"] == "demo:lions")["actor"] == GUEST
    response = client.post(
        DEVICE + "/watch-plan",
        headers={**ORIGIN, "x-dillflix-role": "user", "x-dillflix-user": "spoofed"},
        json={
            "command_id": str(uuid4()),
            "expected_revision": revision(service),
            "action": {"type": "play_now", "content_id": "demo:golf"},
        },
    )
    assert response.status_code == 200, response.text
    service.tick()
    assert public_command(client, service, headers=ORIGIN).status_code == 200
    service.tick()
    device = service.overview()["device"]
    assert device["observed"]["content_id"] == "demo:golf"
    assert next(p for p in device["plan"] if p["content_id"] == "demo:golf")["actor"] == LEGACY_ADMIN
    assert set_permissions(client, service, False).status_code == 200
    assert public_command(client, service, headers=ORIGIN).status_code == 403


def test_direct_admin_writes_require_same_origin(lan_rig):
    client, service, _ = lan_rig
    response = client.put(
        DEVICE + "/public-access",
        headers={"origin": "http://other-server"},
        json={
            "command_id": str(uuid4()),
            "expected_revision": revision(service),
            "play_now": True,
            "add_to_plan": True,
        },
    )
    assert response.status_code == 403


async def test_removing_lan_opt_in_restores_admin_protection(lan_rig):
    _, service, settings = lan_rig
    await service.stop()
    app = create_app(replace(settings, admin_auth_mode="proxy"), start_workers=False)
    with TestClient(app) as client:
        assert client.get("/api/v1/overview").status_code in {401, 403}
        assert client.get("/api/public/v1/overview").status_code == 200


def test_lan_admin_does_not_disable_public_proxy_auth(tmp_path):
    app = create_app(
        Settings(database=str(tmp_path / "proxy.sqlite"), admin_auth_mode="trusted-lan"),
        start_workers=False,
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/overview").status_code == 200
        assert client.get("/api/public/v1/overview").status_code == 503


def test_direct_admin_websocket_reaches_application():
    async def echo_actor(scope, receive, send):
        assert scope["state"]["actor"] == LEGACY_ADMIN
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 1000})

    client = TestClient(ProxyAccess(echo_actor, "", "guest", "trusted-lan"))
    with client.websocket_connect(DEVICE + "/control/input"):
        pass


def test_lan_mode_requires_explicit_configuration(monkeypatch):
    monkeypatch.setenv("CONTROLLER_MODE", "demo")
    monkeypatch.delenv("CONTROLLER_ADMIN_AUTH_MODE", raising=False)
    assert Settings.from_env().admin_auth_mode == "proxy"
    monkeypatch.setenv("CONTROLLER_ADMIN_AUTH_MODE", "trusted-lan")
    assert Settings.from_env().admin_auth_mode == "trusted-lan"
    with pytest.raises(ValueError, match="proxy or trusted-lan"):
        Settings(admin_auth_mode="typo")
