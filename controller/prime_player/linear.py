"""Bind a Teamarr TV airing to explicit Prime station and schedule evidence.

A station is the playback locator, never the identity of the requested program.
Guide windows bound verification; they do not establish sporting completion.
"""
from datetime import UTC, datetime, timedelta
import re
import time

from .labels import norm, prime_option
from .match_context import IDENTITY_CONTRACT

GUIDE_TTL_SECONDS = 60
GTI = re.compile(r"amzn1\.dv\.gti\.[A-Za-z0-9-]+\Z")


def is_linear(request):
    return (request["content_snapshot"].get("broadcast") or {}).get("coverage_type") == "linear_program"


def is_idle(request):
    return request["content_snapshot"].get("source") == "prime_idle"


def catalogue_capability(request):
    return "linear_catalog" if is_linear(request) or is_idle(request) else "search"


async def read_catalogue(player, request, query, timeout, ownership):
    if not is_linear(request) and not is_idle(request):
        return await player.search(query, timeout, ownership)
    started = time.monotonic()
    params = {"facet": "sports", "max_pages": 1 if is_linear(request) else 3} if is_idle(request) else {}
    result = await player.rpc("linear_catalog", timeout=timeout, ownership=ownership, budget=timeout + 15, **params)
    if is_idle(request):
        # Revalidate the exact selected programme first. Most linear stations
        # are on the first guide page; unrelated later pages cannot improve a
        # positive exact match and can consume almost its entire evidence TTL.
        selected, _ = (choose_airing if is_linear(request) else choose_idle_event)(request, result)
        remaining = timeout - (time.monotonic() - started)
        if (not selected or selected.get('readiness') != 'ready') and remaining >= 5 and (
                result.get('catalogue_coverage') or {}).get('exhausted') is not True:
            result = await player.rpc('linear_catalog', timeout=remaining, ownership=ownership,
                                      budget=remaining + 15, facet='sports', max_pages=8)
    # The player may renew its network evidence after a long catalogue read.
    # Keep only normalized, bounded evidence in durable controller reports.
    return {**{k: result.get(k) for k in (
        "session_id", "generation", "network_context_id", "observed_at", "coverage", "complete",
        "station_summary", "events", "catalogue_coverage",
    )}, "search_id": result.get("request_id"), "query": query}


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value.astimezone(UTC) if value.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def airing_valid_until(selected, now):
    """No observation may outlive either this slot or its guide evidence."""
    airing = selected.get("linear_airing") or {}
    start, end, observed = (timestamp(airing.get(k)) for k in ("starts_at", "ends_at", "observed_at"))
    if not all((start, end, observed)) or not start <= now < end or not observed <= now:
        return None
    limit = min(end, observed + timedelta(seconds=GUIDE_TTL_SECONDS))
    return limit if now < limit else None


