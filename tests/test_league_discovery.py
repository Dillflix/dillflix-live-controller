import copy
from uuid import uuid4

import httpx
import pytest
from test_controller import overview

from controller.config import Settings
from controller.fixtures import fixtures
from controller.leagues import DEFAULT_LEAGUES
from controller.service import Controller
from controller.teamarr import TeamarrClient


def select(client, leagues):
    device = overview(client)["device"]
    return client.put("/api/v1/devices/living-room/rules", json={
        "command_id": str(uuid4()), "expected_revision": device["revision"],
        "rules": device["rules"], "team_ranks": device["team_ranks"],
        "preferences": {**device["preferences"], "discovery_leagues": leagues},
    })


def test_selection_hides_discovery_but_retains_commitments_and_supports_undo(rig):
    client, service, _ = rig
    before = overview(client)
    assert before["device"]["preferences"]["discovery_leagues"] == list(DEFAULT_LEAGUES)
    assert select(client, ["nba"]).status_code == 200
    after = overview(client)
    assert after["device"]["plan"] == before["device"]["plan"]
    assert {e["content_id"] for e in after["events"]} == {
        "demo:celtics", "demo:canadiens", "demo:redzone", "demo:golf",
    }
    with service.db.transaction() as db:
        assert service.requested_leagues(db) == ["nba", "nhl"]
    exported = client.get("/api/v1/devices/living-room/configuration").json()
    assert exported["configuration"]["preferences"]["discovery_leagues"] == ["nba"]
    assert client.post("/api/v1/devices/living-room/undo", json={
        "command_id": str(uuid4()), "expected_revision": after["device"]["revision"],
        "history_id": after["undo"]["id"],
    }).status_code == 200
    assert overview(client)["device"]["preferences"] == before["device"]["preferences"]


@pytest.mark.parametrize("leagues", [["nba", "nba"], [" "], ["nba&source=games"], ["x"] * 21])
def test_invalid_selection_does_not_change_configuration(rig, leagues):
    client, _, _ = rig
    before = overview(client)["device"]
    assert select(client, leagues).status_code == 422
    assert overview(client)["device"] == before


async def test_selected_leagues_survive_cursor_expiry_and_cursor_pages_use_no_filters():
    requests = []
    entry = fixtures()[0][1]

    def handler(request):
        requests.append(request.url.params)
        if len(requests) == 2:
            return httpx.Response(410)
        return httpx.Response(200, json={
            "schema_version": 1, "items": [entry],
            "next_cursor": "next" if len(requests) == 1 else None,
        })

    client = TeamarrClient("http://teamarr", transport=httpx.MockTransport(handler))
    await client.fetch_snapshot(leagues=["cfl", "uefa.champions", "f1"])
    assert requests[0].get_list("league") == ["cfl", "uefa.champions", "f1"]
    assert dict(requests[1]) == {"cursor": "next"}
    assert requests[2].get_list("league") == requests[0].get_list("league")
    await client.fetch_snapshot(leagues=[])
    assert "league" not in requests[-1]
    assert requests[-1].get_list("source") == ["nfl_redzone", "golf", "special_events"]


async def test_inflight_old_selection_cannot_replace_catalog(tmp_path):
    service = Controller(Settings(database=str(tmp_path / "feed.db"), mode="teamarr", teamarr_url="http://teamarr"))
    try:
        original = fixtures()[0][1]
        with service.db.transaction() as db:
            service.replace_catalog(db, [original], "teamarr")
            before = service.db.meta(db, "feed_health")

        async def snapshot(leagues):
            assert leagues == sorted(DEFAULT_LEAGUES)
            with service.db.transaction() as db:
                device = service.db.device(db)
                device["preferences"]["discovery_leagues"] = ["cfl"]
                service.db.save_device(db, device)
            return [], 1

        service.client.fetch_snapshot = snapshot
        await service.refresh_catalog()
        with service.db.transaction() as db:
            assert service.db.meta(db, "feed_health") == before
            assert db.execute("SELECT active FROM contents WHERE id=?", (original["id"],)).fetchone()[0] == 1
    finally:
        await service.stop()


def test_shared_feed_union_stays_device_scoped(rig):
    client, service, _ = rig
    with service.db.transaction() as db:
        other = copy.deepcopy(service.db.device(db))
        other.update(id="bedroom", plan=[])
        other["preferences"]["discovery_leagues"] = ["mlb"]
        service.db.save_device(db, other)
    assert select(client, []).status_code == 200
    with service.db.transaction() as db:
        assert service.requested_leagues(db) == ["mlb", "nhl"]
    assert "demo:jays" not in {e["content_id"] for e in overview(client)["events"]}
    assert "demo:jays" in {e["content_id"] for e in service.overview("bedroom")["events"]}


async def test_selection_survives_restart_and_legacy_settings_edits(rig):
    client, _, settings = rig
    assert select(client, ["cfl", "f1"]).status_code == 200
    device = overview(client)["device"]
    legacy = {k: v for k, v in device["preferences"].items() if k != "discovery_leagues"}
    legacy["minimum_viewing_seconds"] = 600
    body = {
        "command_id": str(uuid4()), "expected_revision": device["revision"],
        "rules": device["rules"], "team_ranks": device["team_ranks"], "preferences": legacy,
    }
    response = client.put("/api/v1/devices/living-room/rules", json=body)
    assert response.status_code == 200
    assert client.put("/api/v1/devices/living-room/rules", json=body).json() == response.json()
    restarted = Controller(settings)
    try:
        preferences = restarted.overview()["device"]["preferences"]
        assert preferences["discovery_leagues"] == ["cfl", "f1"]
        assert preferences["minimum_viewing_seconds"] == 600
    finally:
        await restarted.stop()
