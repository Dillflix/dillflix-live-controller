from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from controller.content_status import lifecycle_view, source_observation
from controller.fixtures import fixtures
from controller.teamarr import TeamarrClient


def tennis():
    return next(x for x in fixtures("tennis")[0] if x["id"] == "demo:tennis")


def test_tennis_is_visible_and_reservable_with_no_players_or_end_time(rig):
    client, service, _ = rig
    assert (
        client.post("/api/v1/simulation", json={"action": "scenario", "scenario": "tennis"}).status_code
        == 200
    )
    view = client.get("/api/v1/overview").json()
    item = next(e for e in view["events"] if e["content_id"] == "demo:tennis")
    assert item["teams"] == [] and item["kind"] == "broadcast"
    assert item["expected_end_time"] is None and item["playable"]
    assert item["viewing_options"][0]["channel"] == "DAZN"
    response = client.post(
        "/api/v1/devices/living-room/watch-plan",
        json={
            "command_id": str(uuid4()),
            "expected_revision": view["device"]["revision"],
            "action": {"type": "add", "content_id": "demo:tennis", "priority": "first"},
        },
    )
    assert response.status_code == 200
    view = client.get("/api/v1/overview").json()
    assert view["device"]["plan"][0]["content_id"] == "demo:tennis"


def test_cached_dazn_label_does_not_get_freshness_from_another_feed_read():
    now = datetime.now(UTC)
    acquired = now - timedelta(minutes=5)
    snapshot = {**tennis(), "status": "live", "status_received_at": acquired.isoformat()}
    observation = source_observation(snapshot, now.isoformat(), now, now, "teamarr", 120)
    assert observation["received_at"] == acquired.isoformat()
    assert observation["observed_at"] is None
    assert lifecycle_view(observation, None, now, 120)["state"] == "unknown"


async def test_teamarr_tennis_round_trip_preserves_source_metadata_and_freshness():
    snapshot = {**tennis(), "status_received_at": datetime.now(UTC).isoformat()}
    client = TeamarrClient(
        "http://teamarr",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"schema_version": 1, "items": [snapshot]})
        ),
    )
    items, _ = await client.fetch_snapshot(leagues=[])
    assert items == [snapshot]


@pytest.mark.parametrize("timestamp", ["2026-10-03T01:00:00", "bad", "2999-01-01T00:00:00Z"])
async def test_invalid_status_acquisition_time_rejects_snapshot(timestamp):
    snapshot = {**tennis(), "status_received_at": timestamp}
    client = TeamarrClient(
        "http://teamarr",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"schema_version": 1, "items": [snapshot]})
        ),
    )
    with pytest.raises(ValueError):
        await client.fetch_snapshot()
