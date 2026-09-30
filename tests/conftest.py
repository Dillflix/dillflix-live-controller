import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller.api import create_app
from controller.config import Settings


@pytest.fixture
def rig(tmp_path):
    settings = Settings(database=str(tmp_path / "controller.sqlite"), simulation_delay=0)
    app = create_app(settings, start_workers=False)
    with TestClient(app) as client:
        yield client, app.state.controller, settings


@pytest.fixture
def fake_adb(tmp_path):
    # A real subprocess/TCP double exercises the complete ADB launch/forward lifecycle.
    program = tmp_path / "adb"
    program.write_text(
        f"#!{sys.executable}\n" + Path(__file__).with_name("fixtures").joinpath("fake_adb.py").read_text()
    )
    program.chmod(0o755)
    server = tmp_path / "server.jar"
    server.write_bytes(b"test-server")
    return Settings(
        screen_adb_serial="tv.example:5555", screen_adb_path=str(program), screen_server_path=server
    )
