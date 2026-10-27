import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from .executor.config import ExecutorConfig
from .live_stream_config import validate_configuration, validate_device_map


@dataclass(frozen=True)
class DeviceSettings:
    id: str
    name: str
    prime_player_socket: str = ""
    adb_serial: str = ""

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", self.id):
            raise ValueError("Device IDs must contain lowercase letters, numbers and hyphens")
        if not self.name.strip() or len(self.name) > 80:
            raise ValueError("Device names must contain 1–80 characters")
        # Preserve the original device ID and bindings while retiring the old
        # room label, including registries written by the first add-stream tool.
        if self.id == "living-room" and self.name == "Living room":
            object.__setattr__(self, "name", "Stream 1")


@dataclass(frozen=True)
class Settings:
    device_id: str = "living-room"
    devices: tuple[DeviceSettings, ...] = ()
    proxy_secret: str = ""
    public_auth_mode: str = "proxy"
    public_origin: str = ""
    admin_auth_mode: str = "proxy"
    live_browser_api_url: str = ""
    live_browser_api_key: str = field(default="", repr=False)
    live_browser_device_map: dict[str, str] = field(default_factory=dict)
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
    catalog_retention_days: int = 7
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
        validate_configuration(self.live_browser_api_url, self.live_browser_api_key)
        validate_device_map(self.live_browser_device_map)
        if self.public_auth_mode not in {"proxy", "guest"}:
            raise ValueError("CONTROLLER_PUBLIC_AUTH_MODE must be proxy or guest")
        if self.public_origin:
            try:
                origin = urlsplit(self.public_origin)
                valid = (
                    origin.scheme in {"http", "https"} and origin.hostname
                    and origin.port != 0 and not origin.username and not origin.password
                    and origin.path in {"", "/"} and not origin.query and not origin.fragment
                    and not any(c.isspace() or ord(c) < 32 for c in self.public_origin)
                    and not any(c in self.public_origin for c in "@*\\?#")
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("CONTROLLER_PUBLIC_ORIGIN must be an HTTP(S) origin without credentials, path, query or fragment")
            object.__setattr__(self, "public_origin", self.public_origin.rstrip("/"))
        if self.admin_auth_mode not in {"proxy", "trusted-lan"}:
            raise ValueError("CONTROLLER_ADMIN_AUTH_MODE must be proxy or trusted-lan")
        if len(self.devices) > 8:
            raise ValueError("At most eight streams are supported")
        for field_name in ("id", "adb_serial", "prime_player_socket"):
            values = [getattr(d, field_name) for d in self.devices if getattr(d, field_name)]
            if len(values) != len(set(values)):
                raise ValueError(f"Each device must have a unique {field_name}")
        if self.devices and self.executor.mode == "prime-player":
            for device in self.devices:
                if not device.adb_serial or not device.prime_player_socket.startswith("/"):
                    raise ValueError("Each Prime stream requires an ADB serial and absolute player socket")

    @property
    def configured_devices(self):
        return self.devices or (DeviceSettings(
            self.device_id, "Stream 1", self.prime_player_socket or self.executor.prime_socket,
            self.screen_adb_serial,
        ),)

    def for_device(self, device):
        return replace(self, device_id=device.id,
                       prime_player_socket=device.prime_player_socket,
                       screen_adb_serial=device.adb_serial,
                       executor=replace(self.executor, prime_socket=device.prime_player_socket))

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
        raw_devices = os.getenv("CONTROLLER_DEVICES_JSON", "").strip()
        devices_path = os.getenv("CONTROLLER_DEVICES_FILE", "").strip()
        if raw_devices and devices_path:
            raise ValueError("Set only one of CONTROLLER_DEVICES_JSON or CONTROLLER_DEVICES_FILE")
        if devices_path:
            raw_devices = Path(devices_path).read_text(encoding="utf-8")
        devices = ()
        if raw_devices:
            records = json.loads(raw_devices)
            if not isinstance(records, list) or not records:
                raise ValueError("Device configuration must be a nonempty JSON array")
            devices = tuple(DeviceSettings(**record) for record in records)
        return cls(
            devices=devices,
            proxy_secret=os.getenv("CONTROLLER_PROXY_SECRET", ""),
            public_auth_mode=os.getenv("CONTROLLER_PUBLIC_AUTH_MODE", "proxy"),
            public_origin=os.getenv("CONTROLLER_PUBLIC_ORIGIN", ""),
            admin_auth_mode=os.getenv("CONTROLLER_ADMIN_AUTH_MODE", "proxy"),
            live_browser_api_url=os.getenv("LIVE_BROWSER_API_URL", "").strip().rstrip("/"),
            live_browser_api_key=os.getenv("LIVE_BROWSER_API_KEY", ""),
            live_browser_device_map=json.loads(os.getenv("LIVE_BROWSER_DEVICE_MAP", "").strip() or "{}"),
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
            catalog_retention_days=max(1, int(os.getenv("CATALOG_RETENTION_DAYS", "7"))),
            screen_adb_serial=os.getenv("SCREEN_ADB_SERIAL", "").strip(),
            screen_adb_path=os.getenv("SCREEN_ADB_PATH", "adb"),
            screen_server_path=Path(os.getenv("SCREEN_SERVER_PATH", "data/scrcpy-server-v3.3.4")),
            screen_max_size=min(1920, max(320, int(os.getenv("SCREEN_MAX_SIZE", "1280")))),
            screen_max_fps=min(60, max(5, int(os.getenv("SCREEN_MAX_FPS", "30")))),
            screen_bit_rate=min(12000000, max(250000, int(os.getenv("SCREEN_BIT_RATE", "2000000")))),
            executor=ExecutorConfig.from_env(),
        )
