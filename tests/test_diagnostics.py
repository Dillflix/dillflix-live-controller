import json

import httpx
import pytest

from controller.api import create_app
from controller.config import Settings
from controller.prime_player.diagnostics import collect, redact


async def test_export_is_read_only_and_keeps_device_scope(tmp_path):
    app = create_app(Settings(database=str(tmp_path / "state.sqlite")), start_workers=False)
    service = app.state.controller
    try:
        with service.db.transaction() as db:
            service.db.log(db, "2026-10-03T08:00:00Z", "Decision", "detail", "test", "living-room")
            service.db.log(db, "2026-10-03T08:00:00Z", "Other device", "hidden", "test", "other")
        before = (tmp_path / "state.sqlite").read_bytes()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/devices/living-room/diagnostics")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            bundle = response.json()
            assert bundle["prime_player"]["state"] == "not_configured"
            assert any(r["message"] == "Decision" for r in bundle["activity"])
            assert not any(r["message"] == "Other device" for r in bundle["activity"])
            assert (await client.get("/api/v1/devices/missing/diagnostics")).status_code == 404
        assert (tmp_path / "state.sqlite").read_bytes() == before
    finally:
        await service.stop()


def test_redaction_preserves_correlation_ids():
    value = redact(
        {
            "token": "job-token",
            "request_id": "request",
            "nested": [{"api_key": "private", "cookie": "private"}],
        }
    )
    assert value["token"] == "job-token"
    assert "private" not in json.dumps(value)


async def test_manual_handoff_records_failure_and_can_recover(tmp_path):
    app = create_app(Settings(database=str(tmp_path / "state.sqlite")), start_workers=False)
    service = app.state.controller
    try:
        with service.db.transaction() as db:
            device = service.db.device(db)
            device["input_handoff"] = {"through_intent_version": device["intent_version"]}
            service.db.save_device(db, device)

        def blocked(*args):
            raise ConnectionError("Player acknowledgement missing")

        service.playback.cancel_device = blocked
        for _ in range(2):
            with pytest.raises(ConnectionError):
                service.reconcile_input_handoff()
        bundle = collect(service.settings.database)
        assert bundle["device"]["input_handoff"]["error"] == "Player acknowledgement missing"
        assert len([r for r in bundle["activity"] if r["message"] == "Manual handoff blocked"]) == 1
        service.playback.cancel_device = lambda *args: None
        service.reconcile_input_handoff()
        assert collect(service.settings.database)["device"]["input_handoff"] is None
    finally:
        await service.stop()