def choose_airing(request, results, *, now=None):
    """Exact channel/title/window matching; unknown/partial coverage stays unknown."""
    now = now or datetime.now(UTC)
    summary = results.get("station_summary")
    summary = summary if isinstance(summary, dict) else {}
    observed = timestamp(results.get("observed_at"))
    audit = {"method": "linear_airing", "match_status": "uncertain",
             "coverage": results.get("coverage"), "has_more": summary.get("has_more"),
             "reason": "Exact TV airing has not been established"}
    if (results.get("complete") is not True or summary.get("schema") != "live_tv_epg"
            or not observed or not 0 <= (now - observed).total_seconds() < GUIDE_TTL_SECONDS):
        return None, {**audit, "reason": "Live TV guide is missing, stale or unrecognized"}
    snapshot = request["content_snapshot"]
    expected = snapshot.get("prime_idle") or {}
    start, end = timestamp(snapshot.get("start_time")), timestamp(snapshot.get("expected_end_time"))
    if not start or not end or end <= start or not norm(snapshot.get("title")):
        return None, {**audit, "reason": "Requested TV airing lacks an explicit title and window"}
    stations = summary.get("stations")
    if not isinstance(stations, list) or len(stations) > 200:
        return None, {**audit, "reason": "Live TV station list is invalid"}
    groups = {}
    for station in stations:
        if not isinstance(station, dict):
            return None, audit
        sid = station.get("station_id")
        if isinstance(sid, str) and GTI.fullmatch(sid):
            groups.setdefault(sid, []).append(station)
    choices = []
    for sid, occurrences in groups.items():
        if expected and sid != expected.get("station_id"):
            continue
        station = occurrences[0]
        # Repeated rows cannot silently erase conflicting access or station labels.
        if any(any(s.get(k) != station.get(k) for k in
                   ("name", "entitlement_status", "has_watch_action")) for s in occurrences):
            continue
        airings = []
        invalid = False
        for occurrence in occurrences:
            rows = occurrence.get("airings")
            if not isinstance(rows, list) or len(rows) > 1000 or any(not isinstance(a, dict) for a in rows):
                invalid = True
                break
            airings.extend(rows)
        if invalid:
            continue
        overlaps = [a for a in airings if timestamp(a.get("starts_at")) is not None
                    and timestamp(a.get("ends_at")) is not None
                    and timestamp(a["starts_at"]) < end and timestamp(a["ends_at"]) > start]
        matches = [a for a in overlaps if timestamp(a["starts_at"]) == start
                   and timestamp(a["ends_at"]) == end and norm(a.get("title")) == norm(snapshot["title"])
                   and a.get("station_id") == sid and isinstance(a.get("airing_id"), str)
                   and a["airing_id"].startswith("amzn1.pv.linear.airing.")]
        if not matches or len(matches) != len(overlaps) or len({a["airing_id"] for a in matches}) != 1:
            continue
        airing = matches[0]
        if expected and (airing["airing_id"] != expected.get("airing_id") or any(
            airing.get(k) != expected.get(k) for k in ("title", "synopsis", "episode_title")
        )):
            continue
        if any(a.get("airing_id") == airing["airing_id"] and (
                timestamp(a.get("starts_at")) != start or timestamp(a.get("ends_at")) != end
                or norm(a.get("title")) != norm(snapshot["title"]) or a.get("station_id") != sid
                or any(a.get(k) != airing.get(k) for k in ("synopsis", "episode_title")))
               for a in airings):
            continue
        for option in request["allowed_viewing_options"]:
            if (not prime_option(option) or not norm(option.get("channel"))
                    or norm(option["channel"]) != norm(station.get("name"))
                    or option.get("language")
                    or option.get("presentation") not in (None, "live")
                    or option.get("coverage_type") not in (None, "linear_program")
                    or (option.get("stream_title") and norm(option["stream_title"]) != norm(airing["title"]))):
                continue
            status = station.get("entitlement_status")
            readiness = "access_unknown"
            if status == "UNENTITLED":
                readiness = "feeds_locked"
            elif status == "ENTITLED" and station.get("has_watch_action") is True:
                if now < start:
                    readiness = "waiting_for_feed"
                elif now < end:
                    readiness = "ready"
            choices.append({"content_id": sid, "viewing_option_id": option["id"],
                            "identity_contract": IDENTITY_CONTRACT, "title": airing["title"],
                            "entitlement_status": status, "readiness": readiness,
                            "linear_airing": {**{k: airing.get(k) for k in
                                ("airing_id", "station_id", "title", "starts_at", "ends_at", "episode_title")},
                                "station_name": station["name"], "observed_at": results["observed_at"]}})
    # Prefer a supported entitled route; do not turn incomplete/ambiguous rows into a denial.
    usable = [c for c in choices if c["readiness"] in {"ready", "waiting_for_feed"}]
    if usable:
        selected = min(usable, key=lambda c: (c["content_id"], c["viewing_option_id"]))
        return selected, {**audit, "match_status": "matched", "reason": "Exact channel, title and TV slot",
                          "feeds": [selected]}
    return None, {**audit, "feeds": choices}


def choose_idle_event(request, results, *, now=None):
    """Revalidate the chooser's exact native event; never rematch another feed."""
    from ..idle_sports import catalogue_candidates
    now = now or datetime.now(UTC)
    expected = request["content_snapshot"].get("prime_idle") or {}
    audit = {"method": "prime_idle_event", "match_status": "uncertain",
             "reason": "Selected live event is not confirmed in the current catalogue"}
    for candidate in catalogue_candidates(results, now):
        if candidate["kind"] != "event" or candidate.get("content_id") != expected.get("content_id"):
            continue
        if any(candidate.get(k) != expected.get(k) for k in ("title", "synopsis", "starts_at", "genres")):
            continue
        options = [o for o in request["allowed_viewing_options"] if prime_option(o)
                   and not o.get("channel") and not o.get("language") and o.get("presentation") == "live"]
        if not options:
            continue
        selected = {"content_id": candidate["content_id"], "viewing_option_id": options[0]["id"],
            "title": candidate["title"], "identity_contract": IDENTITY_CONTRACT,
            "entitlement_status": "ENTITLED", "event_state": "LIVE", "readiness": "ready"}
        return selected, {**audit, "match_status": "matched", "feeds": [selected],
                          "reason": "Exact chosen Prime live event"}
    return None, audit
