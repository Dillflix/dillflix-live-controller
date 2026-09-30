import copy
import hashlib
import json
import sqlite3
from uuid import uuid4

import httpx
import pytest
from test_controller import command, overview

from controller.config import Settings
from controller.database import Database, encode
from controller.fixtures import fixtures
from controller.service import Controller
from controller.teamarr import TeamarrClient


def import_request(client, document):
    return {
        "command_id": str(uuid4()),
        "expected_revision": overview(client)["device"]["revision"],
        "document": document,
    }


def undo(client, **overrides):
    data = overview(client)
    body = {
        "command_id": str(uuid4()),
        "expected_revision": data["device"]["revision"],
        "history_id": data["undo"]["id"],
        **overrides,
    }
    return client.post("/api/v1/devices/living-room/undo", json=body)


def test_undo_survives_restart_and_is_idempotent(rig):
    c, _, settings = rig
    original = overview(c)["device"]["plan"]
    command(c, {"type": "remove", "entry_id": original[0]["id"]})
    restarted = Controller(settings)
    assert restarted.overview()["undo"]["description"] == "Remove event from watch plan"
    data = overview(c)
    args = {
        "command_id": str(uuid4()),
        "expected_revision": data["device"]["revision"],
        "history_id": data["undo"]["id"],
    }
    assert undo(c, **args).status_code == 200
    # Retrying the same command works after the undo stack has advanced.
    response = c.post("/api/v1/devices/living-room/undo", json=args)
    assert response.status_code == 200
    data = overview(c)
    assert data["device"]["plan"] == original
    assert data["device"]["revision"] == 2
    assert data["undo"] is None


def test_undo_refuses_an_older_edit_even_with_current_revision(rig):
    c, _, _ = rig
    command(c, {"type": "add", "content_id": "demo:jays"})
    older = overview(c)["undo"]
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    assert undo(c, history_id=older["id"]).status_code == 409
    assert overview(c)["device"]["plan"][0]["content_id"] == "demo:lions"


def test_undo_stack_handles_interleaved_plan_and_configuration_edits(rig):
    c, _, _ = rig
    original = overview(c)["device"]
    command(c, {"type": "add", "content_id": "demo:jays"})
    document = c.get("/api/v1/devices/living-room/configuration").json()
    document["configuration"]["preferences"]["minimum_viewing_seconds"] = 900
    c.post("/api/v1/devices/living-room/configuration/import", json=import_request(c, document))
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    for _ in range(3):
        assert undo(c).status_code == 200
    after = overview(c)
    assert after["undo"] is None
    assert after["device"]["plan"] == original["plan"]
    assert after["device"]["preferences"] == original["preferences"]


def test_undo_preserves_pause_and_does_not_replay_completed_content(rig):
    c, s, _ = rig
    original = overview(c)["device"]["plan"]
    command(c, {"type": "remove", "entry_id": original[0]["id"]})
    d = overview(c)["device"]
    c.post(
        "/api/v1/devices/living-room/automation",
        json={"command_id": str(uuid4()), "expected_revision": d["revision"], "mode": "paused"},
    )
    c.post("/api/v1/simulation", json={"action": "advance", "minutes": 300})
    assert undo(c).status_code == 200
    d = overview(c)["device"]
    assert d["automation"] == "paused"
    assert d["plan"] == original
    c.post(
        "/api/v1/devices/living-room/automation",
        json={"command_id": str(uuid4()), "expected_revision": d["revision"], "mode": "active"},
    )
    s.tick()
    assert overview(c)["device"]["observed"] is None


