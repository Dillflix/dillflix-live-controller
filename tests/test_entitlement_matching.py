"""Entitlement uses shared identity matching without repeatedly identifying a feed."""

import copy
import json
from functools import partial
from pathlib import Path

import pytest
from playback_fixtures import payload
from test_prime_matching import GTI, results, tile

from controller.executor.config import ExecutorConfig
from controller.executor.models import ExecutorError
from controller.prime_player.entitlement import choose_entitlement
from controller.prime_player.identity_cache import EventMatchCache
from controller.prime_player.matching import EventMatcher
from controller.prime_player.search import choose_search


class IdentityModel:
    def __init__(self, matching=(GTI,), uncertain=()):
        self.matching, self.uncertain, self.calls = set(matching), set(uncertain), []

    async def close(self):
        pass

    async def completion(self, model, messages, schema, **kwargs):
        context = json.loads(messages[1]["content"])
        self.calls.append(context)
        match = next((i for i in context["candidates"] if i["content_id"] in self.matching), None)
        uncertain = any(i["content_id"] in self.uncertain for i in context["candidates"])
        return json.dumps({
            "match_status": "matched" if match else "uncertain" if uncertain else "no_match",
            "content_id": match["content_id"] if match else None,
            "viewing_option_id": context["allowed_viewing_options"][0]["id"] if match else None,
            "reason": "Scripted event identity decision",
            "evidence": [{"field": "title", "quote": match["title"]}] if match else [],
        })


def labelled(cid=GTI, **changes):
    # A supported fixture identity which needs the existing model rather than
    # deterministic equality with the Teamarr title/aliases.
    return tile("New York (AFC) at Detroit", cid=cid, **changes)


async def scan(responses, model, request=None):
    if request is None:
        request = payload()
        request["content_snapshot"]["event"]["away_team_details"]["short_name"] = "NYJ"
        request["content_snapshot"]["event"]["home_team_details"]["short_name"] = "DET"
    matcher = EventMatcher(ExecutorConfig(), model=model)
    cache = EventMatchCache(matcher)
    calls = []

    async def search(query):
        calls.append(query)
        response = responses[len(calls) - 1]
        if isinstance(response, Exception):
            raise response
        return {"query": query, "session_id": "session", "generation": 1,
                "search_id": str(len(calls)), **response}

    result = await choose_search(request, "UTC", matcher, search, None, lambda _: None,
                                 select=partial(choose_entitlement, cache), exhaust_denials=True)
    return result, calls


async def test_incident_searches_all_team_queries_but_identifies_repeated_feed_once():
    request = payload()
    request["content_snapshot"].update(
        title="Mississippi State Bulldogs at LSU Tigers", competition="college-football",
        event={"away_team_details": {"name": "Bulldogs", "full_name": "Mississippi State Bulldogs",
                                     "short_name": "Mississippi St"},
               "home_team_details": {"name": "Tigers", "full_name": "LSU Tigers", "short_name": "LSU"}},
    )
    feed = tile("#16 Mississippi State vs. #22 LSU", entitlement_status="UNENTITLED", event_state="UPCOMING",
                entitlement_messaging={"message": "Upgrade your TSN subscription for $29.99/month"})
    model = IdentityModel()
    (_, selected, audit), calls = await scan([results(feed)] * 3, model, request)
    assert calls == ["Bulldogs Tigers", "Mississippi State Bulldogs LSU Tigers", "Mississippi St LSU"]
    assert len(model.calls) == 1
    assert selected["entitlement_status"] == "UNENTITLED"
    assert audit["complete"] is False
    assert audit["search_attempts"][0]["state"] == "feeds_locked"
    assert audit["search_queries_exhausted"] is True


