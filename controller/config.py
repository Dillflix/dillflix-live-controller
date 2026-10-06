import os
from dataclasses import dataclass, field
from pathlib import Path

from .executor.config import ExecutorConfig


@dataclass(frozen=True)
class Settings:
    proxy_secret: str = ""
    public_auth_mode: str = "proxy"
    admin_auth_mode: str = "proxy"
    database: str = "data/controller.sqlite3"
    mode: str = "demo"
    teamarr_url: str = ""
    teamarr_token: str = ""
    feed_interval: int = 60
    status_ttl: int = 120
    status_interval: float = 15
    status_lookup_timeout: float = 5
    worker_interval: float = 1.0
    simulation_delay: float = 1.0
    navigation_timeout: float = 120.0
    observation_ttl: int = 15
    playback_recovery_grace: float = 60
    recovery_stable_seconds: float = 30
    team_directory_interval: int = 3600
    maintenance_interval: int = 3600
    job_history_limit: int = 1000
    command_history_limit: int = 10000
    catalog_retention_days: int = 30
    prime_player_socket: str = ""
    screen_adb_serial: str = ""
    screen_adb_path: str = "adb"
    screen_server_path: Path = Path("data/scrcpy-server-v3.3.4")
    screen_max_size: int = 1280
    screen_max_fps: int = 30
    screen_bit_rate: int = 2000000
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)
    frontend: Path = Path(__file__).resolve().parents[1] / "frontend" / "dist"

    def __post_init__(self):
        if self.public_auth_mode not in {"proxy", "guest"}:
            raise ValueError("CONTROLLER_PUBLIC_AUTH_MODE must be proxy or guest")
        if self.admin_auth_mode not in {"proxy", "trusted-lan"}:
            raise ValueError("CONTROLLER_ADMIN_AUTH_MODE must be proxy or trusted-lan")

    @property
    def playback_evidence_ttl(self):
        """Expire prior verified Prime evidence five minutes after observation.

        observation_ttl remains the maximum age of a newly accepted sample.
        Failed or contradictory checks revoke evidence before this deadline.
        """
        if self.executor.mode == "prime-player":
            return 300
        return self.observation_ttl

    @classmethod
    def from_env(cls):
        mode = os.getenv("CONTROLLER_MODE", "demo")
        if mode not in {"demo", "teamarr"}:
            raise ValueError("CONTROLLER_MODE must be demo or teamarr")
        url = os.getenv("TEAMARR_URL", "").rstrip("/")
        if mode == "teamarr" and not url:
            raise ValueError("TEAMARR_URL is required in teamarr mode")
        return cls(
            proxy_secret=os.getenv("CONTROLLER_PROXY_SECRET", ""),
            public_auth_mode=os.getenv("CONTROLLER_PUBLIC_AUTH_MODE", "proxy"),
            admin_auth_mode=os.getenv("CONTROLLER_ADMIN_AUTH_MODE", "proxy"),
            prime_player_socket=os.getenv("PRIME_PLAYER_SOCKET", ""),
            database=os.getenv("CONTROLLER_DATABASE", "data/controller.sqlite3"),
            mode=mode,
            teamarr_url=url,
            teamarr_token=os.getenv("TEAMARR_TOKEN", ""),
            feed_interval=max(10, int(os.getenv("FEED_INTERVAL_SECONDS", "60"))),
            status_interval=max(5, float(os.getenv("STATUS_INTERVAL_SECONDS", "15"))),
            navigation_timeout=max(5, float(os.getenv("NAVIGATION_TIMEOUT_SECONDS", "120"))),
            playback_recovery_grace=max(5, float(os.getenv("PLAYBACK_RECOVERY_GRACE_SECONDS", "60"))),
            job_history_limit=max(50, int(os.getenv("JOB_HISTORY_LIMIT", "1000"))),
            command_history_limit=max(100, int(os.getenv("COMMAND_HISTORY_LIMIT", "10000"))),
            catalog_retention_days=max(1, int(os.getenv("CATALOG_RETENTION_DAYS", "30"))),
            screen_adb_serial=os.getenv("SCREEN_ADB_SERIAL", "").strip(),
            screen_adb_path=os.getenv("SCREEN_ADB_PATH", "adb"),
            screen_server_path=Path(os.getenv("SCREEN_SERVER_PATH", "data/scrcpy-server-v3.3.4")),
            screen_max_size=min(1920, max(320, int(os.getenv("SCREEN_MAX_SIZE", "1280")))),
            screen_max_fps=min(60, max(5, int(os.getenv("SCREEN_MAX_FPS", "30")))),
            screen_bit_rate=min(12000000, max(250000, int(os.getenv("SCREEN_BIT_RATE", "2000000")))),
            executor=ExecutorConfig.from_env(),
        )
