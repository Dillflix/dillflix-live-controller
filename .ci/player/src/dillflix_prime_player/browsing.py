"""Bounded automatic browsing under the controller's existing ownership lease."""

from dataclasses import replace
from datetime import datetime, timezone
import math
import time
import uuid
from .errors import BusyError, OutcomeUnknownError, StaleResultError
from .models import PageInfo, PagesResults, BrowseResults, SearchTile
from .tile_access import is_locked
from .page_layout import layout, route, merge_rows, bind


def _deadline(timeout, maximum=120, minimum=40):
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not minimum <= timeout <= maximum
    ):
        raise ValueError(f"browse timeout must be 40..{maximum} seconds")
    end = time.monotonic() + timeout

    def remaining():
        left = end - time.monotonic()
        if left <= 0:
            raise TimeoutError("page collection deadline exceeded")
        return left

    return remaining


def _prepare(c, remaining):
    if c._active_attempts():
        raise BusyError("finish playback verification before browsing")
    if any(a["state"] == "unknown" for a in c.attempts.values()):
        c.ownership.reconcile_unknown()
    c._require("native_inspection")
    c._ready(min(20, remaining()))
    c.handles.clear()
    c.current_attempt_id = None
    return c._generation()


def _snapshot(c, remaining, expected=None):
    c._ready(min(20, remaining()))
    start, cid = c.backend.cursor, uuid.uuid4().hex
    c.backend.command("inspect-native", command_id=cid, timeout=min(20, remaining()))

    def received():
        return next(
            (
                r
                for r in c.backend.records(start)
                if r.get("kind") == "native_query"
                and r["data"].get("inspectionId") == cid
                and r["data"].get("method") == "inspectPage"
            ),
            None,
        )

    record = c.backend.wait(received, min(25, remaining()))
    parsed = layout(record)
    if expected is not None and parsed["page"].get("pageId") != expected:
        raise StaleResultError("rendered destination does not match requested page")
    return record, parsed


def _navigate(c, params, remaining):
    c._require("javascript_navigation")
    c.backend.command(
        "navigate",
        destination="COLLECTION_PAGE",
        params=params,
        timeout=min(20, remaining()),
    )
    expected = params["pageType"] + "::" + params["pageId"]

    # The old ready state may still be present after the command receipt.
    def arrived():
        state = c.backend.health()["state"]
        return state.get("ready") and state.get("page", {}).get("pageId") == expected

    c.backend.wait(arrived, min(25, remaining()))
    return expected


def _discover(c, remaining):
    record, parsed = _snapshot(c, remaining)
    if not parsed["tabs"]:
        expected = _navigate(c, dict(pageType="home", pageId="home"), remaining)
        record, parsed = _snapshot(c, remaining, expected)
    if not parsed["tabs"]:
        raise StaleResultError("native navigation tabs unavailable")
    return record, parsed["tabs"]


def inventory(c, timeout, ownership):
    remaining = _deadline(timeout)
    with c.operation(ownership):
        generation = _prepare(c, remaining)
        _, tabs = _discover(c, remaining)
        if generation != c._generation():
            raise StaleResultError("runtime changed during navigation discovery")
        return PagesResults(
            c.backend.session_id,
            generation,
            datetime.now(timezone.utc).isoformat(),
            tuple(PageInfo(**t) for t in tabs),
        ).to_dict()


def _pause(c, seconds, remaining):
    until = time.monotonic() + seconds
    c.backend.wait(lambda: time.monotonic() >= until, remaining())


def _input(c, lease, key):
    # Same physical-write mutex as manual handoff. An acknowledged handoff cannot
    # overtake a queued automatic ADB write.
    while not c.ownership.input_lock.acquire(timeout=0.1):
        lease.check()
    try:
        lease.check()
        try:
            c.backend.manual_input(key=key)
        except Exception as exc:
            with c.ownership.lock:
                c.ownership.input_uncertain = True
            raise OutcomeUnknownError(
                "automatic direction delivery uncertain; do not retry"
            ) from exc
        lease.check()
    finally:
        c.ownership.input_lock.release()


