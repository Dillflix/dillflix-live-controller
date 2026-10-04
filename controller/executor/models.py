"""Validated playback API requests and durable reports."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..planner import allowed_options, parse_time


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PlaybackRequest(Strict):
    schema_version: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=200)
    device_id: Literal["living-room"]
    intent_version: int = Field(strict=True, ge=0, le=9007199254740991)
    content_id: str = Field(min_length=1, max_length=1024)
    mode: Literal["live"]
    purpose: Literal["selection", "route_handoff", "recovery"] = "selection"
    previous_request_id: str | None = Field(default=None, min_length=1, max_length=200)
    content_snapshot_schema_version: Literal[1] = 1
    content_snapshot: dict
    allowed_viewing_options: list[dict] = Field(min_length=1, max_length=100)
    deadline_at: datetime

    @model_validator(mode="after")
    def consistent(self):
        if self.content_snapshot.get("id") != self.content_id:
            raise ValueError("content_id must equal the original snapshot id")
        if self.deadline_at.tzinfo is None:
            raise ValueError("deadline_at needs a UTC offset")
        self.deadline_at = self.deadline_at.astimezone(UTC)
        ids = set()
        originals = self.content_snapshot.get("viewing_options")
        if not isinstance(originals, list) or any(not isinstance(option, dict) for option in originals):
            raise ValueError("The complete snapshot must contain its original viewing_options")
        if self.content_snapshot.get("kind") not in {"event", "broadcast", "session"}:
            raise ValueError("Snapshot kind must be event, broadcast, or session")
        if (
            not isinstance(self.content_snapshot.get("title"), str)
            or not self.content_snapshot["title"].strip()
        ):
            raise ValueError("Snapshot title is required")
        start = self.content_snapshot.get("start_time")
        if not isinstance(start, str) or not parse_time(start):
            raise ValueError("Snapshot start_time must include a UTC offset")
        permitted = allowed_options(self.content_snapshot)
        for option in self.allowed_viewing_options:
            if not isinstance(option.get("id"), str) or not option["id"] or option["id"] in ids:
                raise ValueError("Every allowed option needs a unique original id")
            if option.get("decision") == "excluded" or option.get("presentation") in {"replay", "highlights"}:
                raise ValueError("Excluded or non-live viewing options cannot be requested")
            if option not in permitted:
                raise ValueError(
                    "Allowed options must be unchanged permitted options from the original snapshot"
                )
            ids.add(option["id"])
        return self


class CancelRequest(Strict):
    device_id: Literal["living-room"]
    token: str | None = Field(default=None, min_length=1, max_length=200)
    through_intent_version: int | None = Field(default=None, strict=True, ge=0, le=9007199254740991)

    @model_validator(mode="after")
    def one_scope(self):
        if (self.token is None) == (self.through_intent_version is None):
            raise ValueError("Supply exactly one of token or through_intent_version")
        return self


class ErrorDetail(Strict):
    code: str
    message: str
    retryable: bool


class Problem(Strict):
    type: str
    title: str
    status: int
    detail: str
    code: str
    retryable: bool


class Evidence(Strict):
    method: Literal["device_observation"]
    evidence_id: str
    summary: str
    confidence: float | None
    captured_at: datetime
    decision: Literal["confirmed"] | None = None


class RouteAttempt(Strict):
    attempt: int
    viewing_option_id: str
    phase: str
    started_at: datetime
    finished_at: datetime | None
    error: ErrorDetail | None


class Operation(Strict):
    state: Literal[
        "accepted",
        "navigating",
        "playing_verified",
        "completed",
        "failed",
        "cancelled",
        "superseded",
        "timed_out",
        "waiting_for_feed",
        "access_unknown",
        "feeds_locked",
        "no_matching_feed",
    ]
    phase: str
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    deadline_at: datetime
    error: ErrorDetail | None
    attempts: list[RouteAttempt]


class PlaybackObservation(Strict):
    device_id: Literal["living-room"]
    request_id: str
    intent_version: int
    content_id: str
    viewing_option_id: str
    presentation: Literal["live"]
    verified: bool
    simulated: Literal[False]
    health: Literal["healthy"]
    observed_at: datetime
    valid_until: datetime
    evidence: Evidence


class ObservationStatus(Strict):
    state: Literal["fresh", "stale", "unavailable"]
    checked_at: datetime
    error: ErrorDetail | None


LifecycleState = Literal[
    "scheduled", "live", "ended", "cancelled", "delayed", "suspended", "postponed", "unknown"
]


class LifecycleObservation(Strict):
    content_id: str
    state: LifecycleState
    source: Literal["teamarr_feed", "prime_video_visual", "prime_player"]
    simulated: Literal[False]
    timestamp_basis: Literal["feed_received", "device_observed"]
    observed_at: datetime | None
    received_at: datetime
    valid_until: datetime
    evidence: Evidence | None = None


class ContentStatus(Strict):
    content_id: str
    lookup_state: Literal["ok", "unavailable"]
    effective_state: LifecycleState
    stale: bool
    observation: LifecycleObservation | None
    checked_at: datetime
    error: ErrorDetail | None


class Cancellation(Strict):
    state: Literal["none", "requested", "acknowledged"]
    input_quiescent: bool
    active_playback: Literal["stopped", "already_inactive", "unknown", "not_current"] = "unknown"


class PlaybackReport(Strict):
    schema_version: Literal[1]
    token: str
    request_id: str
    device_id: Literal["living-room"]
    content_id: str
    intent_version: int
    revision: int
    operation: Operation
    observation: PlaybackObservation | None
    observation_status: ObservationStatus
    content_status: ContentStatus
    cancellation: Cancellation
    retained_until: datetime | None
    prime_player: dict | None = None


class CancelResult(Strict):
    device_id: Literal["living-room"]
    token: str | None
    through_intent_version: int | None
    input_quiescent: Literal[True]
    active_playback: Literal["stopped", "already_inactive", "unknown", "not_current"]
    acknowledged_at: datetime


class ExecutorError(Exception):
    def __init__(self, code, message, *, status=503, retryable=True):
        super().__init__(message)
        self.code, self.message, self.status, self.retryable = code, message, status, retryable

    def detail(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable}
