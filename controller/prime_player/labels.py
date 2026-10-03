"""Match provider-supplied labels without assuming structured Prime metadata."""

import re
import unicodedata
from datetime import timedelta
from zoneinfo import ZoneInfo

from ..planner import parse_time

PRIME_APPS = {"primevideo", "amazonprimevideo", "amazonprime", "comamazonfirebat"}


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
