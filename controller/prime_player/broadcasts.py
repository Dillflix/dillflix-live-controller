"""Choose a broadcast of an already matched event from the captured BTF contract."""

import re

from ..executor.models import ExecutorError
from .labels import norm
from .matching import EXCLUDED, GTI, tile_state

LANGUAGES = {
    "english": "en",
    "anglais": "en",
    "en": "en",
    "enca": "en",
    "enus": "en",
    "french": "fr",
    "francais": "fr",
    "fr": "fr",
    "frca": "fr",
    "spanish": "es",
    "espanol": "es",
    "german": "de",
    "deutsch": "de",
    "portuguese": "pt",
    "italian": "it",
    "hindi": "hi",
    "arabic": "ar",
}


def title_language(title):
    # A localized synopsis or /en-US artwork URL does not identify audio language.
    # In particular, "French Open" is an event name, not a broadcast qualifier.
    match = re.search(r"\(\s*(?:in\s+|en\s+)?([^()]+)\s*\)\s*$", title, re.I)
    return LANGUAGES.get(norm(match[1])) if match else None


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
        if container.get("type") != "STANDARD_CAROUSEL" or rows > 1:
            raise ExecutorError("prime_invalid_broadcasts", "Unrecognized broadcast row structure")
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
                    language=title_language(raw["title"]),
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


def select(parent, parent_title, response, option):
    items, has_more, has_row = parse(response)
    audit = dict(
        parent=parent,
        request_id=response.get("request_id"),
        candidates=items,
        complete=not has_more,
        policy="english_then_unlabeled; skip_other_explicit_languages",
    )
    if not has_row:
        # A direct event with no alternate broadcasts keeps its original ID.
        items = [{**parent, "title": parent_title, "language": title_language(parent_title)}]
    elif not items:
        return None, {**audit, "match_status": "uncertain", "reason": "Broadcast row has no choices"}
    preferred = [i for i in items if i["language"] in (None, "en") and not EXCLUDED.search(i["title"])]
    route_unknown = False
    for field in ("channel", "stream_title"):
        required_text = option.get(field)
        if required_text:

            def route_matches(item):
                text = (
                    item["title"]
                    + " "
                    + " ".join(
                        str(v.get("message") or "")
                        for v in item.get("entitlement_messaging", {}).values()
                        if isinstance(v, dict)
                    )
                )
                return bool(norm(required_text)) and norm(required_text) in norm(text)

            matched = [i for i in preferred if route_matches(i)]
            route_unknown = route_unknown or len(matched) < len(preferred)
            preferred = matched
    required = option.get("language")
    if required:
        language = LANGUAGES.get(norm(required))
        preferred = [i for i in preferred if language and i["language"] == language]
    if not preferred:
        uncertain = (
            has_more or route_unknown or (bool(required) and any(i["language"] is None for i in items))
        )
        return None, {
            **audit,
            "match_status": "uncertain" if uncertain else "no_match",
            "reason": "No English or unlabeled broadcast satisfies the permitted route",
        }
    states = ["ready", "waiting_for_feed", "access_unknown", "feeds_locked", "no_matching_feed"]
    chosen = min(
        preferred, key=lambda i: (states.index(tile_state(i)), i["language"] != "en", i["content_id"])
    )
    readiness = tile_state(chosen)
    if has_more and readiness not in {"ready", "waiting_for_feed"}:
        return None, {**audit, "match_status": "uncertain", "reason": "More broadcasts remain uninspected"}
    selected = {**parent, **chosen, "readiness": readiness}
    if has_row:
        selected.update(parent_content_id=parent["content_id"], starts_at=None, ends_at=None)
    return selected, {
        **audit,
        "match_status": "matched",
        "selected": chosen,
        "reason": "Preferred language group; entitlement and live state checked per broadcast",
    }


async def refine(request, results, selected, audit, fetch):
    if not selected or selected["readiness"] != "ready":
        return selected, audit
    response = await fetch(selected["content_id"])
    if (
        response.get("session_id") != results.get("session_id")
        or response.get("content_id") != selected["content_id"]
        or response.get("generation") != results.get("generation")
    ):
        raise ExecutorError("prime_stale_result", "Broadcast response belongs to another event or runtime")
    title = next(
        i["title"]
        for c in results["containers"]
        for i in c["items"]
        if i.get("content_id") == selected["content_id"]
    )
    option = next(o for o in request["allowed_viewing_options"] if o["id"] == selected["viewing_option_id"])
    choice, decision = select(selected, title, response, option)
    return choice, {
        **audit,
        "event_match": selected,
        "broadcast_selection": decision,
        "match_status": decision["match_status"],
        "reason": decision["reason"],
    }
