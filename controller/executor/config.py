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
    actor_model: str = ""
    observer_model: str = ""
    actor_protocol: str = "json"
    structured_output: str = "json_schema"
    model_timeout: float = 45
    adb_timeout: float = 10
    cancel_timeout: float = 20
    monitor_interval: float = 5
    completion_interval: float = 15
    settle_seconds: float = 1
    max_actions: int = 30
    frame_max_age: float = 60
    retention_days: int = 7
    # Explicit self-hosted sampler extensions are opt-in, never assumed by the client.
    model_options: dict = field(default_factory=lambda: {"temperature": 0, "top_p": 1})

    def validate(self):
        if self.mode not in {"simulator", "prime-video"}:
            raise ValueError("PLAYBACK_ADAPTER must be simulator or prime-video")
        if self.mode == "simulator":
            return
        url = urlsplit(self.base_url)
        if (
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
        if not self.actor_model or not self.observer_model:
            raise ValueError("EXECUTOR_ACTOR_MODEL and EXECUTOR_OBSERVER_MODEL are required")
        if self.api_token and len(self.api_token) < 32:
            raise ValueError("EXECUTOR_API_TOKEN must contain at least 32 characters")
        if self.actor_protocol not in {"json", "tvtheseus"}:
            raise ValueError("EXECUTOR_ACTOR_PROTOCOL must be json or tvtheseus")
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
            "adb_timeout",
            "cancel_timeout",
            "monitor_interval",
            "completion_interval",
            "frame_max_age",
            "max_actions",
            "retention_days",
        ):
            if not 0 < getattr(self, key) <= 86400:
                raise ValueError(f"Invalid executor setting: {key}")
        if not 0 <= self.settle_seconds <= 10:
            raise ValueError("Executor settle time must be between zero and ten seconds")

    @classmethod
    def from_env(cls):
        value = cls(
            mode=os.getenv("PLAYBACK_ADAPTER", "simulator"),
            api_token=os.getenv("EXECUTOR_API_TOKEN", ""),
            base_url=os.getenv("EXECUTOR_LLM_BASE_URL", "").rstrip("/"),
            api_key=os.getenv("EXECUTOR_LLM_API_KEY", ""),
            actor_model=os.getenv("EXECUTOR_ACTOR_MODEL", ""),
            observer_model=os.getenv("EXECUTOR_OBSERVER_MODEL", ""),
            actor_protocol=os.getenv("EXECUTOR_ACTOR_PROTOCOL", "json"),
            structured_output=os.getenv("EXECUTOR_STRUCTURED_OUTPUT", "json_schema"),
            model_timeout=float(os.getenv("EXECUTOR_MODEL_TIMEOUT_SECONDS", "45")),
            adb_timeout=float(os.getenv("EXECUTOR_ADB_TIMEOUT_SECONDS", "10")),
            cancel_timeout=float(os.getenv("EXECUTOR_CANCEL_TIMEOUT_SECONDS", "20")),
            monitor_interval=float(os.getenv("EXECUTOR_MONITOR_INTERVAL_SECONDS", "5")),
            completion_interval=float(os.getenv("EXECUTOR_COMPLETION_INTERVAL_SECONDS", "15")),
            settle_seconds=float(os.getenv("EXECUTOR_SETTLE_SECONDS", "1")),
            max_actions=int(os.getenv("EXECUTOR_MAX_ACTIONS", "30")),
            model_options=json.loads(os.getenv("EXECUTOR_LLM_OPTIONS_JSON") or '{"temperature":0,"top_p":1}'),
        )
        value.validate()
        return value