def _assert_scope(c, start, generation):
    if generation != c._generation():
        raise StaleResultError("runtime changed during page collection")
    if any(
        r.get("kind") == "control_app_event"
        and r["data"].get("name") == "NAVIGATE_TO_PAGE"
        for r in c.backend.records(start)
    ):
        raise StaleResultError("navigation changed during page collection")


def _collect_page(c, tab, limit, max_rows, max_steps, remaining, lease, generation):
    expected = _navigate(c, route(tab), remaining)
    record, parsed = _snapshot(c, remaining, expected)
    browse_id = uuid.uuid4().hex
    watch = c.backend.command(
        "watch-page",
        pageId=expected,
        command_id=browse_id,
        timeout=min(15, remaining()),
    )["result"]
    started_ms = watch["startedTimeMs"]
    start = c.backend.cursor
    rows = merge_rows([], parsed["rows"], max_rows)
    previous_signature = None
    unchanged = 0
    stop = "step_limit"
    # Sports puts its event row after the league row; channels commonly put
    # events first. Extend that already observed row without scanning the page.
    target = next(
        (
            i
            for i, r in enumerate(rows)
            if any(t["availability"] in ("live", "upcoming") for t in r["tiles"])
        ),
        max(0, len(rows) - 1),
    )
    entry_steps = target + 1
    # Direction-only traversal never selects tiles or invokes their actions.
    for step in range(max_steps):
        lease.check()
        _assert_scope(c, start, generation)
        count = sum(len(r["tiles"]) for r in rows)
        if step >= entry_steps and count >= limit:
            stop = "limit_met"
            break
        if remaining() < 42:
            stop = "time_budget"
            break
        key = "down" if step < entry_steps else "right"
        # Horizontal input on an unresolved top-nav focus could switch pages.
        # Require a visible tile row after the vertical entry steps.
        if key == "right" and not any(
            t["intersects_viewport"] for r in rows for t in r["tiles"]
        ):
            stop = "row_not_visible"
            break
        _input(c, lease, key)
        _pause(c, 1, remaining)
        record, parsed = _snapshot(c, remaining, expected)
        rows = merge_rows(rows, parsed["rows"], max_rows)
        signature = [
            (r["key"], [(t["artwork"], t["bounds"]) for t in r["tiles"]]) for r in rows
        ]
        unchanged = unchanged + 1 if signature == previous_signature else 0
        previous_signature = signature
        if unchanged >= 3:
            stop = "no_progress"
            break
    if sum(len(r["tiles"]) for r in rows) >= limit:
        stop = "limit_met"
    # Captured impressions batch for ~30s. Keep one cancellable, finite window.
    _pause(c, min(35, max(0, remaining() - 7)), remaining)
    record, parsed = _snapshot(c, remaining, expected)
    rows = merge_rows(rows, parsed["rows"], max_rows)
    _assert_scope(c, start, generation)
    bind(
        rows,
        c.backend.records(start),
        browse_id=browse_id,
        page_id=expected,
        started_ms=started_ms,
        ended_ms=record["data"]["receivedTimeMs"],
    )
    tiles = []
    for row in rows:
        for t in row["tiles"]:
            if len(tiles) == limit:
                break
            handle = f"{c.backend.session_id}:{browse_id}:{row['key']}:{t['position']}"
            tiles.append(SearchTile(handle=handle, is_locked=is_locked(t["artwork"]), **t))
    if len(tiles) >= limit:
        stop = "limit_met"
    result = BrowseResults(
        query="",
        session_id=c.backend.session_id,
        search_id=browse_id,
        generation=generation,
        observed_at=datetime.now(timezone.utc).isoformat(),
        tiles=tuple(tiles),
        source="page",
        page_id=tab["id"],
        requested_limit=limit,
        row_limit=max_rows,
        stop_reason=stop,
        coverage="bounded_loaded_rows",
    )
    return result


