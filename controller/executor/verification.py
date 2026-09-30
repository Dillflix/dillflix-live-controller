"""Deterministic comparisons of independently observed facts to requested content."""

import re
import unicodedata
from datetime import timedelta
from zoneinfo import ZoneInfo

from ..planner import parse_time
from .adb import PACKAGE

PRIME_APPS = {"primevideo", "amazonprimevideo", "amazonprime", "comamazonfirebat"}
BLOCKED = re.compile(
    r"\b(replay|highlights?|subscribe|subscription required|purchase|buy|rent|start over|beginning|sign in)\b",
    re.I,
)
LIVE = re.compile(r"\b(live|en direct)\b", re.I)
FINAL_GAME = re.compile(r"\b(final|full[ -]?time|game over|match ended|event ended)\b|^FT$", re.I)
FINAL_COVERAGE = re.compile(
    r"\b(broadcast|coverage|session|event)\s+(?:has\s+)?(?:ended|concluded|finished|complete)\b", re.I
)


def norm(value):
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(c for c in text if c.isalnum() and not unicodedata.combining(c))


def prime_option(option):
    return norm(option.get("app")) in PRIME_APPS


def team_aliases(team):
    if not isinstance(team, dict):
        return {norm(team)} - {""}
    values = [team.get(k) for k in ("name", "full_name", "short_name", "abbreviation")]
    values += team.get("aliases", []) if isinstance(team.get("aliases"), list) else []
    if team.get("city") and team.get("name"):
        values.append(team["city"] + " " + team["name"])
    return {norm(x) for x in values if isinstance(x, str) and x.strip()}


def teams(snapshot):
    event = snapshot.get("event") or {}
    return [event.get(side + "_team_details") or event.get(side + "_team") for side in ("away", "home")]


def search_queries(snapshot):
    pair = teams(snapshot)

    def label(team):
        return (
            (team.get("name") or team.get("full_name") or team.get("short_name"))
            if isinstance(team, dict)
            else team
        )

    names = [label(t) for t in pair]
    queries = []
    if all(names):
        queries.append(" ".join(names))  # Archive: paired opponents improve search disambiguation.
    queries.append(snapshot.get("title", ""))
    if all(names):
        queries.append(names[0])
    return list(dict.fromkeys(q.strip()[:300] for q in queries if isinstance(q, str) and q.strip()))[:3]


def date_agrees(text, snapshot, captured_at, timezone):
    if not text or not snapshot.get("start_time"):
        return True  # A date is not exposed on every live player/scoreboard.
    expected = parse_time(snapshot["start_time"]).astimezone(ZoneInfo(timezone)).date()
    current = captured_at.astimezone(ZoneInfo(timezone)).date()
    if "tomorrow" in text.casefold():
        return expected == current + timedelta(days=1)
    if "today" in text.casefold():
        return expected == current
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso:
        return str(expected) == iso[0]
    months = "jan feb mar apr may jun jul aug sep oct nov dec".split()
    month = re.search(r"\b(" + "|".join(months) + r")[a-z]*\s+(\d{1,2})\b", text, re.I)
    if month:
        return expected.month == months.index(month[1].lower()) + 1 and expected.day == int(month[2])
    # Unrecognized visible date must not be silently treated as matching.
    return False


def identity_matches(identity, snapshot, captured_at, timezone="America/Vancouver"):
    if identity is None or identity.kind == "unknown":
        return False
    event = snapshot.get("event") or {}
    expected_league = snapshot.get("competition") or event.get("league")
    if expected_league and identity.competition and norm(expected_league) != norm(identity.competition):
        return False
    if not date_agrees(identity.date_text, snapshot, captured_at, timezone):
        return False
    pair = teams(snapshot)
    if all(pair):
        if identity.kind != "game" or len(identity.teams) != 2:
            return False
        actual = [norm(t) for t in identity.teams]
        left, right = (team_aliases(t) for t in pair)
        return bool(
            actual[0] != actual[1]
            and ((actual[0] in left and actual[1] in right) or (actual[1] in left and actual[0] in right))
        )
    expected_kind = "session" if snapshot.get("kind") == "session" else "broadcast"
    if identity.kind != expected_kind:
        return False
    titles = [snapshot.get("title"), (snapshot.get("broadcast") or {}).get("name")]
    titles += snapshot.get("aliases", []) if isinstance(snapshot.get("aliases"), list) else []
    return norm(identity.title) in {norm(t) for t in titles if t}


