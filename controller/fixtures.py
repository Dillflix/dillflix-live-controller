"""Illustrative scenarios, not an assertion about a real sports schedule."""

from datetime import UTC, datetime, timedelta

BASE = datetime(2026, 10, 4, 7, tzinfo=UTC)  # Midnight Pacific on the sample day.


def instant(minutes):
    return (BASE + timedelta(minutes=minutes)).isoformat()


def make_team(city, name, code):
    return {
        "id": code,
        "provider": "demo",
        "city": city,
        "name": name,
        "full_name": f"{city} {name}",
        "short_name": name,
        "abbreviation": code,
        "logo_url": None,
    }


def fixtures(scenario="normal"):
    rows = [
        ("redzone", "NFL RedZone", "nfl", "regular", 600, 1020, None, None),
        (
            "lions",
            "Lions at Bills",
            "nfl",
            "regular",
            780,
            975,
            make_team("Detroit", "Lions", "DET"),
            make_team("Buffalo", "Bills", "BUF"),
        ),
        ("golf", "PGA Tour final round", "pga", "final_round", 720, 1020, None, None),
        (
            "chiefs",
            "Chiefs at Ravens",
            "nfl",
            "regular",
            805,
            1005,
            make_team("Kansas City", "Chiefs", "KC"),
            make_team("Baltimore", "Ravens", "BAL"),
        ),
        (
            "canadiens",
            "Canadiens at Maple Leafs",
            "nhl",
            "playoffs",
            810,
            975,
            make_team("Montreal", "Canadiens", "MTL"),
            make_team("Toronto", "Maple Leafs", "TOR"),
        ),
        (
            "jays",
            "Blue Jays at Yankees",
            "mlb",
            "playoffs",
            840,
            1020,
            make_team("Toronto", "Blue Jays", "TOR"),
            make_team("New York", "Yankees", "NYY"),
        ),
        (
            "celtics",
            "Celtics at Heat",
            "nba",
            "regular",
            855,
            1005,
            make_team("Boston", "Celtics", "BOS"),
            make_team("Miami", "Heat", "MIA"),
        ),
    ]
    entries = []
    for key, title, league, phase, start, end, away, home in rows:
        source = "nfl_redzone" if key == "redzone" else "golf" if key == "golf" else "games"
        event = {
            "league": league,
            "season_type": phase,
            "away_team_details": away,
            "home_team_details": home,
            "event_id": key,
            "provider": "demo",
            "home_team": home["full_name"] if home else None,
            "away_team": away["full_name"] if away else None,
        }
        options = [
            {
                "id": f"demo-option:{key}:{app}",
                "app": app,
                "decision": "eligible",
                "reasons": [],
                "presentation": "live",
                "basis": "demo",
                "coverage_type": "full",
            }
            for app in (
                ["prime_video"]
                if league == "nfl"
                else ["tsn", "sportsnet"]
                if league == "pga"
                else ["demo_provider"]
            )
        ]
        entries.append(
            {
                "id": f"demo:{key}",
                "kind": "event" if home else "broadcast",
                "source": source,
                "title": title,
                "competition": league,
                "sports": [league],
                "provider": "demo",
                "start_time": instant(start),
                "expected_end_time": instant(end),
                "end_time_estimated": True,
                "timing_basis": "demo_duration",
                "status": "unknown",
                "event": event,
                "sessions": [],
                "broadcast": None,
                "related_ids": [],
                "viewing_options": options,
                "artwork": {},
                "_simulation": {"actual_end_time": instant(end), "phase": phase},
            }
        )
    now = {
        "normal": 800,
        "overlap": 870,
        "overtime": 980,
        "delayed": 835,
        "failure": 800,
        "stale": 800,
        "empty": 1100,
    }[scenario]
    if scenario == "overtime":
        next(x for x in entries if x["id"] == "demo:canadiens")["_simulation"]["actual_end_time"] = instant(
            1005
        )
    if scenario == "delayed":
        next(x for x in entries if x["id"] == "demo:canadiens")["_simulation"]["override"] = "delayed"
    if scenario == "stale":
        next(x for x in entries if x["id"] == "demo:redzone")["_simulation"]["override"] = "unknown"
    if scenario == "failure":
        next(x for x in entries if x["id"] == "demo:redzone")["_simulation"]["fail_playback"] = True
    return entries, instant(now)


def default_device(mode):
    rules = [
        {"id": "redzone", "name": "NFL RedZone", "league": "nfl", "source": "nfl_redzone"},
        {"id": "nfl-playoffs", "name": "NFL playoffs", "league": "nfl", "phase": "playoffs"},
        {"id": "nhl-playoffs", "name": "NHL playoffs", "league": "nhl", "phase": "playoffs"},
        {"id": "nfl-regular", "name": "NFL regular season", "league": "nfl", "phase": "regular"},
        {"id": "other", "name": "Other live sports", "league": "all"},
    ]
    if mode == "demo":
        rules.insert(
            1,
            {
                "id": "canadiens",
                "name": "Canadiens playoffs",
                "league": "nhl",
                "phase": "playoffs",
                "team_id": "demo:nhl:MTL",
            },
        )
        rules.insert(
            -1,
            {
                "id": "blue-jays",
                "name": "Blue Jays playoffs",
                "league": "mlb",
                "phase": "playoffs",
                "team_id": "demo:mlb:TOR",
            },
        )
    rules = [
        {"enabled": True, "phase": "any", "team_id": None, "source": None, "kind": None, **r} for r in rules
    ]
    return {
        "id": "living-room",
        "revision": 0,
        "name": "Living room",
        "rules": rules,
        "team_ranks": {},
        "plan": [],
        "automation": "active",
        "intent_version": 0,
        "desired": None,
        "observed": None,
        "playback_state": "waiting",
        "reason": "Starting",
        "preferences": {
            "timezone": "America/Vancouver",
            "minimum_viewing_seconds": 300,
            "switch_cooldown_seconds": 30,
            "same_tier_switching": False,
        },
        "force_switch": False,
        "failures": {},
        "started_at": None,
        "last_switch_at": None,
    }
