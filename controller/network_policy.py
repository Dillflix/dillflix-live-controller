"""Deployment-owned network permission; catalogue/model output grants none."""
import hashlib
import json
from pathlib import Path
import re


class NetworkPolicy:
    def __init__(self, value=None):
        value = value or {}
        if not isinstance(value, dict) or set(value) - {"enabled", "routes", "acquisition_timeout", "max_transitions", "access_ttl"}:
            raise ValueError("Invalid Prime network policy")
        self.enabled = value.get("enabled", False)
        self.routes = value.get("routes", {})
        self.timeout = value.get("acquisition_timeout", 600)
        self.maximum = value.get("max_transitions", 2)
        self.ttl = value.get("access_ttl", 300)
        if type(self.enabled) is not bool or not isinstance(self.routes, dict):
            raise ValueError("Invalid network policy enabled/routes")
        for number, low, high in ((self.timeout, 120, 1800), (self.maximum, 1, 4), (self.ttl, 60, 3600)):
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

    def rule(self, options):
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