def test_configuration_import_preview_apply_and_undo(rig):
    c, s, _ = rig
    object.__setattr__(s.settings, "teamarr_token", "must-not-export")
    original = c.get("/api/v1/devices/living-room/configuration").json()
    assert set(original["configuration"]) == {"rules", "team_ranks", "preferences"}
    assert "must-not-export" not in json.dumps(original)
    changed = copy.deepcopy(original)
    changed["configuration"]["preferences"]["minimum_viewing_seconds"] = 900
    changed["configuration"]["team_ranks"] = {"nhl": ["demo:nhl:VAN"]}
    body = import_request(c, changed)
    before = overview(c)["device"]
    preview = c.post("/api/v1/devices/living-room/configuration/import/preview", json=body)
    assert preview.status_code == 200
    assert preview.json()["summary"]["ranked_teams"] == 1
    assert overview(c)["device"] == before
    assert c.post("/api/v1/devices/living-room/configuration/import", json=body).status_code == 200
    assert overview(c)["device"]["plan"] == before["plan"]
    assert overview(c)["device"]["preferences"]["minimum_viewing_seconds"] == 900
    assert undo(c).status_code == 200
    assert (
        c.get("/api/v1/devices/living-room/configuration").json()["configuration"]
        == original["configuration"]
    )


def test_import_warns_about_other_mode_and_keeps_unresolved_teams(rig):
    c, _, _ = rig
    document = c.get("/api/v1/devices/living-room/configuration").json()
    document["source_mode"] = "teamarr"
    document["configuration"]["team_ranks"] = {"nfl": ["espn:nfl:8"]}
    body = import_request(c, document)
    preview = c.post("/api/v1/devices/living-room/configuration/import/preview", json=body)
    assert len(preview.json()["warnings"]) == 2
    assert c.post("/api/v1/devices/living-room/configuration/import", json=body).status_code == 200
    assert overview(c)["device"]["team_ranks"]["nfl"] == ["espn:nfl:8"]


@pytest.mark.parametrize("problem", ["version", "timezone", "duplicate", "extra"])
def test_bad_import_is_rejected_without_writing(rig, problem):
    c, _, _ = rig
    document = c.get("/api/v1/devices/living-room/configuration").json()
    if problem == "version":
        document["schema_version"] = 999
    elif problem == "timezone":
        document["configuration"]["preferences"]["timezone"] = "invalid/timezone"
    elif problem == "duplicate":
        document["configuration"]["rules"].append(document["configuration"]["rules"][0])
    else:
        document["configuration"]["automation"] = "active"
    before = overview(c)["device"]
    body = import_request(c, document)
    assert c.post("/api/v1/devices/living-room/configuration/import", json=body).status_code == 422
    assert overview(c)["device"] == before


def test_import_revision_cannot_overwrite_a_newer_plan(rig):
    c, _, _ = rig
    doc = c.get("/api/v1/devices/living-room/configuration").json()
    body = import_request(c, doc)
    command(c, {"type": "add", "content_id": "demo:jays"})
    assert c.post("/api/v1/devices/living-room/configuration/import", json=body).status_code == 409


def test_v1_upgrade_retains_configuration_catalog_jobs_and_receipts(rig):
    c, s, settings = rig
    object.__setattr__(settings, "simulation_delay", 100)
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    before = overview(c)["device"]
    jobs = c.get("/api/v1/devices/living-room/jobs").json()
    with s.db.transaction() as db:
        # Version 1 has exactly the same original tables, without the two version-2 additions.
        db.execute("DROP TABLE team_directory")
        db.execute("DROP TABLE edit_history")
        db.execute("PRAGMA user_version=1")
        receipts = db.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
        snapshots = list(db.execute("SELECT id,snapshot FROM contents"))
    upgraded = Controller(settings)
    assert upgraded.overview()["device"] == before
    assert c.get("/api/v1/devices/living-room/jobs").json() == jobs
    assert upgraded.overview()["teams"]
    with upgraded.db.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == Database.SCHEMA_VERSION
        assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == receipts
        assert list(db.execute("SELECT id,snapshot FROM contents")) == snapshots