async def test_actual_ambiguous_alternative_still_prevents_denial():
    possible = GTI + "-possible"
    model = IdentityModel(uncertain=[possible])
    response = results(labelled(entitlement_status="UNENTITLED"), labelled(possible))
    (_, selected, audit), calls = await scan([response] * 3, model)
    assert selected is None and audit["match_status"] == "uncertain"
    assert len(calls) == 3
    assert len(model.calls) == 2  # Repeated unresolved pool is also reused as uncertain.
    # The confirmed denied feed is identified once, even though the other
    # candidate's identity remains unresolved through the fallback queries.
    assert sum(any(c["content_id"] == GTI for c in call["candidates"]) for call in model.calls) == 1
    assert audit["search_attempts"][1]["audit"]["passes"][-1]["method"] == "reused"


@pytest.mark.parametrize("found_at", [1, 2])
async def test_denied_first_feed_does_not_hide_entitled_feed_in_later_query(found_at):
    entitled = GTI + "-other-provider"
    denied = results(labelled(entitlement_status="UNENTITLED"))
    model = IdentityModel(matching=[GTI, entitled])
    (_, selected, audit), calls = await scan(
        [denied] * found_at + [results(labelled(entitled))], model)
    assert len(calls) == found_at + 1
    assert len(model.calls) == 2
    assert selected["content_id"] == entitled and selected["entitlement_status"] == "ENTITLED"
    assert audit["search_attempts"][0]["state"] == "feeds_locked"


async def test_empty_later_queries_preserve_known_match_until_scan_finishes():
    model = IdentityModel()
    (_, selected, audit), calls = await scan([
        results(labelled(entitlement_status="UNENTITLED")), results(), results(),
    ], model)
    assert len(calls) == 3 and len(model.calls) == 1
    assert selected["entitlement_status"] == "UNENTITLED"
    assert audit["search_queries_exhausted"] is True
    assert [a["state"] for a in audit["search_attempts"]] == ["feeds_locked", "no_matching_feed", "no_matching_feed"]


async def test_later_unknown_feed_prevents_denial_even_after_known_denied_match():
    unknown = GTI + "-unknown"
    model = IdentityModel(matching=[GTI, unknown])
    (_, selected, audit), calls = await scan([
        results(labelled(entitlement_status="UNENTITLED")),
        results(labelled(unknown, entitlement_status=None)), results(),
    ], model)
    assert len(calls) == 3 and selected is None
    assert audit["match_status"] == "uncertain"


async def test_error_after_denied_match_cannot_finish_as_unentitled():
    with pytest.raises(ExecutorError, match="Response lost"):
        await scan([results(labelled(entitlement_status="UNENTITLED")),
                    ExecutorError("prime_transport_unknown", "Response lost")], IdentityModel())


async def test_recorded_incident_evaluates_new_alternatives_without_rematching_tsn_feed():
    fixture = json.loads(Path(__file__).with_name("fixtures").joinpath("entitlement_mississippi_lsu.json").read_text())
    model = IdentityModel(matching=[fixture["matched_content_id"]])
    (_, selected, audit), calls = await scan(fixture["responses"], model, fixture["request"])
    assert calls == fixture["queries"]
    assert len(model.calls) == 3  # Matching feed once, plus two different entitled pools.
    assert sum(any(c["content_id"] == fixture["matched_content_id"] for c in call["candidates"])
               for call in model.calls) == 1
    assert selected["entitlement_status"] == "UNENTITLED"
    assert audit["search_queries_exhausted"] is True


async def test_fallback_reuses_match_and_exclusions_but_inspects_new_entitled_feed():
    unrelated, entitled = GTI + "-unrelated", GTI + "-entitled"
    unknown = labelled(entitlement_status=None)
    wrong = labelled(unrelated)
    model = IdentityModel(matching=[GTI, entitled])
    (_, selected, audit), calls = await scan([
        results(unknown), results(unknown, wrong), results(unknown, wrong, labelled(entitled)),
    ], model)
    assert len(calls) == 3
    assert selected["content_id"] == entitled and selected["entitlement_status"] == "ENTITLED"
    assert [[c["content_id"] for c in call["candidates"]] for call in model.calls] == [[GTI], [unrelated], [entitled]]
    assert audit["search_attempts"][1]["audit"]["passes"][-1]["method"] == "reused"
    assert audit["passes"][0]["reused_content_ids"] == [unrelated]


