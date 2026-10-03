from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from controller.config import Settings
from controller.executor.config import ExecutorConfig

TOKEN = "test-service-credential-32-characters-long"
HEADERS = {"Authorization": "Bearer " + TOKEN}


def settings(tmp_path, **overrides):
    config = ExecutorConfig(
        mode="prime-player",
        prime_socket="/test/player.sock",
        api_token=TOKEN,
        base_url="http://model.invalid/v1",
        monitor_interval=30,
        cancel_timeout=3,
    )
    return Settings(
        database=str(tmp_path / "executor.sqlite3"),
        mode="teamarr",
        teamarr_url="http://teamarr.invalid",
        screen_adb_serial="fixture",
        simulation_delay=0,
        executor=replace(config, **overrides),
        prime_player_socket=overrides.get("prime_socket", config.prime_socket),
    )


def payload(intent=2):
    return {
        "schema_version": 1,
        "request_id": uuid4().hex,
        "device_id": "living-room",
        "intent_version": intent,
        "content_id": "fixture-game",
        "mode": "live",
        "purpose": "selection",
        "previous_request_id": None,
        "content_snapshot_schema_version": 1,
        "deadline_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
        "content_snapshot": {
            "id": "fixture-game",
            "kind": "event",
            "title": "Jets vs. Lions",
            "competition": "nfl",
            "status": "live",
            "start_time": datetime.now(UTC).isoformat(),
            "event": {
                "league": "nfl",
                "away_team_details": {"name": "Jets", "full_name": "New York Jets", "abbreviation": "NYJ"},
                "home_team_details": {"name": "Lions", "full_name": "Detroit Lions", "abbreviation": "DET"},
            },
            "viewing_options": [
                {"id": "prime-option", "app": "prime_video", "presentation": "live", "decision": "eligible"}
            ],
        },
        "allowed_viewing_options": [
            {"id": "prime-option", "app": "prime_video", "presentation": "live", "decision": "eligible"}
        ],
    }
