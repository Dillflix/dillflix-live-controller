"""Deployment-owned network permission; catalogue/model output grants none."""
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from .acquisition import MAX_ACQUISITION_SECONDS


def city_key(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", value).casefold() if not unicodedata.combining(c)).split())


def event_cities(snapshot):
    event = (snapshot or {}).get("event") or {}
    cities = []
    for side in ("away", "home"):
        team = event.get(side + "_team_details") or event.get(side + "_team")
        city = team.get("city") if isinstance(team, dict) else None
        if isinstance(city, str) and city.strip():
            cities.append(city.strip())
    return cities


class NetworkPolicy:
    def __init__(self, value=None):
        value = value or {}
        if not isinstance(value, dict) or set(value) - {"enabled", "routes", "acquisition_timeout", "max_transitions", "access_ttl", "locations"}:
            raise ValueError("Invalid Prime network policy")
        # Accept legacy deployment files without retaining their 10/30-minute
        # budgets. New API configuration is validated against the five-minute cap.
        timeout = value.get("acquisition_timeout", MAX_ACQUISITION_SECONDS)
        if type(timeout) is not int or not 120 <= timeout <= 1800:
            raise ValueError("Invalid network policy bounds")
        value = {**value, "acquisition_timeout": min(timeout, MAX_ACQUISITION_SECONDS)}
        self.enabled = value.get("enabled", False)
        self.routes = value.get("routes", {})
        self.locations = value.get("locations")
        if self.locations is not None:
            from .models import VPNPolicy
            value = VPNPolicy.model_validate(value).model_dump()
            self.locations = value["locations"]
        self.timeout = value["acquisition_timeout"]
        self.maximum = value.get("max_transitions", 2)
        self.ttl = value.get("access_ttl", 300)
        if type(self.enabled) is not bool or not isinstance(self.routes, dict):
            raise ValueError("Invalid network policy enabled/routes")
        for number, low, high in ((self.timeout, 120, MAX_ACQUISITION_SECONDS), (self.maximum, 1, 4), (self.ttl, 60, 3600)):
            if type(number) is not int or not low <= number <= high:
                raise ValueError("Invalid network policy bounds")
        for route, rule in self.routes.items():
            if not isinstance(route, str) or not route or not isinstance(rule, dict) or set(rule) != {"targets", "fallback_on"}:
                raise ValueError("Network routes require targets and fallback_on")
            targets, reasons = rule["targets"], rule["fallback_on"]
            if not isinstance(targets, list) or not 1 <= len(targets) <= 5 or any(
                not isinstance(t, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", t) for t in targets
            ) or len(set(targets)) != len(targets):
                raise ValueError("Invalid configured network targets")
            if not isinstance(reasons, list) or any(r not in {"feeds_unavailable", "no_matching_feed"} for r in reasons):
                raise ValueError("Network fallback only supports unavailable or absent feeds")
        self.revision = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def rule(self, options, snapshot=None):
        if self.locations is not None:
            if not self.enabled or not any(o.get("app") == "prime_video" for o in options):
                return None
            cities = event_cities(snapshot)
            matches = [{"target_id": row["target_id"], "cities": [c for c in cities if city_key(c) in {city_key(v) for v in row["team_cities"]}]} for row in self.locations]
            matches = [row for row in matches if row["cities"]]
            return {"targets": [row["target_id"] for row in matches] + ["direct"],
                    "fallback_on": ["feeds_unavailable", "feeds_locked", "feeds_language", "no_matching_feed"],
                    "matched_policies": matches, "team_cities": cities}

        # Every permitted Prime route must authorize the switch. Preserve the
        # original options; never give a model a modified catalogue snapshot.
        routes = [o["id"] for o in options if o.get("app") == "prime_video"]
        if not self.enabled or not routes or any(r not in self.routes for r in routes):
            return None
        rules = [self.routes[r] for r in routes]
        return {"targets": [t for t in rules[0]["targets"] if all(t in r["targets"] for r in rules)],
                "fallback_on": [s for s in rules[0]["fallback_on"] if all(s in r["fallback_on"] for r in rules)]}


def read_policy(path):
    if not path:
        return {}
    data = Path(path).read_bytes()
    if len(data) > 65536:
        raise ValueError("Prime network policy too large")
    value = json.loads(data)
    NetworkPolicy(value)
    return value
