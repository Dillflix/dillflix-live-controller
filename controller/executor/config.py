"""Explicit deployment settings; none of these values come from playback requests."""

import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ExecutorConfig:
    mode: str = "simulator"
    api_token: str = field(default="", repr=False)
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    structured_output: str = "json_schema"
    model_timeout: float = 45
    cancel_timeout: float = 80
    monitor_interval: float = 5
    retention_days: int = 7
    prime_socket: str = ""
    prime_match_model: str = ""
    prime_search_timeout: float = 45
    prime_status_timeout: float = 60
    # Explicit self-hosted sampler extensions are opt-in, never assumed by the client.
    model_options: dict = field(default_factory=lambda: {"temperature": 0, "top_p": 1})

    def validate(self):
        if self.mode not in {"simulator", "prime-player"}:
            raise ValueError(
                "PLAYBACK_ADAPTER must be simulator or prime-player; the prime-video executor has been removed"
            )
        if self.mode == "simulator":
            return
        if self.api_token and len(self.api_token) < 32:
            raise ValueError("EXECUTOR_API_TOKEN must contain at least 32 characters")
        if self.mode == "prime-player":
            if not self.prime_socket.startswith("/"):
                raise ValueError("PRIME_PLAYER_SOCKET must be an absolute Unix socket path")
            if not 5 <= self.prime_search_timeout <= 120 or not 5 <= self.prime_status_timeout <= 60:
                raise ValueError("Prime Player search/status timeouts must be within service limits")
        needs_model = bool(self.prime_match_model)
        url = urlsplit(self.base_url)
        if needs_model and (
            url.scheme not in {"http", "https"}
            or not url.netloc
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "EXECUTOR_LLM_BASE_URL must be an HTTP(S) server URL without embedded credentials"
            )
        if self.structured_output not in {"json_schema", "json_object"}:
            raise ValueError("EXECUTOR_STRUCTURED_OUTPUT must be json_schema or json_object")
        allowed = {
            "temperature",
            "top_p",
            "top_k",
            "min_p",
            "presence_penalty",
            "frequency_penalty",
            "repetition_penalty",
            "seed",
            "chat_template_kwargs",
        }
        if not isinstance(self.model_options, dict) or set(self.model_options) - allowed:
            raise ValueError("EXECUTOR_LLM_OPTIONS_JSON contains unsupported model options")
        json.dumps(self.model_options, allow_nan=False)
        for key in (
            "model_timeout",
            "cancel_timeout",
            "monitor_interval",
            "retention_days",
        ):
            if not 0 < getattr(self, key) <= 86400:
                raise ValueError(f"Invalid executor setting: {key}")

    @classmethod
    def from_env(cls):
        value = cls(
            mode=os.getenv("PLAYBACK_ADAPTER", "simulator"),
            api_token=os.getenv("EXECUTOR_API_TOKEN", ""),
            base_url=os.getenv("EXECUTOR_LLM_BASE_URL", "").rstrip("/"),
            api_key=os.getenv("EXECUTOR_LLM_API_KEY", ""),
            structured_output=os.getenv("EXECUTOR_STRUCTURED_OUTPUT", "json_schema"),
            model_timeout=float(os.getenv("EXECUTOR_MODEL_TIMEOUT_SECONDS", "45")),
            cancel_timeout=float(os.getenv("EXECUTOR_CANCEL_TIMEOUT_SECONDS", "80")),
            monitor_interval=float(os.getenv("EXECUTOR_MONITOR_INTERVAL_SECONDS", "5")),
            prime_socket=os.getenv("PRIME_PLAYER_SOCKET", ""),
            prime_match_model=os.getenv("PRIME_PLAYER_MATCH_MODEL", ""),
            prime_search_timeout=float(os.getenv("PRIME_PLAYER_SEARCH_TIMEOUT_SECONDS", "45")),
            prime_status_timeout=float(os.getenv("PRIME_PLAYER_STATUS_TIMEOUT_SECONDS", "60")),
            model_options=json.loads(os.getenv("EXECUTOR_LLM_OPTIONS_JSON") or '{"temperature":0,"top_p":1}'),
        )
        value.validate()
        return value
