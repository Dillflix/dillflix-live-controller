"""Choose a broadcast of an already matched event from the captured BTF contract."""

from ..executor.models import ExecutorError
from .broadcast_matching import choose as choose_variant
from .matching import GTI, selection_state


def parse(response):
    resource = response.get("resource")
    containers = resource.get("containerList") if isinstance(resource, dict) else None
    if (
        response.get("complete") is not True
        or response.get("coverage") != "live_details_btf_response"
        or not isinstance(containers, list)
        or len(containers) > 64
    ):
        raise ExecutorError("prime_invalid_broadcasts", "Incomplete or invalid Prime broadcast response")
    items, has_more, rows = [], False, 0
    for container in containers:
        if not isinstance(container, dict) or not isinstance(container.get("items"), list):
            raise ExecutorError("prime_invalid_broadcasts", "Invalid Prime broadcast container")
        if container.get("title") != "Broadcasts":
            # Do not mistake related titles for this event's broadcast choices.
            if any(
                isinstance(i, dict) and i.get("cardType") == "BONUS_SCHEDULE_CARD" for i in container["items"]
            ):
                raise ExecutorError("prime_invalid_broadcasts", "Unrecognized broadcast row")
            continue
        rows += 1
        # Carousel type controls Prime's presentation, not broadcast semantics.
        # Validate the row's contents below, independently of that UI hint.
        if rows > 1:
            raise ExecutorError("prime_invalid_broadcasts", "Multiple broadcast rows")
        has_more = bool(container.get("paginationLink") or container.get("seeMore"))
        for raw in container["items"]:
            if (
                not isinstance(raw, dict)
                or raw.get("contentType") != "LIVE_EVENT_ITEM"
                or raw.get("cardType") != "BONUS_SCHEDULE_CARD"
                or not isinstance(raw.get("gti"), str)
                or not GTI.fullmatch(raw["gti"])
                or not isinstance(raw.get("title"), str)
                or not raw["title"].strip()
            ):
                raise ExecutorError("prime_invalid_broadcasts", "Unrecognized broadcast item")
            messaging = raw.get("entitlementMessaging") or {}
            if not isinstance(messaging, dict):
                raise ExecutorError("prime_invalid_broadcasts", "Invalid broadcast entitlement metadata")
            icons = {
                v.get("icon")
                for v in messaging.values()
                if isinstance(v, dict) and isinstance(v.get("icon"), str)
            }
            entitled = "ENTITLED_ICON" in icons
            offer = "OFFER_ICON" in icons
            items.append(
                dict(
                    content_id=raw["gti"],
                    title=raw["title"],
                    synopsis=raw.get("synopsis") if isinstance(raw.get("synopsis"), str) else None,
                    entitlement_status="ENTITLED"
                    if entitled and not offer
                    else "UNENTITLED"
                    if offer and not entitled
                    else None,
                    event_state=raw.get("liveliness"),
                    entitlement_messaging=messaging,
                )
            )
    if len(items) > 100 or len({i["content_id"] for i in items}) != len(items):
        raise ExecutorError("prime_invalid_broadcasts", "Too many or duplicate broadcast identities")
    return items, has_more, bool(rows)


async def select(parent, parent_item, response, option, *, target, matcher):
    items, has_more, has_row = parse(response)
    if not has_row:
        items = [
            {
                **parent,
                **{k: parent_item.get(k) for k in ("title", "synopsis", "entitlement_messaging")},
            }
        ]
    audit = dict(
        parent=parent,
        request_id=response.get("request_id"),
        candidates=items,
        complete=not has_more,
        policy="english_then_unlabeled; skip_other_explicit_languages",
    )
    if not items:
        return None, {**audit, "match_status": "uncertain", "reason": "Broadcast row has no choices"}
    chosen, decision = await choose_variant(
        items,
        parent={"content_id": parent["content_id"], "title": parent_item["title"]},
        target=target,
        option=option,
        complete=not has_more,
        direct=not has_row,
        matcher=matcher,
    )
    audit.update(decision)
    if chosen is None:
        return None, audit
    selected = {**parent, **chosen}
    if has_row:
        selected.update(parent_content_id=parent["content_id"], starts_at=None, ends_at=None)
    return selected, {**audit, "selected": chosen}


