import json

import pytest
from fastapi.testclient import TestClient

from controller.api import create_app
from controller.config import DeviceSettings, Settings

from controller.database import Database, encode
from controller.leagues import DEFAULT_LEAGUES


@pytest.mark.parametrize("version", [18, 19])
@pytest.mark.parametrize("selection", ["defaults", "reordered", "custom", "empty"])
def test_cup_migration_two_stream_api_restarts(tmp_path, version, selection):
    settings = Settings(
        database=str(tmp_path / "fleet.db"), admin_auth_mode="trusted-lan",
        devices=(DeviceSettings("living-room", "Dillflix 1"),
                 DeviceSettings("stream-2", "Dillflix 2")),
    )
    cups = ["eng.league_cup", "ger.dfb_pokal"]
    previous = [league for league in DEFAULT_LEAGUES if league not in cups]
    selected = {"defaults": previous, "reordered": previous[::-1],
                "custom": ["nfl"], "empty": []}[selection]
    expanded = selected + cups if selection in {"defaults", "reordered"} else selected
    app = create_app(settings, start_workers=False)
    with TestClient(app):
        pass
    database = Database(settings.database)
    with database.transaction() as db:
        canonical = database.meta(db, "fleet_plan")
        canonical["preferences"]["discovery_leagues"] = selected
        database.set_meta(db, "fleet_plan", canonical)
        for device_id in ("living-room", "stream-2"):
            device = database.device(db, device_id)
            device["preferences"]["discovery_leagues"] = expanded if version == 19 else selected
            database.save_device(db, device)
        db.execute(f"PRAGMA user_version={version}")
        preserved = {key: canonical[key] for key in canonical if key != "preferences"}
    for _ in range(2):
        app = create_app(settings, start_workers=False)
        with TestClient(app) as client:
            for device_id in ("living-room", "stream-2"):
                response = client.get("/api/v1/overview", params={"device_id": device_id})
                assert response.status_code == 200, response.text
                assert response.json()["device"]["preferences"]["discovery_leagues"] == expanded
        with database.transaction() as db:
            shared = database.meta(db, "fleet_plan")
            assert shared["preferences"]["discovery_leagues"] == expanded
            assert {key: shared[key] for key in preserved} == preserved


def test_cup_repair_does_not_hide_unrelated_shared_plan_conflicts(tmp_path):
    from controller.fixtures import default_device
    from controller.fleet import planning_fields

    path = str(tmp_path / "conflict.db")
    database = Database(path)
    cups = ["eng.league_cup", "ger.dfb_pokal"]
    previous = [league for league in DEFAULT_LEAGUES if league not in cups]
    device = default_device("demo")
    device["preferences"]["discovery_leagues"] = previous
    canonical = planning_fields(device)
    device["preferences"]["discovery_leagues"] = previous + cups
    device["preferences"]["timezone"] = "Europe/London"
    with database.transaction() as db:
        database.save_device(db, device)
        database.set_meta(db, "fleet_plan", canonical)
        db.execute("PRAGMA user_version=19")
    repaired = Database(path)
    with repaired.transaction() as db:
        assert repaired.meta(db, "fleet_plan") == canonical
        assert repaired.device(db)["preferences"] == device["preferences"]


def test_cup_upgrade_preserves_custom_selections_and_restarts(tmp_path):
    path = str(tmp_path / "cups.db")
    cups = ["eng.league_cup", "ger.dfb_pokal"]
    previous = [league for league in DEFAULT_LEAGUES if league not in cups]
    selections = [previous, list(reversed(previous)), [], ["nfl"], previous + cups[:1]]
    original = Database(path)
    expected = {}
    with original.transaction() as db:
        for index, selection in enumerate(selections):
            payload = {
                "preferences": {"discovery_leagues": selection, "timezone": "America/Vancouver"},
                "plan": [{"content_id": "saved"}], "automation": "paused",
                "rules": [{"league": "nhl"}], "intent_version": 17,
            }
            db.execute("INSERT INTO devices VALUES (?,?,?)", (str(index), 7, encode(payload)))
            if index < 2:
                payload["preferences"]["discovery_leagues"] = selection + cups
            expected[str(index)] = payload
        db.execute("PRAGMA user_version=18")
    for _ in range(2):
        upgraded = Database(path)
        with upgraded.transaction() as db:
            for device_id, revision, raw in db.execute("SELECT id,revision,payload FROM devices"):
                assert json.loads(raw) == expected[device_id]
                assert revision == (8 if int(device_id) < 2 else 7)
            assert db.execute("PRAGMA user_version").fetchone()[0] == Database.SCHEMA_VERSION
    with upgraded.transaction() as db:
        expected["0"]["preferences"]["discovery_leagues"] = previous
        db.execute("UPDATE devices SET payload=? WHERE id='0'", (encode(expected["0"]),))
    reopened = Database(path)
    with reopened.transaction() as db:
        raw = db.execute("SELECT payload FROM devices WHERE id='0'").fetchone()[0]
        assert json.loads(raw) == expected["0"]


def test_soccer_upgrade_expands_only_old_defaults_once(tmp_path):
    path = str(tmp_path / "upgrade.db")
    original = Database(path)
    old_defaults = ["nfl", "college-football", "nhl", "mlb", "nba", "cfl", "uefa.champions", "f1"]
    selections = {
        "defaults": old_defaults,
        "reordered": list(reversed(old_defaults)),
        "custom": ["nfl", "ger.1"],
        "empty": [],
    }
    payloads = {}
    with original.transaction() as db:
        for device_id, selected in selections.items():
            payload = {
                "preferences": {"discovery_leagues": selected, "timezone": "America/Vancouver"},
                "plan": [{"content_id": "saved", "source": "admin"}],
                "rules": [{"id": "saved-rule", "league": "nhl"}],
                "automation": "paused", "intent_version": 17,
                "observed": {"content_id": "playing"},
            }
            payloads[device_id] = payload
            db.execute("INSERT INTO devices VALUES (?,?,?)", (device_id, 7, encode(payload)))
        db.execute("PRAGMA user_version=12")

    upgraded = Database(path)
    with upgraded.transaction() as db:
        for device_id, revision, raw in db.execute("SELECT id,revision,payload FROM devices"):
            expected = payloads[device_id]
            expanded = device_id in {"defaults", "reordered"}
            if expanded:
                expected["preferences"]["discovery_leagues"] = selections[device_id] + [
                    league for league in DEFAULT_LEAGUES if league not in old_defaults
                ]
            assert json.loads(raw) == expected
            assert revision == (9 if expanded else 7)
        assert db.execute("PRAGMA user_version").fetchone()[0] == Database.SCHEMA_VERSION
        # A later user opt-out must not be overwritten on the next startup.
        payloads["defaults"]["preferences"]["discovery_leagues"] = old_defaults
        db.execute("UPDATE devices SET payload=? WHERE id='defaults'", (encode(payloads["defaults"]),))
    reopened = Database(path)
    with reopened.transaction() as db:
        raw, revision = db.execute("SELECT payload,revision FROM devices WHERE id='defaults'").fetchone()
        assert json.loads(raw)["preferences"]["discovery_leagues"] == old_defaults
        assert revision == 9
