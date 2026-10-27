import json

from controller.database import Database, encode
from controller.leagues import DEFAULT_LEAGUES


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
