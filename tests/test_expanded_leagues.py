import copy
import json
from uuid import uuid4

import pytest
from test_controller import overview
from test_league_discovery import select

from controller.config import Settings
from controller.database import Database, encode
from controller.fixtures import fixtures
from controller.leagues import DEFAULT_LEAGUES
from controller.service import Controller


def test_schema_13_expands_defaults_preserving_custom_settings_and_opt_out(tmp_path):
    path = str(tmp_path / "upgrade.db")
    previous = [
        "nfl", "college-football", "nhl", "mlb", "nba", "cfl", "uefa.champions",
        "eng.2", "ger.1", "ger.2", "f1",
    ]
    selections = [previous, list(reversed(previous)), ["nfl", "pwhl"], [], list(DEFAULT_LEAGUES)]
    original = Database(path)
    payloads = []
    with original.transaction() as db:
        for index, selection in enumerate(selections):
            payload = {
                "preferences": {"discovery_leagues": selection, "timezone": "America/Vancouver"},
                "plan": [{"content_id": "saved", "source": "user"}],
                "rules": [{"id": "saved-rule", "league": "nhl"}],
                "automation": "paused", "intent_version": 17,
                "observed": {"content_id": "playing"},
            }
            payloads.append(copy.deepcopy(payload))
            db.execute("INSERT INTO devices VALUES (?,?,?)", (str(index), 7, encode(payload)))
        db.execute("PRAGMA user_version=13")
    upgraded = Database(path)
    with upgraded.transaction() as db:
        for device_id, revision, raw in db.execute("SELECT id,revision,payload FROM devices"):
            index = int(device_id)
            expected = payloads[index]
            if index < 2:
                expected["preferences"]["discovery_leagues"] += [
                    league for league in DEFAULT_LEAGUES if league not in previous
                ]
            assert json.loads(raw) == expected
            assert revision == (8 if index < 2 else 7)
        # A subsequent opt-out must survive another startup.
        payloads[0]["preferences"]["discovery_leagues"] = previous
        db.execute("UPDATE devices SET payload=? WHERE id='0'", (encode(payloads[0]),))
    reopened = Database(path)
    with reopened.transaction() as db:
        row = db.execute("SELECT revision,payload FROM devices WHERE id='0'").fetchone()
        assert row["revision"] == 8
        assert json.loads(row["payload"]) == payloads[0]


def test_expanded_selection_round_trips_configuration_and_undo(rig):
    client, _, _ = rig
    assert len(DEFAULT_LEAGUES) == 38
    assert select(client, list(DEFAULT_LEAGUES)).status_code == 200
    exported = client.get("/api/v1/devices/living-room/configuration").json()
    assert exported["configuration"]["preferences"]["discovery_leagues"] == list(DEFAULT_LEAGUES)
    assert select(client, ["pwhl"]).status_code == 200
    state = overview(client)
    assert client.post("/api/v1/devices/living-room/undo", json={
        "command_id": str(uuid4()), "expected_revision": state["device"]["revision"],
        "history_id": state["undo"]["id"],
    }).status_code == 200
    assert overview(client)["device"]["preferences"]["discovery_leagues"] == list(DEFAULT_LEAGUES)


@pytest.mark.parametrize("fail_second", [False, True])
async def test_expanded_catalog_batches_are_atomic_and_deduplicate_shared_sources(tmp_path, fail_second):
    service = Controller(Settings(
        database=str(tmp_path / "catalog.db"), mode="teamarr", teamarr_url="http://teamarr",
    ))
    original = fixtures()[0][1]
    shared = copy.deepcopy(original)
    shared["id"] = "shared-broadcast"
    calls = []

    async def snapshot(leagues):
        calls.append(leagues)
        assert len(leagues) <= 20
        if fail_second and len(calls) == 2:
            raise RuntimeError("second batch unavailable")
        entries = []
        for league in leagues:
            entry = copy.deepcopy(original)
            entry.update(id=f"event:{league}", competition=league)
            entry["event"]["league"] = league
            entries.append(entry)
        return entries + [shared], 1

    try:
        with service.db.transaction() as db:
            service.replace_catalog(db, [original], "teamarr")
        service.client.fetch_snapshot = snapshot
        await service.refresh_catalog()
        assert calls == [sorted(DEFAULT_LEAGUES)[:20], sorted(DEFAULT_LEAGUES)[20:]]
        with service.db.transaction() as db:
            active = {row["id"] for row in db.execute("SELECT id FROM contents WHERE active=1")}
            if fail_second:
                assert active == {original["id"]}
                assert service.db.meta(db, "feed_health")["state"] == "degraded"
            else:
                assert active == {f"event:{league}" for league in DEFAULT_LEAGUES} | {shared["id"]}
    finally:
        await service.stop()
