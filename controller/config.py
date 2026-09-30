import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database: str = "data/controller.sqlite3"
    mode: str = "demo"
    teamarr_url: str = ""
    teamarr_token: str = ""
    feed_interval: int = 60
    status_ttl: int = 120
    worker_interval: float = 1.0
    simulation_delay: float = 1.0
    team_directory_interval: int = 3600
    frontend: Path = Path(__file__).resolve().parents[1] / "frontend" / "dist"

    @classmethod
    def from_env(cls):
        mode = os.getenv("CONTROLLER_MODE", "demo")
        if mode not in {"demo", "teamarr"}:
            raise ValueError("CONTROLLER_MODE must be demo or teamarr")
        url = os.getenv("TEAMARR_URL", "").rstrip("/")
        if mode == "teamarr" and not url:
            raise ValueError("TEAMARR_URL is required in teamarr mode")
        return cls(
            database=os.getenv("CONTROLLER_DATABASE", "data/controller.sqlite3"),
            mode=mode,
            teamarr_url=url,
            teamarr_token=os.getenv("TEAMARR_TOKEN", ""),
            feed_interval=max(10, int(os.getenv("FEED_INTERVAL_SECONDS", "60"))),
        )