async def refine(request, results, selected, audit, fetch, matcher=None):
    if not selected or selected["readiness"] not in {"ready", "feeds_unavailable"}:
        return selected, audit
    response = await fetch(selected["content_id"])
    if (
        response.get("session_id") != results.get("session_id")
        or response.get("content_id") != selected["content_id"]
        or response.get("generation") != results.get("generation")
    ):
        raise ExecutorError("prime_stale_result", "Broadcast response belongs to another event or runtime")
    parent_item = next(
        i for c in results["containers"] for i in c["items"] if i.get("content_id") == selected["content_id"]
    )
    option = next(o for o in request["allowed_viewing_options"] if o["id"] == selected["viewing_option_id"])
    choice, decision = await select(
        selected, parent_item, response, option, target=request["content_snapshot"], matcher=matcher
    )
    return choice, {
        **audit,
        "event_match": selected,
        "broadcast_selection": decision,
        "match_status": decision["match_status"],
        "reason": decision["reason"],
    }


async def choose_broadcast(request, results, timezone, matcher, fetch, progress):
    """Try independently matched parents until one supplies a playable broadcast.

    Removing a rejected parent only changes this matching pass, never the saved
    search response. The normal matcher must establish every alternative's event
    identity and permitted route; similar search results are not interchangeable.
    Caller deadlines bound the scan. Progress is persisted before each await so
    interruption cannot erase which alternatives remained unexamined.
    """
    remaining, attempts, outcomes = results, [], []
    uncertain = False
    audit = {}

    def report(**changes):
        return {**audit, "parent_selections": list(attempts), **changes}

    while True:
        progress(report(match_status="uncertain", reason="Matching remaining Prime event alternatives"))
        parent, audit = await matcher.choose(request, remaining, timezone)
        if not parent:
            uncertain |= audit.get("match_status") == "uncertain"
            break
        attempt = {"parent": parent, "matching": audit, "state": "pending"}
        attempts.append(attempt)
        progress(report(match_status="uncertain", reason="Inspecting matched Prime event broadcasts"))
        try:
            selected, refined = await refine(request, results, parent, audit, fetch, matcher)
        except ExecutorError as exc:
            if exc.code not in {
                "prime_transport_unknown",
                "prime_operation_unknown",
                "prime_invalid_broadcasts",
                "prime_broadcast_selection_unavailable",
                "prime_broadcast_selection_failed",
                "prime_broadcast_selection_invalid",
            }:
                raise  # Session, ownership and cancellation fences stop the entire scan.
            selected = None
            refined = {**audit, "match_status": "uncertain", "reason": str(exc), "error": exc.detail()}
        state = selection_state(selected, refined)
        attempt.update(state=state, selected=selected, broadcast_selection=refined.get("broadcast_selection"))
        if refined.get("error"):
            attempt["error"] = refined["error"]
        progress({**refined, "parent_selections": list(attempts)})
        if state == "ready":
            return selected, {**refined, "parent_selections": attempts}
        uncertain |= state == "access_unknown"
        outcomes.append((selected, refined))
        remaining = {
            **remaining,
            "containers": [
                {
                    **container,
                    "items": [
                        item for item in container["items"] if item.get("content_id") != parent["content_id"]
                    ],
                }
                for container in remaining["containers"]
            ],
        }

    if uncertain:
        return None, report(
            match_status="uncertain",
            reason="Some matching Prime event alternatives could not be fully evaluated",
            error=next((attempt["error"] for attempt in attempts if attempt.get("error")), None),
        )
    if not outcomes:
        return None, report()
    # Upcoming/unknown access must never be hidden by a locked alternative.
    order = ["waiting_for_feed", "access_unknown", "feeds_unavailable", "feeds_locked", "no_matching_feed"]
    selected, final = min(outcomes, key=lambda outcome: order.index(selection_state(*outcome)))
    return selected, {**final, "parent_selections": attempts}
