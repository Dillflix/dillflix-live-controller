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
