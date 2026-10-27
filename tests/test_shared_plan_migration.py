import copy
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from controller.config import DeviceSettings, Settings
from controller.fleet import planning_fields
from controller.service import Controller


def registry(settings):
    # New device first must still migrate from the saved primary.
    return replace(settings, devices=(DeviceSettings("second-tv", "Two"),
                                      DeviceSettings("living-room", "One")))


def saved_records(service):
    with service.db.snapshot() as db:
        return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("devices", "metadata", "activity")}


@pytest.mark.parametrize("conflict", ["plan", "completion", "discovery"])
async def test_conflicting_legacy_plans_are_rejected_before_cleanup(tmp_path, conflict):
    settings = Settings(database=str(tmp_path / "legacy.sqlite"))
    service = Controller(settings)
    with service.db.transaction() as db:
        primary = service.db.device(db)
        primary["plan"] = [service.entry("demo:lions"), service.entry("demo:golf")]
        primary["manual_completions"]["demo:lions"] = datetime.now(UTC).isoformat()
        other = copy.deepcopy(primary)
        other["id"] = "second-tv"
        if conflict == "plan":
            other["plan"].reverse()
        elif conflict == "completion":
            other["manual_completions"].clear()
        else:
            other["preferences"]["discovery_leagues"] = ["mlb"]
        service.db.save_device(db, primary)
        service.db.save_device(db, other)
    await service.stop()
    before = saved_records(service)
    with pytest.raises(ValueError, match="Saved stream plans differ.*living-room"):
        Controller(registry(settings))
    assert saved_records(service) == before


async def test_missing_stream_is_rejected_without_pruning_saved_plans(tmp_path):
    settings = registry(Settings(database=str(tmp_path / "missing.sqlite")))
    service = Controller(settings)
    with service.db.transaction() as db:
        device = service.db.device(db, "living-room")
        device["plan"] = [service.entry("demo:lions")]
        device["manual_completions"]["demo:lions"] = datetime.now(UTC).isoformat()
        service.db.save_device(db, device)
    await service.stop()
    before = saved_records(service)
    with pytest.raises(ValueError, match="omits saved streams"):
        Controller(replace(settings, devices=()))
    assert saved_records(service) == before


async def test_stale_projection_cannot_replace_an_existing_canonical_plan(tmp_path):
    settings = registry(Settings(database=str(tmp_path / "stale.sqlite")))
    service = Controller(settings)
    await service.stop()
    # Simulate conflicting persisted input without invoking the normal atomic
    # synchronization hook. An ordinary startup must not treat this as an edit.
    service.db.before_commit = None
    service.db.after_commit = None
    with service.db.transaction() as db:
        device = service.db.device(db, "second-tv")
        device["plan"] = [service.entry("demo:lions")]
        service.db.save_device(db, device)
    before = saved_records(service)
    with pytest.raises(ValueError, match="Saved stream plans differ.*second-tv"):
        Controller(settings)
    assert saved_records(service) == before


async def test_shared_startup_cleanup_updates_canonical_plan_without_resurrection(tmp_path):
    settings = registry(Settings(database=str(tmp_path / "cleanup.sqlite")))
    service = Controller(settings)
    with service.db.transaction() as db:
        device = service.db.device(db, "living-room")
        device["plan"] = [service.entry("demo:lions"), service.entry("demo:golf")]
        device["manual_completions"]["demo:lions"] = datetime.now(UTC).isoformat()
        device["preferences"]["vpn_policy"] = {"enabled": True, "locations": []}
        service.db.save_device(db, device)
        second = service.db.device(db, "second-tv")
        second["automation"] = "paused"
        service.db.save_device(db, second)
    await service.stop()
    for _ in range(2):
        service = Controller(settings)
        try:
            with service.db.transaction() as db:
                for key in service.runtimes:
                    device = service.db.device(db, key)
                    assert [entry["content_id"] for entry in device["plan"]] == ["demo:golf"]
                    assert service.db.meta(db, "fleet_plan") == planning_fields(device)
                second = service.db.device(db, "second-tv")
                assert second["automation"] == "paused"
                assert second["preferences"]["vpn_policy"] == {"enabled": True, "locations": []}
                service.db.set_meta(db, "unrelated_worker", {"state": "idle"})
        finally:
            await service.stop()
