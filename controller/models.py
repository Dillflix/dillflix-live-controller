from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from .leagues import DEFAULT_LEAGUES


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


class PublicCommand(StrictModel):
    command_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    action: Literal["add", "play_now"]
    content_id: str = Field(min_length=1, max_length=1000)


class PublicAccessUpdate(StrictModel):
    command_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    play_now: bool
    add_to_plan: bool


class EntitlementCommand(StrictModel):
    command_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    content_id: str = Field(min_length=1, max_length=1000)


class Rule(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    league: str = "all"
    phase: str = "any"
    team_id: str | None = None
    source: str | None = None
    kind: Literal["event", "session", "broadcast"] | None = None


class VPNLocationPolicy(StrictModel):
    target_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    team_cities: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("target_id")
    @classmethod
    def vpn_only(cls, value):
        if value == "direct":
            raise ValueError("Direct is always permitted; configure VPN locations only")
        return value

    @field_validator("team_cities")
    @classmethod
    def cities(cls, values):
        from .network_policy import city_key
        if any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in values):
            raise ValueError("Use nonempty team cities of at most 100 characters")
        if len({city_key(v) for v in values}) != len(values):
            raise ValueError("Team cities must be unique")
        return [v.strip() for v in values]


class VPNPolicy(StrictModel):
    enabled: bool = False
    locations: list[VPNLocationPolicy] = Field(default_factory=list, max_length=4)
    acquisition_timeout: int = Field(default=1800, ge=120, le=1800)
    max_transitions: int = Field(default=4, ge=1, le=4)
    access_ttl: int = Field(default=300, ge=60, le=3600)

    @model_validator(mode="after")
    def unique_locations(self):
        if len({v.target_id for v in self.locations}) != len(self.locations):
            raise ValueError("VPN locations must be unique")
        return self


class Preferences(StrictModel):
    vpn_policy: VPNPolicy | None = None
    timezone: str = "America/Vancouver"
    minimum_viewing_seconds: int = Field(default=300, ge=0, le=3600)
    switch_cooldown_seconds: int = Field(default=30, ge=0, le=600)
    same_tier_switching: bool = False
    discovery_leagues: list[str] = Field(default_factory=lambda: list(DEFAULT_LEAGUES), max_length=20)

    @field_validator("discovery_leagues")
    @classmethod
    def valid_leagues(cls, value):
        if len(value) != len(set(value)) or any(
            not league or len(league) > 100 or not all(c.isalnum() or c in "._-" for c in league)
            for league in value
        ):
            raise ValueError("Use unique Teamarr league codes")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use an IANA timezone such as America/Vancouver") from exc
        return value


class Configuration(StrictModel):
    rules: list[Rule] = Field(max_length=100)
    team_ranks: dict[str, list[str]] = Field(default_factory=dict)
    preferences: Preferences

    @model_validator(mode="after")
    def consistent_identities(self):
        if len({r.id for r in self.rules}) != len(self.rules):
            raise ValueError("Rule IDs must be unique")
        if len(self.team_ranks) > 100 or any(len(ranks) > 1000 for ranks in self.team_ranks.values()):
            raise ValueError("Too many team rankings")
        if any(len(ranks) != len(set(ranks)) for ranks in self.team_ranks.values()):
            raise ValueError("Team rankings must not contain duplicate identities")
        if any(not key or len(key) > 300 for ranks in self.team_ranks.values() for key in ranks):
            raise ValueError("Invalid team identity")
        return self


class RulesUpdate(Configuration):
    command_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)


class ConfigurationDocument(StrictModel):
    format: Literal["dillflix-controller-config"]
    schema_version: Literal[1]
    source_mode: Literal["demo", "teamarr"]
    exported_at: AwareDatetime
    configuration: Configuration


class ConfigurationImport(StrictModel):
    command_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    document: ConfigurationDocument


class UndoCommand(StrictModel):
    command_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    history_id: int = Field(ge=1)


class AutomationUpdate(StrictModel):
    command_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    mode: Literal["active", "paused"]


class CompletionCommand(StrictModel):
    command_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    content_id: str = Field(min_length=1, max_length=1000)


class SimulationCommand(StrictModel):
    action: Literal["advance", "scenario", "disconnect", "reconnect"]
    minutes: int = Field(default=15, ge=1, le=1440)
    scenario: Literal[
        "normal",
        "overlap",
        "overtime",
        "delayed",
        "failure",
        "stale",
        "empty",
        "timeout",
        "replay",
        "status_outage",
        "outside_feed",
        "coverage_switch",
        "device_outage",
        "tennis",
    ] = "normal"


class ManualControlCommand(StrictModel):
    command_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    action: Literal["take", "extend", "release"]
    session_id: str = Field(min_length=16, max_length=100)
    owner_token: str = Field(min_length=32, max_length=128)
    minutes: int = Field(default=15, ge=1, le=1440)
    takeover: bool = False
    release_mode: Literal["active", "paused"] = "active"
