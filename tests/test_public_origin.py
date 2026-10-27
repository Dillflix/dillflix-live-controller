"""TLS termination keeps origin checks independent of browser authentication."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from controller.access import ProxyAccess
from controller.config import Settings

PUBLIC = "/api/public/v1/streams/sessions"
ORIGIN = "https://live-controller.example.com"
HEADERS = {"host": "live-controller.example.com", "origin": ORIGIN,
           "sec-fetch-site": "same-origin"}


def app_for(settings):
    app = FastAPI()
    app.add_middleware(ProxyAccess, secret=settings.proxy_secret,
                       public_auth_mode=settings.public_auth_mode,
                       admin_auth_mode=settings.admin_auth_mode,
                       public_origin=settings.public_origin)

    @app.post(PUBLIC)
    @app.post("/api/v1/admin")
    def action():
        return {"accepted": True}

    return app


def test_explicit_public_origin_accepts_https_behind_http_and_preserves_lan():
    with TestClient(app_for(Settings(public_auth_mode="guest", public_origin=ORIGIN))) as client:
        assert client.post(PUBLIC, headers=HEADERS).status_code == 200
        assert client.post(PUBLIC, headers={"origin": "http://testserver"}).status_code == 200


@pytest.mark.parametrize("headers", [
    {**HEADERS, "origin": "https://sibling.example.com"},
    {**HEADERS, "origin": "https://live-controller.example.com:444"},
    {**HEADERS, "origin": ORIGIN + "/path"},
    {**HEADERS, "origin": ORIGIN + "?query"},
    {**HEADERS, "origin": ORIGIN + "#fragment"},
    {**HEADERS, "origin": "null"},
    {**HEADERS, "origin": ""},
    {**HEADERS, "host": "testserver", "x-forwarded-host": "live-controller.example.com"},
    {**HEADERS, "sec-fetch-site": "same-site"},
    {**HEADERS, "sec-fetch-site": "cross-site"},
])
def test_public_origin_does_not_allow_cross_origin_requests(headers):
    with TestClient(app_for(Settings(public_auth_mode="guest", public_origin=ORIGIN))) as client:
        response = client.post(PUBLIC, headers=headers)
        assert response.status_code == 403
        assert response.json()["detail"] == "Actions require the same origin."


def test_forwarded_proto_still_requires_authenticated_proxy_without_explicit_origin():
    with TestClient(app_for(Settings(public_auth_mode="guest", proxy_secret="secret"))) as client:
        headers = {**HEADERS, "x-forwarded-proto": "https"}
        assert client.post(PUBLIC, headers=headers).status_code == 403
        assert client.post(PUBLIC, headers={**headers, "x-dillflix-proxy-key": "wrong"}).status_code == 403
        assert client.post(PUBLIC, headers={**headers, "x-dillflix-proxy-key": "secret"}).status_code == 200


def test_public_origin_does_not_bypass_authentication_or_extend_admin_origins():
    settings = Settings(proxy_secret="secret", public_origin=ORIGIN)
    with TestClient(app_for(settings)) as client:
        assert client.post(PUBLIC, headers=HEADERS).status_code == 401
        identity = {**HEADERS, "x-dillflix-proxy-key": "secret",
                    "x-dillflix-user": "viewer", "x-dillflix-role": "user"}
        assert client.post(PUBLIC, headers=identity).status_code == 200
        assert client.post("/api/v1/admin", headers=identity).status_code == 403
        assert client.post("/api/v1/admin", headers={**identity, "x-dillflix-role": "admin"}).status_code == 403
    settings = Settings(public_auth_mode="guest", admin_auth_mode="trusted-lan", public_origin=ORIGIN)
    with TestClient(app_for(settings)) as client:
        assert client.post("/api/v1/admin", headers=HEADERS).status_code == 403


def test_public_origin_environment(monkeypatch):
    monkeypatch.setenv("CONTROLLER_PUBLIC_ORIGIN", ORIGIN + "/")
    assert Settings.from_env().public_origin == ORIGIN


@pytest.mark.parametrize("origin", [
    "ftp://example.com", "https://", "example.com", "https://user:pass@example.com",
    "https://example.com/public/", "https://example.com?x", "https://example.com#x",
    "https://*.example.com", "https://example.com:70000", "https://example.com:0",
    "https://example.com\n", " https://example.com", "https://example.com\\evil",
    "https://[invalid", "https://example.com,https://other.example.com",
])
def test_invalid_public_origins_fail_configuration(origin):
    with pytest.raises(ValueError, match="CONTROLLER_PUBLIC_ORIGIN"):
        Settings(public_origin=origin)
