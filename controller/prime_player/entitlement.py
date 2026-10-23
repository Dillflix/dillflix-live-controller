"""Subscription evidence from matched listings and their alternate broadcasts."""

import inspect

from ..executor.models import ExecutorError
from .broadcasts import parse, select
from .labels import prime_option
from .matching import candidates


async def choose_entitlement(matcher, request, results, timezone, *, before_match=None,
                             fetch_broadcasts=None, broadcast_matcher=None):
    items, rejected = candidates(results, request["content_snapshot"], timezone)
    options = [o for o in request["allowed_viewing_options"] if prime_option(o)]
    passes, denied, uncertain, missing_route = [], [], not options, False
    broadcast_checks = []
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
            while pool:
                if before_match:
                    pending = before_match()
                    if inspect.isawaitable(pending):
                        await pending
                choice, audit = await matcher._choose(scoped, results, timezone, eligible=pool)
                passes.append(audit)
                if audit.get("match_status") == "uncertain":
                    uncertain = True
                if not choice:
                    break
                item = next(i for i in pool if i["content_id"] == choice["content_id"])
                evidence = {**choice, **{k: item.get(k) for k in (
                    "title", "entitlement_status", "entitlement_messaging", "event_state", "availability_status"
                )}}
                if status == "ENTITLED":
                    return {**evidence, "readiness": "ready"}, {
                        "match_status": "matched", "passes": passes, "feeds": [evidence],
                        "broadcast_checks": broadcast_checks,
                    }
                if fetch_broadcasts:
                    response = await fetch_broadcasts(results, item["content_id"])
                    if any(response.get(k) != expected for k, expected in (
                        ("session_id", results.get("session_id")),
                        ("generation", results.get("generation")),
                        ("content_id", item["content_id"]),
                    )):
                        raise ExecutorError("prime_stale_result", "Broadcast entitlement belongs to another event or runtime")
                    _, has_more, has_row = parse(response)
                    if has_row:
                        alternative, decision = await select(
                            evidence, item, response, option, target=request["content_snapshot"],
                            matcher=broadcast_matcher,
                        )
                        broadcast_checks.append(decision)
                        if alternative and alternative.get("entitlement_status") == "ENTITLED":
                            return {**alternative, "readiness": "ready"}, {
                                "match_status": "matched", "passes": passes, "feeds": [alternative],
                                "broadcast_checks": broadcast_checks,
                            }
                        if has_more or not alternative or alternative.get("entitlement_status") != "UNENTITLED":
                            uncertain = True
                        else:
                            evidence = alternative
                    else:
                        broadcast_checks.append({"parent": evidence, "request_id": response.get("request_id"),
                                                 "complete": True, "reason": "No alternate broadcast row"})
                if evidence.get("entitlement_status") != "UNENTITLED":
                    uncertain = True
                else:
                    denied.append(evidence)
                    route_denied = True
                if not fetch_broadcasts:
                    break
                # A locked parent does not settle other matching parents.
                pool = [i for i in pool if i["content_id"] != item["content_id"]]
        missing_route |= not route_denied
    audit = {"passes": passes, "feeds": denied, "rejected": rejected,
             "coverage": results.get("coverage"), "complete": results.get("complete"),
             "broadcast_checks": broadcast_checks}
    # This is only this query's result. The background search must exhaust its
    # remaining queries before accepting denial, to look for entitled alternatives.
    if denied and not uncertain and not missing_route:
        return {**denied[0], "readiness": "feeds_locked"}, {**audit, "match_status": "matched"}
    if not denied and not uncertain:
        return None, {**audit, "match_status": "no_match", "reason": "No matching event in this response"}
    return None, {**audit, "match_status": "uncertain", "reason": (
        "Entitlement could not be established for all permitted matching feeds"
    )}
