from dataclasses import dataclass, field, asdict
from typing import Any, Iterator


@dataclass(frozen=True)
class SearchTile:
    handle: str
    collection: str
    position: int
    title: str | None
    date_label: str | None
    availability: str | None
    content_id: str | None
    candidate_ids: tuple[str, ...]
    identity_status: str
    labels: tuple[str, ...]
    artwork: tuple[str, ...]
    bounds: dict[str, float]
    intersects_viewport: bool
    occurrences: tuple[dict, ...] = ()
    starts_at: str | None = None
    ends_at: str | None = None
    action: str | None = None
    entitlement: dict[str, Any] | None = None
    resolution_status: str = "not_requested"
    resolution_reason: str | None = None
    resolved_at: str | None = None
    is_locked: bool | None = None

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        for key in ("candidate_ids", "labels", "artwork"):
            data[key] = tuple(data[key])
        data["occurrences"] = tuple(data.get("occurrences", ()))
        return cls(**data)


@dataclass(frozen=True)
class BrowseResults:
    query: str
    session_id: str
    search_id: str
    generation: int
    observed_at: str
    tiles: tuple[SearchTile, ...]
    complete: bool = False
    coverage: str = "loaded_renderer"
    warnings: tuple[str, ...] = ()
    source: str = "search"
    page_id: str | None = None
    pages: tuple[str, ...] = ()
    page_results: tuple[dict, ...] = ()
    requested_limit: int | None = None
    row_limit: int | None = None
    stop_reason: str | None = None
    network_context_id: str | None = None

    def __iter__(self) -> Iterator[SearchTile]:
        return iter(self.tiles)

    def matching(self, *, title=None, availability=None, collection=None, action=None):
        """Filter returned metadata; never performs navigation or metadata discovery."""
        return tuple(
            t
            for t in self.tiles
            if (title is None or t.title == title)
            and (availability is None or t.availability == availability)
            and (collection is None or t.collection == collection)
            and (action is None or t.action == action)
        )

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(
            **{
                **data,
                "tiles": tuple(SearchTile.from_dict(t) for t in data["tiles"]),
                "warnings": tuple(data.get("warnings", [])),
                "pages": tuple(data.get("pages", [])),
                "page_results": tuple(data.get("page_results", [])),
            }
        )


@dataclass(frozen=True)
class ContentResolution:
    request_id: str
    session_id: str
    requested_id: str
    resolved_id: str | None
    action: str | None
    entitlement: dict[str, Any] | None
    playback_class: str | None
    observed_at: str
    status: str
    reason: str | None = None
    network_context_id: str | None = None

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class PlaybackOutcome:
    attempt_id: str
    state: str
    requested_id: str
    resolved_id: str | None = None
    reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    network_context_id: str | None = None


@dataclass(frozen=True)
class PlaybackStatus:
    attempt_id: str
    session_id: str
    requested_id: str
    resolved_id: str | None
    state: str
    is_playing: bool | None
    matches_attempt: bool | None
    current_content_id: str | None
    position: float | None
    position_unit: str
    observed_at: str | None
    observation_age_seconds: float | None
    state_event_age_seconds: float | None
    error: str | None
    evidence: dict[str, Any] = field(default_factory=dict)
    network_context_id: str | None = None


@dataclass(frozen=True)
class PageInfo:
    id: str
    title: str | None
    kind: str
    position: int
    title_hint: str | None = None
    available: bool = True


@dataclass(frozen=True)
class PagesResults:
    session_id: str
    generation: int | None
    observed_at: str
    pages: tuple[PageInfo, ...]
    network_context_id: str | None = None

    def __iter__(self):
        return iter(self.pages)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**{**data, "pages": tuple(PageInfo(**p) for p in data["pages"])})
