"""A failed automatic candidate must not repeatedly cancel its fallback search."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from test_controller import overview
from test_playback import jobs, successful_report


def automatic_fallback(rig):
    client, service, settings = rig
    object.__setattr__(settings, "simulation_delay", 100)
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["plan"] = []
        service.db.save_device(db, device)
    service.stage_playback()
    failed = jobs(client)[0]
    with service.db.transaction() as db:
        device = service.db.device(db)
        service.fail_attempt(db, device, failed, "Temporary transport failure")
        service.db.save_device(db, device)
    service.stage_playback()
    fallback = jobs(client)[0]
    assert fallback["content_id"] != failed["content_id"]
    return failed, fallback


def test_expiring_retry_does_not_cancel_pending_fallback_and_result_is_accepted(rig):
    client, service, _ = rig
    failed, fallback = automatic_fallback(rig)
    report = successful_report(service, fallback)
    with patch("controller.coordinator.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime.now(UTC) + timedelta(seconds=10)
        service.stage_playback()
        assert overview(client)["device"]["desired"] == fallback["content_id"]
        assert len(jobs(client)) == 2
        assert service.receive_playback_report(fallback["id"], report)
    assert jobs(client)[0]["state"] == "verified"


@pytest.mark.parametrize("change", ["manual", "revision", "deadline", "intent", "route"])
def test_fallback_retry_protection_does_not_override_new_authority(rig, change):
    from controller.planner import choose

    client, service, _ = rig
    failed, fallback = automatic_fallback(rig)
    with service.db.transaction() as db:
        device = service.db.device(db)
        items = service.items(db)
        if change == "manual":
            device["plan"] = [{"content_id": failed["content_id"]}]
        elif change == "revision":
            device["revision"] += 1
        elif change == "deadline":
            device["automatic_attempt"]["deadline_at"] = 0
        elif change == "intent":
            device["intent_version"] += 1
        else:
            next(i for i in items if i["content_id"] == fallback["content_id"])["viewing_options"] = []
        selected = choose(device, items, service.now(db), datetime.now(UTC) + timedelta(seconds=10))
    assert selected["content_id"] == failed["content_id"]