def discover(c, pages, limit_per_page, max_rows, max_steps, timeout, ownership, resolve_access=True):
    """One ownership/result scope for all requested pages, in caller order.

    Bounds apply per page; timeout is the overall wall-clock budget. Handles are
    published together only after success. A failed/cancelled batch publishes none.
    """
    if type(resolve_access) is not bool:
        raise ValueError("resolve_access must be boolean")
    if not isinstance(pages, (list, tuple)) or not 1 <= len(pages) <= 8:
        raise ValueError("pages must contain 1..8 discovered page IDs or names")
    if any(not isinstance(p, str) or not 1 <= len(p) <= 256 for p in pages):
        raise ValueError("each page must be a discovered page ID or name")
    for name, value, low, high in [
        ("limit_per_page", limit_per_page, 1, 100),
        ("max_rows", max_rows, 1, 6),
        ("max_steps", max_steps, 0, 32),
    ]:
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be {low}..{high}")
    remaining = _deadline(timeout, 960)
    if timeout < 40 * len(pages):
        raise ValueError("timeout must allow at least 40 seconds per requested page")
    with c.operation(ownership) as lease:
        generation = _prepare(c, remaining)
        _, tabs = _discover(c, remaining)
        selected = []
        for page in pages:
            matches = [
                t
                for t in tabs
                if page.casefold()
                in {
                    str(t.get(k) or "").casefold()
                    for k in ("id", "title", "title_hint")
                }
            ]
            if len(matches) != 1:
                raise ValueError("page is absent or ambiguous; use an ID from pages()")
            route(matches[0])  # Validate every destination before collecting any.
            if matches[0]["id"] in {t["id"] for t in selected}:
                raise ValueError("pages must identify distinct destinations")
            selected.append(matches[0])
        tiles, indices, summaries = [], {}, []
        for index, tab in enumerate(selected):
            lease.check()
            budget = min(120, remaining() / (len(selected) - index))
            result = _collect_page(
                c,
                tab,
                limit_per_page,
                max_rows,
                max_steps,
                _deadline(budget, minimum=0),
                lease,
                generation,
            )
            summaries.append(
                dict(
                    page_id=tab["id"],
                    title=tab["title"],
                    observed_at=result.observed_at,
                    tile_count=len(result.tiles),
                    stop_reason=result.stop_reason,
                )
            )
            for tile in result.tiles:
                occurrence = dict(
                    page_id=tab["id"],
                    collection=tile.collection,
                    position=tile.position,
                    observed_at=result.observed_at,
                    artwork=tile.artwork,
                    availability=tile.availability,
                    is_locked=tile.is_locked,
                )
                tile = replace(tile, occurrences=(occurrence,))
                # Never deduplicate unresolved identities using titles/artwork.
                key = tile.content_id
                if key and key in indices:
                    i = indices[key]
                    prior = tiles[i]
                    tiles[i] = replace(
                        prior,
                        occurrences=prior.occurrences + tile.occurrences,
                        labels=tuple(dict.fromkeys((*prior.labels, *tile.labels))),
                        artwork=tuple(dict.fromkeys((*prior.artwork, *tile.artwork))),
                        is_locked=prior.is_locked if prior.is_locked == tile.is_locked else None,
                        availability=prior.availability
                        if prior.availability == tile.availability
                        else None,
                    )
                else:
                    if key:
                        indices[key] = len(tiles)
                    tiles.append(tile)
        lease.check()
        remaining()
        if generation != c._generation():
            raise StaleResultError("runtime changed during discovery")
        result = BrowseResults(
            query="",
            session_id=c.backend.session_id,
            search_id=uuid.uuid4().hex,
            generation=generation,
            observed_at=datetime.now(timezone.utc).isoformat(),
            tiles=tuple(tiles),
            source="discover",
            requested_limit=limit_per_page,
            row_limit=max_rows,
            stop_reason="pages_collected",
            coverage="bounded_loaded_rows",
            pages=tuple(t["id"] for t in selected),
            page_results=tuple(summaries),
        )
        if resolve_access:
            from .resolution import enrich

            resolution_start = c.backend.cursor
            result = enrich(c, result, remaining)
            tiles = result.tiles
            _assert_scope(c, resolution_start, generation)
        c.handle_runtime_generation = generation
        # Result-handle lifetime begins when the combined result is published;
        # per-page acquisition times remain explicit above. Play resolves live anew.
        now = time.monotonic()
        c.handles = {t.handle: (t, now) for t in tiles}
        return result.to_dict()
