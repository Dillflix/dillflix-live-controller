import copy
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from test_controller import overview

from controller.config import Settings
from controller.fixtures import fixtures
from controller.leagues import DEFAULT_LEAGUES, LEAGUE_NAMES
from controller.models import Rule
from controller.service import Controller
from controller.teamarr import TeamarrClient


@pytest.mark.parametrize("season,priority", [("regular", 1), ("postseason", 0)])
@pytest.mark.parametrize("league,sport,away,home", [
    ("college-football", "football", "Michigan Wolverines", "Ohio State Buckeyes"),
    ("eng.2", "soccer", "Wolves", "Burnley"),
    ("ger.1", "soccer", "K\u00f6ln", "Eintracht Frankfurt"),
    ("ger.2", "soccer", "1. FC N\u00fcrnberg", "Energie Cottbus"),
])
async def test_league_feed_reaches_priorities_team_directory_and_playback_request(
    tmp_path, season, priority, league, sport, away, home,
):
    service = Controller(Settings(
        database=str(tmp_path / "college.db"), mode="teamarr", teamarr_url="http://teamarr"
    ))
    entry = copy.deepcopy(fixtures()[0][1])
    entry.pop("_simulation")
    entry.update(
        id=f"event:espn:{league}:fixture",
        provider="espn", competition=league, sports=[sport],
        title=f"{away} at {home}", status="live",
        start_time=(datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
        expected_end_time=(datetime.now(UTC) + timedelta(hours=3)).isoformat(),
        viewing_options=[{
            "id": f"route:{league}:prime_video", "app": "prime_video",
            "decision": "eligible", "basis": "configured_route",
            "reasons": ["user_configured_league_route"],
        }],
    )
    entry["event"].update(
        league=league, sport=sport, provider="espn", event_id="fixture", season_type=season,
        home_team=home, away_team=away,
    )
    for field, team_id, name in [
        ("away_team_details", "130", away),
        ("home_team_details", "194", home),
    ]:
        entry["event"][field].update(
            id=team_id, provider="espn", full_name=name, short_name=name,
            name=name, city=None, abbreviation="MICH" if team_id == "130" else "OSU",
        )
    requests = []

    def handler(request):
        requests.append(request.url)
        if request.url.path.endswith("/teams"):
            return httpx.Response(200, json=[{
                "provider_team_id": "130", "provider": "espn",
                "league": league, "team_name": away,
                "team_abbrev": "MICH",
            }])
        assert request.url.params.get_list("league") == [league]
        return httpx.Response(200, json={"schema_version": 1, "items": [entry], "next_cursor": None})

    try:
        service.client = TeamarrClient("http://teamarr", transport=httpx.MockTransport(handler))
        with service.db.transaction() as db:
            device = service.db.device(db)
            device["preferences"]["discovery_leagues"] = [league]
            device["rules"] = [
                Rule(id="cfb-postseason", name="College postseason", league=league, phase="playoffs").model_dump(),
                Rule(id="cfb-regular", name="College regular season", league=league, phase="regular").model_dump(),
            ]
            service.db.save_device(db, device)
        await service.refresh_catalog()
        await service.refresh_team_directory()
        view = service.overview()
        assert view["meta"]["league_choices"][league] == LEAGUE_NAMES[league]
        assert view["events"][0]["priority"] == priority
        assert view["events"][0]["playable"] is True
        assert any(t["key"] == f"espn:{league}:130" for t in view["teams"])
        assert any(url.path.endswith(f"/cache/leagues/{league}/teams") for url in requests)
        service.tick()
        with service.db.transaction() as db:
            row = db.execute("SELECT payload FROM jobs ORDER BY rowid DESC LIMIT 1").fetchone()
            issued = json.loads(row["payload"])
        assert issued["content_id"] == entry["id"]
        assert issued["content_snapshot"] == entry
        assert issued["allowed_viewing_options"] == entry["viewing_options"]
    finally:
        await service.stop()


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
    await client.fetch_snapshot(leagues=["college-football", "cfl", "uefa.champions", "f1"])
    assert requests[0].get_list("league") == ["college-football", "cfl", "uefa.champions", "f1"]
    assert dict(requests[1]) == {"cursor": "next"}
    assert requests[2].get_list("league") == requests[0].get_list("league")
    await client.fetch_snapshot(leagues=[])
    assert "league" not in requests[-1]
    assert requests[-1].get_list("source") == ["nfl_redzone", "golf", "special_events", "dazn_tennis"]


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
