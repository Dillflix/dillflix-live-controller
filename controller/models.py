from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Action(StrictModel):
    type: Literal["add", "play_now", "remove", "reorder"]
    content_id: str | None = None
    entry_id: str | None = None
    ordered_entry_ids: list[str] | None = None
    priority: Literal["first", "last"] = "last"


class Command(StrictModel):
    command_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    action: Action


class Rule(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    league: str = "all"
    phase: str = "any"
    team_id: str | None = None
    source: str | None = None
    kind: Literal["event", "session", "broadcast"] | None = None


class Preferences(StrictModel):
    timezone: str = "America/Vancouver"
    minimum_viewing_seconds: int = Field(default=300, ge=0, le=3600)
    switch_cooldown_seconds: int = Field(default=30, ge=0, le=600)
    same_tier_switching: bool = False

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use an IANA timezone such as America/Vancouver") from exc
        return value


class RulesUpdate(StrictModel):
    command_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    rules: list[Rule] = Field(max_length=100)
    team_ranks: dict[str, list[str]] = Field(default_factory=dict)
    preferences: Preferences


class AutomationUpdate(StrictModel):
    command_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    mode: Literal["active", "paused"]


class SimulationCommand(StrictModel):
    action: Literal["advance", "scenario"]
    minutes: int = Field(default=15, ge=1, le=1440)
    scenario: Literal["normal", "overlap", "overtime", "delayed", "failure", "stale", "empty"] = "normal"
