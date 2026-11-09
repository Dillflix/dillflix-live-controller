"""Task-specific model inputs; executor routing and planning state stay in code."""
from .labels import teams

IDENTITY_CONTRACT = "event-identity-v2"

CONSTRAINT_FIELDS = ("channel", "stream_title", "language", "coverage_type", "presentation")
TEAM_FIELDS = ("name", "full_name", "short_name", "abbreviation", "city", "aliases")


def route_constraints(option):
    return {key: option[key] for key in CONSTRAINT_FIELDS if option.get(key) is not None}


def event_context(snapshot):
    event = snapshot.get("event") or {}
    identity = {k: event[k] for k in (
        "event_name", "short_name", "sport", "league", "league_name", "start_time",
        "season_year", "season_type", "week", "tournament_name", "round_name",
        "draw_type", "event_note", "main_card_start", "session_name", "session_type", "round", "stage",
    ) if event.get(k) is not None}
    for side, team in zip(("away", "home"), teams(snapshot)):
        if isinstance(team, dict):
            identity[side + "_team_details"] = {k: team[k] for k in TEAM_FIELDS if team.get(k)}
        elif team:
            identity[side + "_team"] = team
    return {**{k: snapshot[k] for k in ("kind", "title", "competition", "sports", "start_time")
               if snapshot.get(k) is not None}, "event": identity}



def candidate_context(item, *, messaging=False):
    """Only identity text and explicit constraint evidence, never executor policy."""
    result = {k: item.get(k) for k in ("content_id", "title", "titles", "synopsis", "starts_at", "ends_at")}
    if messaging:
        # Preserve field paths used by evidence validation; strip icon/action/raw metadata.
        def text_messages(value):
            if not isinstance(value, dict):
                return {}
            messages = {}
            for key, child in value.items():
                if key == "message" and isinstance(child, str):
                    messages[key] = child
                elif isinstance(child, dict):
                    nested = text_messages(child)
                    if nested:
                        messages[key] = nested
            return messages
        result["entitlement_messaging"] = text_messages(item.get("entitlement_messaging"))
    return result