def route_matches(identity, option):
    if identity is None or not prime_option(option):
        return False
    # Explicit provider/language constraints cannot be silently satisfied by some
    # other Prime subscription or alternate feed. Preserve uncertainty.
    for requested, observed in (
        (option.get("channel"), identity.provider),
        (option.get("language"), identity.language),
    ):
        if (
            isinstance(requested, str)
            and requested.strip()
            and norm(requested) not in {norm("Prime Video"), norm(PACKAGE)}
        ):
            if not observed or norm(requested) != norm(observed):
                return False
    return True


def activation_allowed(scene, request, option, frame, timezone):
    focus = scene.focus
    if scene.blocker != "none" or not focus.label or BLOCKED.search(focus.label):
        return False
    if focus.role == "navigation":
        return not re.search(r"\b(watch|play|resume|start)\b", focus.label, re.I)
    if focus.role not in {"event", "play_live"}:
        return False
    if not identity_matches(focus.identity, request["content_snapshot"], frame.captured_at, timezone):
        return False
    if not route_matches(focus.identity, option):
        return False
    return bool(
        focus.availability == "live"
        and focus.live_text
        and LIVE.search(focus.live_text)
        and not BLOCKED.search(focus.live_text)
    )


def playing_session(frame):
    active = [s for s in frame.sessions if s["active"] and s["state"] == 3]
    return active[0] if len(active) == 1 and active[0]["package"] == PACKAGE else None


def playback_sample(scene, frame, request, timezone):
    player = scene.player
    return bool(
        scene.surface == "player"
        and scene.blocker == "none"
        and player
        and identity_matches(player.identity, request["content_snapshot"], frame.captured_at, timezone)
        and player.live_edge is True
        and player.live_text
        and LIVE.search(player.live_text)
        and player.transport in {"playing", "unknown"}
        and playing_session(frame)
    )


def progression(previous, current):
    old_frame, old_scene = previous
    frame, scene = current
    elapsed = (frame.captured_at - old_frame.captured_at).total_seconds()
    if not 1 <= elapsed <= 90:
        return False
    a, b = old_scene.player.position_seconds, scene.player.position_seconds
    if a is not None and b is not None and 0 < b - a <= elapsed * 3 + 5:
        return True
    before, after = playing_session(old_frame), playing_session(frame)
    if not before or not after:
        return False
    a, b = before.get("position_ms"), after.get("position_ms")
    return a is not None and b is not None and a >= 0 and 0 < b - a <= (elapsed * 3 + 5) * 1000


def completed(scene, frame, request, timezone):
    evidence = scene.completion
    if (
        scene.blocker != "none"
        or not evidence.final_text
        or not identity_matches(evidence.identity, request["content_snapshot"], frame.captured_at, timezone)
    ):
        return False
    kind = (
        "game"
        if all(teams(request["content_snapshot"]))
        else "session"
        if request["content_snapshot"].get("kind") == "session"
        else "broadcast"
    )
    if evidence.scope != kind:
        return False
    if re.search(r"\b(not|half[ -]?time|intermission|quarter|period)\b", evidence.final_text, re.I):
        return False
    return bool((FINAL_GAME if kind == "game" else FINAL_COVERAGE).search(evidence.final_text))


def evidence(frame, summary, *, decision=None):
    item = {
        "method": "device_observation",
        "evidence_id": frame.sha256[:32] + ":" + frame.captured_at.isoformat(),
        "summary": summary[:1000],
        "confidence": None,
        "captured_at": frame.captured_at.isoformat(),
    }
    if decision:
        item["decision"] = decision
    return item
