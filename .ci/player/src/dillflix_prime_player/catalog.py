"""Catalogue search: authenticated Find data without navigation or resolution."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
import time
import uuid

from .errors import BusyError, EvidenceLostError, StaleResultError, UnavailableError


@dataclass(frozen=True)
class SearchItem:
    content_id: str | None
    title: str | None
    content_type: str | None
    synopsis: str | None
    entitlement_status: str | None
    entitlement_messaging: dict | None
    event_state: str | None
    starts_at: str | None
    ends_at: str | None
    artwork: dict
    action: dict | None
    actions: tuple
    metadata: dict

    @classmethod
    def from_dict(cls, value):
        return cls(**{**value, "actions": tuple(value.get("actions", ()))})


@dataclass(frozen=True)
class SearchContainer:
    items: tuple[SearchItem, ...]
    has_more: bool
    metadata: dict

    @classmethod
    def from_dict(cls, value):
        return cls(**{**value, "items": tuple(SearchItem.from_dict(i) for i in value["items"])})


@dataclass(frozen=True)
class SearchResults:
    query: str
    session_id: str
    search_id: str
    generation: int | None
    observed_at: str
    containers: tuple[SearchContainer, ...]
    metadata: dict
    has_more: bool
    complete: bool
    coverage: str = "find_initial_response"
    warnings: tuple[str, ...] = ()
    network_context_id: str | None = None

    @property
    def items(self):
        return tuple(i for c in self.containers for i in c.items)

    def __iter__(self):
        return iter(self.items)

    def matching(self, *, title=None, entitlement_status=None, event_state=None):
        return tuple(i for i in self if (title is None or i.title == title)
                     and (entitlement_status is None or i.entitlement_status == entitlement_status)
                     and (event_state is None or i.event_state == event_state))

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**{**value, "containers": tuple(SearchContainer.from_dict(c) for c in value["containers"]),
                      "warnings": tuple(value.get("warnings", ()))})


def _timestamp(value):
    # Preserve the original epoch milliseconds in metadata as well.
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds")
    except (ValueError, OverflowError, OSError):
        return None


def parse(resource, *, query, session_id, search_id, generation, warnings=()):
    if not isinstance(resource, dict) or not isinstance(resource.get("containers"), list):
        raise EvidenceLostError("Find response has no containers array")
    containers = []
    for c in resource["containers"]:
        if not isinstance(c, dict) or not isinstance(c.get("items"), list):
            raise EvidenceLostError("Find container has no items array")
        items = []
        for i in c["items"]:
            if not isinstance(i, dict):
                raise EvidenceLostError("Find item is not an object")
            items.append(SearchItem(
                content_id=i.get("gti"), title=i.get("title"), content_type=i.get("contentType"),
                synopsis=i.get("synopsis"), entitlement_status=i.get("entitlementStatus"),
                entitlement_messaging=i.get("entitlementMessaging"), event_state=i.get("liveliness"),
                starts_at=_timestamp(i.get("startTime")), ends_at=_timestamp(i.get("endTime")),
                artwork={k: v for k, v in i.items() if "image" in k.lower() or "artwork" in k.lower()},
                action=i.get("action"), actions=tuple(i.get("actions") or ()), metadata=i,
            ))
        containers.append(SearchContainer(tuple(items), bool(c.get("paginationLink")),
                                          {k: v for k, v in c.items() if k != "items"}))
    has_more = bool(resource.get("paginationLink")) or any(c.has_more for c in containers)
    return SearchResults(query, session_id, search_id, generation,
                         datetime.now(timezone.utc).isoformat(), tuple(containers),
                         {k: v for k, v in resource.items() if k != "containers"}, has_more,
                         not has_more, warnings=tuple(warnings))


def assemble(rows, request_id):
    pieces = [r["data"] for r in rows if r.get("kind") == "catalog_search_piece"
              and r["data"].get("id") == request_id]
    ends = [r["data"] for r in rows if r.get("kind") == "catalog_search_done"
            and r["data"].get("id") == request_id]
    if not ends:
        return None
    end = ends[-1]
    if end["status"] == "busy":
        raise BusyError("previous catalogue request is still settling")
    if end["status"] != "resolved":
        raise UnavailableError("catalogue search failed: " + end["status"])
    if len(pieces) != end["pieces"] or [p["index"] for p in pieces] != list(range(len(pieces))):
        raise EvidenceLostError("catalogue response pieces were lost or duplicated")
    result = None
    for p in pieces:
        path, body = p["path"], p["body"]
        if not path:
            if result is not None:
                raise EvidenceLostError("duplicate catalogue root")
            result = {**body, "containers": []}
        elif result is None:
            raise EvidenceLostError("missing catalogue root")
        elif len(path) == 2 and path[0] == "containers" and path[1] == len(result["containers"]):
            result["containers"].append({**body, "items": []})
        elif (len(path) == 4 and path[0] == "containers" and path[2] == "items"
              and 0 <= path[1] < len(result["containers"])
              and path[3] == len(result["containers"][path[1]]["items"])):
            result["containers"][path[1]]["items"].append(body)
        else:
            raise EvidenceLostError("invalid catalogue piece ordering")
    if result is None:
        raise EvidenceLostError("missing catalogue response")
    return result, end.get("warnings", ())


def search(controller, query, timeout, ownership):
    if not isinstance(query, str) or not query.strip() or len(query) > 512:
        raise ValueError("query must contain 1..512 characters")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 5 <= timeout <= 120:
        raise ValueError("search timeout must be 5..120 seconds")
    with controller.operation(ownership, readonly=True):
        if controller._active_attempts():
            raise BusyError("finish playback verification before searching")
        controller._require("search")
        deadline = time.monotonic() + timeout
        controller._ready(min(20, timeout))
        generation = controller._generation()
        b = controller.backend
        cursor, request_id = b.cursor, uuid.uuid4().hex
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UnavailableError("catalogue search readiness deadline exceeded")
        b.command("search-data", command_id=request_id, query=query,
                  requestTimeoutMs=max(1, int(remaining * 1000)), timeout=min(30, remaining))
        try:
            resource, warnings = b.wait(lambda: assemble(b.records(cursor), request_id),
                                        max(0, deadline - time.monotonic()))
        except TimeoutError as exc:
            raise UnavailableError("catalogue search timed out; request may still be settling") from exc
        if controller._generation() != generation:
            raise StaleResultError("runtime changed during catalogue search")
        return parse(resource, query=query, session_id=b.session_id, search_id=request_id,
                     generation=generation, warnings=warnings).to_dict()