def test_future_database_version_is_not_downgraded(tmp_path):
    path = str(tmp_path / "future.db")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="newer controller"):
        Database(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


def test_legacy_rules_receipt_still_recognizes_a_retry_after_upgrade(rig):
    c, s, _ = rig
    d = overview(c)["device"]
    # Version 0.1 serialized these fields in this order when hashing command receipts.
    original = {
        "command_id": str(uuid4()),
        "expected_revision": d["revision"],
        "rules": [
            {key: r[key] for key in ("id", "name", "enabled", "league", "phase", "team_id", "source", "kind")}
            for r in d["rules"]
        ],
        "team_ranks": d["team_ranks"],
        "preferences": d["preferences"],
    }
    receipt = {"command_id": original["command_id"], "revision": d["revision"] + 1, "accepted": True}
    with s.db.transaction() as db:
        db.execute(
            "INSERT INTO commands VALUES (?,?,?,?)",
            (
                d["id"],
                original["command_id"],
                hashlib.sha256(encode(original).encode()).hexdigest(),
                encode(receipt),
            ),
        )
        d["revision"] += 1
        s.db.save_device(db, d)
    response = c.put("/api/v1/devices/living-room/rules", json=original)
    assert response.status_code == 200
    assert response.json() == receipt
    assert overview(c)["device"]["revision"] == d["revision"]


async def test_team_cache_uses_provider_id_and_preserves_richer_feed_data(tmp_path):
    service = Controller(
        Settings(database=str(tmp_path / "teams.db"), mode="teamarr", teamarr_url="http://teamarr")
    )
    entry = fixtures()[0][1]
    team = {"id": "8", "provider": "espn", "full_name": "Detroit Lions", "name": "Lions", "city": "Detroit"}
    entry["event"]["away_team_details"] = team
    with service.db.transaction() as db:
        service.replace_catalog(db, [entry], "teamarr")

    def handler(request):
        assert request.url.path.endswith("/teams")
        if "/nfl/" in request.url.path:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 90210,
                        "provider": "espn",
                        "provider_team_id": "8",
                        "league": "nfl",
                        "team_name": "Detroit Lions",
                        "team_abbrev": "DET",
                        "team_short_name": "Detroit",
                    }
                ],
            )
        return httpx.Response(200, json=[])

    service.client = TeamarrClient("http://teamarr", transport=httpx.MockTransport(handler))
    await service.refresh_team_directory()
    teams = service.overview()["teams"]
    lion = next(t for t in teams if t["key"] == "espn:nfl:8")
    assert lion["id"] == "8"
    assert lion["city"] == "Detroit"
    assert lion["name"] == "Lions"
    assert lion["abbreviation"] == "DET"
    with service.db.transaction() as db:
        service.replace_catalog(db, [], "teamarr")
    service.client = TeamarrClient(
        "http://teamarr", transport=httpx.MockTransport(lambda _: httpx.Response(503))
    )
    await service.refresh_team_directory()
    assert next(t for t in service.overview()["teams"] if t["key"] == lion["key"]) == lion
    assert service.overview()["team_directory_health"]["state"] == "degraded"
    assert service.overview()["health"]["state"] == "ok"


def test_demo_team_not_in_event_window_is_available(rig):
    c, _, _ = rig
    data = overview(c)
    assert not any(t["key"] == "demo:nhl:VAN" for e in data["events"] for t in e["teams"])
    assert any(t["key"] == "demo:nhl:VAN" for t in c.get("/api/v1/teams?league=nhl").json()["items"])
    assert c.get("/api/v1/teams").json()["health"]["state"] == "demo"


async def test_malformed_directory_row_rejects_the_whole_league_snapshot():
    valid = {"league": "nfl", "provider": "espn", "provider_team_id": "8", "team_name": "Detroit Lions"}
    malformed = {**valid, "provider_team_id": "9", "team_name": {"unexpected": "object"}}
    client = TeamarrClient(
        "http://teamarr",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[valid, malformed])),
    )
    with pytest.raises(ValueError, match="provider identity"):
        await client.fetch_teams("nfl")
