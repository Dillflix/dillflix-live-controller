"""Fence the fallback job an older controller could issue during manual backoff."""
from datetime import UTC, datetime, timedelta

from test_controller import overview
from test_playback import jobs, pending


def test_existing_fallback_is_cancelled_then_only_manual_event_is_retried(rig, monkeypatch):
    client, service, settings = rig
    original = pending(rig)
    with service.db.transaction() as db:
        device = service.db.device(db)
        service.fail_attempt(db, device, original, "Resolver refused playback")
        service.db.save_device(db, device)
    # Reproduce the already-deployed controller's fallback selection.
    with monkeypatch.context() as scoped:
        scoped.setattr("controller.coordinator.choose", lambda *args: {
            "content_id": "demo:golf", "manual": False, "reason": "Old fallback", "rule_id": None
        })
        service.stage_playback()
    fallback = jobs(client)[0]
    service.playback.submit(fallback["payload"])
    intent = overview(client)["device"]["intent_version"]
    object.__setattr__(settings, "simulation_delay", 0)
    service.tick()
    device = overview(client)["device"]
    assert device["desired"] == original["content_id"]
    assert device["intent_version"] == intent + 1
    assert device["playback_state"] == "failed"
    assert len(jobs(client)) == 2
    cancelled = next(j for j in jobs(client) if j["id"] == fallback["id"])
    assert cancelled["state"] == "superseded" and cancelled["cancel_sent"]
    assert service.playback.inspect(fallback["id"])["state"] == "cancelled"
    service.tick()
    assert overview(client)["device"]["intent_version"] == device["intent_version"]
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["failures"][original["content_id"]]["retry_after"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        service.db.save_device(db, device)
    service.tick()
    assert len(jobs(client)) == 3
    assert overview(client)["device"]["observed"]["content_id"] == original["content_id"]
