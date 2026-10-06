import pytest

from controller.database import Database
from controller.plex.state import configuration


@pytest.mark.parametrize("lineage", ["public", "plex"])
def test_schema_nine_lineages_upgrade_without_losing_state(rig, lineage):
    _, service, settings = rig
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["public_access"] = {"play_now": True, "add_to_plan": False}
        device["plan"].append(service.entry("demo:lions", {
            "type": "user", "id": "guest:anonymous", "name": "Guest",
        }))
        service.db.save_device(db, device)
        if lineage == "public":
            for table in ("plex_settings", "plex_runtime", "plex_assets", "plex_commands"):
                db.execute("DROP TABLE " + table)
        else:
            db.execute("INSERT INTO plex_assets VALUES ('saved', X'0102', 'image/png', 1, 1)")
        db.execute("PRAGMA user_version=9")
    upgraded = Database(settings.database)
    with upgraded.transaction() as db:
        assert upgraded.device(db) == device
        assert db.execute("PRAGMA user_version").fetchone()[0] == 10
        assert not configuration(db, "living-room")["enabled"]
        for table in ("plex_settings", "plex_runtime", "plex_assets", "plex_commands"):
            db.execute("SELECT * FROM " + table).fetchall()
        if lineage == "plex":
            assert db.execute("SELECT data FROM plex_assets WHERE digest='saved'").fetchone()[0] == b"\x01\x02"