async def test_entitled_alternative_wins_over_denied_feed_in_same_response():
    entitled = GTI + "-entitled"
    model = IdentityModel(matching=[GTI, entitled])
    (_, selected, _), calls = await scan([
        results(labelled(entitlement_status="UNENTITLED"), labelled(entitled)),
    ], model)
    assert len(calls) == len(model.calls) == 1
    assert selected["content_id"] == entitled


async def test_unknown_entitlement_is_not_converted_to_denial_or_rematched():
    model = IdentityModel()
    (_, selected, audit), calls = await scan([results(labelled(entitlement_status=None))] * 3, model)
    assert len(calls) == 3 and len(model.calls) == 1
    assert selected is None and audit["match_status"] == "uncertain"


async def test_no_match_reuses_exclusion_without_manufacturing_denial():
    model = IdentityModel(matching=[])
    (_, selected, audit), calls = await scan([results(labelled(entitlement_status="UNENTITLED"))] * 3, model)
    assert len(calls) == 3 and len(model.calls) == 1
    assert selected is None and audit["match_status"] == "no_match"


@pytest.mark.parametrize("change", ["target", "route", "timezone", "session", "generation", "network",
                                    "title", "synopsis", "starts_at", "messaging"])
async def test_identity_reuse_is_scoped_to_unchanged_evidence_and_context(change):
    request = payload()
    response = {**results(labelled()), "session_id": "one", "generation": 1, "network_context_id": "one"}
    model = IdentityModel()
    cache = EventMatchCache(EventMatcher(ExecutorConfig(), model=model))
    await choose_entitlement(cache, request, response, "UTC")
    request, response = copy.deepcopy(request), copy.deepcopy(response)
    timezone = "UTC"
    item = response["containers"][0]["items"][0]
    if change == "target":
        request["content_snapshot"]["id"] = "different-event"
    elif change == "route":
        request["allowed_viewing_options"][0]["language"] = "en"
    elif change == "timezone":
        timezone = "America/Vancouver"
    elif change == "session":
        response["session_id"] = "two"
    elif change == "generation":
        response["generation"] = 2
    elif change == "network":
        response["network_context_id"] = "two"
    elif change == "messaging":
        item["entitlement_messaging"] = {"message": "Included with another provider"}
    elif change == "starts_at":
        item["starts_at"] = request["content_snapshot"]["start_time"]
    else:
        item[change] = "Changed identity evidence"
    await choose_entitlement(cache, request, response, timezone)
    assert len(model.calls) == 2


async def test_reused_identity_reads_fresh_access_and_preserves_original_evidence():
    request = payload()
    model = IdentityModel()
    cache = EventMatchCache(EventMatcher(ExecutorConfig(), model=model))
    selected, audit = await choose_entitlement(cache, request, results(labelled(entitlement_status="UNENTITLED")), "UTC")
    selected["evidence"][0]["quote"] = "Do not mutate retained evidence"
    selected, audit = await choose_entitlement(cache, request, results(labelled(event_state="UPCOMING")), "UTC")
    assert len(model.calls) == 1
    assert selected["entitlement_status"] == "ENTITLED" and selected["event_state"] == "UPCOMING"
    assert selected["evidence"][0]["quote"] == labelled()["title"]
    assert audit["passes"][0]["method"] == "reused"


async def test_match_does_not_cache_other_candidates_as_exclusions():
    other = GTI + "-other"
    model = IdentityModel(matching=[GTI, other])
    cache = EventMatchCache(EventMatcher(ExecutorConfig(), model=model))
    request = payload()
    response = results(labelled(), labelled(other))
    await choose_entitlement(cache, request, response, "UTC")
    selected, _ = await choose_entitlement(cache, request, results(labelled(other)), "UTC")
    assert selected["content_id"] == other and len(model.calls) == 2
