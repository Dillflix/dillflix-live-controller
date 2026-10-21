"""Subscription evidence only: no broadcasts, resolver, navigation or playback."""

import inspect

from .labels import prime_option
from .matching import candidates


async def choose_entitlement(matcher, request, results, timezone, *, before_match=None):
    items, rejected = candidates(results, request["content_snapshot"], timezone)
    options = [o for o in request["allowed_viewing_options"] if prime_option(o)]
    passes, denied, uncertain, missing_route = [], [], not options, False
    # An entitled feed is sufficient. Every permitted route must have supported
    # negative evidence before we can deny the event, and ambiguity wins over denial.
    for option in options:
        scoped = {**request, "allowed_viewing_options": [option]}
        route_denied = False
        for status in ("ENTITLED", "unknown", "UNENTITLED"):
            pool = [i for i in items if (
                i.get("entitlement_status") == status if status != "unknown"
                else i.get("entitlement_status") not in {"ENTITLED", "UNENTITLED"}
            )]
            if not pool:
                continue
            if before_match:
                pending = before_match()
                if inspect.isawaitable(pending):
                    await pending
            choice, audit = await matcher._choose(scoped, results, timezone, eligible=pool)
            passes.append(audit)
            if audit.get("match_status") == "uncertain":
                uncertain = True
            if not choice:
                continue
            item = next(i for i in pool if i["content_id"] == choice["content_id"])
            evidence = {**choice, **{k: item.get(k) for k in (
                "title", "entitlement_status", "entitlement_messaging", "event_state", "availability_status"
            )}}
            if status == "ENTITLED":
                return {**evidence, "readiness": "ready"}, {
                    "match_status": "matched", "passes": passes, "feeds": [evidence],
                }
            if status == "unknown":
                uncertain = True
            else:
                denied.append(evidence)
                route_denied = True
        missing_route |= not route_denied
    audit = {"passes": passes, "feeds": denied, "rejected": rejected,
             "coverage": results.get("coverage"), "complete": results.get("complete")}
    # This is only this query's result. The background search must exhaust its
    # remaining queries before accepting denial, to look for entitled alternatives.
    if denied and not uncertain and not missing_route:
        return {**denied[0], "readiness": "feeds_locked"}, {**audit, "match_status": "matched"}
    if not denied and not uncertain:
        return None, {**audit, "match_status": "no_match", "reason": "No matching event in this response"}
    return None, {**audit, "match_status": "uncertain", "reason": (
        "Entitlement could not be established for all permitted matching feeds"
    )}
