import time

import pytest
from broadcast_model import FixtureBroadcastModel
from fastapi.testclient import TestClient
from playback_fixtures import HEADERS, payload, settings
from test_prime_workflow import Player

from controller.api import create_app
from controller.executor.models import CancelResult, PlaybackReport


@pytest.fixture
def api(tmp_path):
    app = create_app(settings(tmp_path), start_workers=False)
    engine = app.state.controller.executor
    engine.matcher.model = FixtureBroadcastModel()
    engine.player = Player()
    with TestClient(app) as client:
        yield client, app.state.controller, engine


def test_api_durable_play_get_active_cancel_and_authentication(api):
    client, service, engine = api
    request = payload()
    assert client.post("/v1/playbacks", json=request).status_code == 401
    result = client.post("/v1/playbacks", json=request, headers=HEADERS)
    assert result.status_code == 202, result.text
    token = result.json()["token"]
    PlaybackReport.model_validate(result.json())
    assert client.post("/v1/playbacks", json=request, headers=HEADERS).json()["token"] == token
    assert (
        client.post(
            "/v1/playbacks",
            json={**request, "content_snapshot": {**request["content_snapshot"], "title": "changed"}},
            headers=HEADERS,
        ).status_code
        == 409
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        report = client.get("/v1/playbacks/" + token, headers=HEADERS).json()
        if report["operation"]["state"] == "playing_verified":
            break
        time.sleep(0.02)
    assert report["operation"]["state"] == "playing_verified", report
    PlaybackReport.model_validate(report)
    assert report["observation"]["simulated"] is False and report["observation"]["verified"]
    assert [c[0] for c in engine.player.calls].count("play") == 1
    cancelled = client.post(
        "/v1/playbacks/cancel", headers=HEADERS, json={"device_id": "living-room", "token": token}
    )
    assert cancelled.status_code == 200 and cancelled.json()["input_quiescent"]
    CancelResult.model_validate(cancelled.json())
    assert engine.player.calls[-1][0] == "stop"
    again = client.post(
        "/v1/playbacks/cancel", headers=HEADERS, json={"device_id": "living-room", "token": token}
    )
    assert again.status_code == 200 and [c[0] for c in engine.player.calls].count("stop") == 1
    report = client.get("/v1/playbacks/" + token, headers=HEADERS).json()
    assert report["observation"] is None
    assert report["content_status"]["effective_state"] != "ended"
    assert report["cancellation"]["state"] == "acknowledged"
    assert service.overview()["meta"]["playback_adapter"] == "prime-player"


def test_body_limits_validation_and_token_errors(api):
    client, _, _ = api
    assert client.get("/v1/playbacks/absent", headers=HEADERS).status_code == 404
    for change in (
        {"mode": "replay"},
        {"device_id": "different"},
        {"intent_version": -1},
        {"content_id": "different"},
    ):
        assert client.post("/v1/playbacks", headers=HEADERS, json={**payload(), **change}).status_code == 422
    assert (
        client.post("/v1/playbacks/cancel", headers=HEADERS, json={"device_id": "living-room"}).status_code
        == 422
    )
    assert (
        client.post("/v1/playbacks", headers=HEADERS, content=b"x" * (2 * 1024 * 1024 + 1)).status_code == 413
    )
