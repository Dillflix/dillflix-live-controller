"""Exercise real matching across bounded provider-label searches."""

import asyncio
import copy

import pytest
from broadcast_model import FixtureBroadcastModel
from playback_fixtures import payload
from test_prime_matching import GTI, results, tile

from controller.executor.config import ExecutorConfig
from controller.executor.models import ExecutorError
from controller.prime_player.labels import search_queries
from controller.prime_player.matching import EventMatcher
from controller.prime_player.search import choose_search


def college_request():
    request = payload()
    request["content_snapshot"].update(
        title="Hawai'i Rainbow Warriors at Arizona State Sun Devils",
        competition="college-football",
        event={
            "away_team_details": {
                "name": "Rainbow Warriors",
                "full_name": "Hawai'i Rainbow Warriors",
                "short_name": "Hawai'i",
                "abbreviation": "HAW",
            },
            "home_team_details": {
                "name": "Sun Devils",
                "full_name": "Arizona State Sun Devils",
                "short_name": "Arizona St",
                "abbreviation": "ASU",
            },
        },
    )
    return request


COLLEGE_QUERIES = [
    "Rainbow Warriors Sun Devils",
    "Hawai'i Rainbow Warriors Arizona State Sun Devils",
    "Hawai'i Arizona St",
]


def test_existing_query_first_then_only_full_and_short_team_fallbacks():
    assert search_queries(college_request()["content_snapshot"]) == COLLEGE_QUERIES
    assert search_queries(payload()["content_snapshot"]) == [
        "Jets Lions",
        "New York Jets Detroit Lions",
    ]
    assert search_queries({"title": "NFL RedZone"}) == ["NFL RedZone"]


def test_query_deduplication_missing_fields_and_plain_team_names():
    snapshot = {
        "title": "Team A TEAM B",
        "event": {
            "away_team_details": {"name": "Team A", "full_name": "Team A", "short_name": "TEAM A"},
            "home_team": "Team B",
        },
    }
    assert search_queries(snapshot) == ["Team A Team B"]
    assert search_queries({"title": "Event only", "event": {"away_team": "Team A"}}) == ["Event only"]
    assert search_queries({"title": "  "}) == []


async def scan(responses, request=None):
    request = request or college_request()
    calls, history = [], []

    async def search(query):
        calls.append(query)
        value = responses[len(calls) - 1]
        if isinstance(value, Exception):
            raise value
        return {
            "session_id": "session",
            "generation": 1,
            "search_id": str(len(calls)),
            "query": query,
            **value,
        }

    async def fetch(result, cid):
        return {
            "session_id": result["session_id"],
            "generation": result["generation"],
            "content_id": cid,
            "complete": True,
            "coverage": "live_details_btf_response",
            "resource": {"containerList": []},
        }

    matcher = EventMatcher(ExecutorConfig(), model=FixtureBroadcastModel())
    result = await choose_search(
        request,
        "America/Vancouver",
        matcher,
        search,
        fetch,
        lambda audit: history.append(copy.deepcopy(audit)),
    )
    return result, calls, history


def matched(**changes):
    return results(tile("Hawai'i Rainbow Warriors at Arizona State Sun Devils", **changes))


def irrelevant():
    # The incident's mascot query returned non-event titles of this kind.
    return results(tile("Rainbow: In Performance", cid=GTI + "-unrelated", content_type="MOVIE"))


@pytest.mark.parametrize("found_at", [0, 1, 2])
async def test_fallback_finds_event_and_stops_at_first_usable_match(found_at):
    (result, selected, audit), calls, history = await scan([irrelevant()] * found_at + [matched()])
    assert calls == COLLEGE_QUERIES[: found_at + 1]
    assert selected["content_id"] == GTI and selected["readiness"] == "ready"
    assert result["query"] == calls[-1]
    assert [a["query"] for a in audit["search_attempts"]] == calls
    assert [a["state"] for a in audit["search_attempts"]] == ["no_matching_feed"] * found_at + ["ready"]
    assert history[0]["search_attempts"][0]["state"] == "pending"
    if found_at:
        assert audit["search_attempts"][0]["audit"]["rejected"][0]["reason"] == "not_event"


async def test_no_match_requires_exhausting_distinct_fallbacks():
    (_, selected, audit), calls, _ = await scan([irrelevant()] * 3)
    assert selected is None and audit["match_status"] == "no_match"
    assert calls == COLLEGE_QUERIES
    assert len(audit["search_attempts"]) == 3
    assert all(a["complete"] is False for a in audit["search_attempts"])


async def test_unavailable_trial_does_not_hide_playable_feed_in_later_query():
    blocked = matched(availability_status="UNAVAILABLE")
    playable = matched(cid=GTI + "-alternative")
    (result, selected, audit), calls, _ = await scan([blocked, playable])
    assert calls == COLLEGE_QUERIES[:2]
    assert selected["content_id"] == GTI + "-alternative"
    assert not selected["unavailable_trial"]
    assert result["query"] == COLLEGE_QUERIES[1]
    assert audit["search_attempts"][0]["audit"]["broadcast_selection"]["selected"]["unavailable_trial"]


async def test_unavailable_trial_retains_its_source_after_other_queries_find_nothing():
    (result, selected, audit), calls, history = await scan([
        matched(availability_status="UNAVAILABLE"), irrelevant(), irrelevant(),
    ])
    assert calls == COLLEGE_QUERIES
    assert selected["unavailable_trial"]
    assert result["query"] == COLLEGE_QUERIES[0]
    assert result["search_id"] == "1"
    assert audit["search_queries_exhausted"] is True
    assert audit["broadcast_selection"]["selected"]["content_id"] == selected["content_id"]
    assert history[-1] == audit


async def test_later_uncertainty_does_not_authorize_retained_unavailable_trial():
    (_, selected, audit), calls, _ = await scan([
        matched(availability_status="UNAVAILABLE"),
        matched(cid=GTI + "-alternative", entitlement_status=None), irrelevant(),
    ])
    assert calls == COLLEGE_QUERIES
    assert selected is None and audit["match_status"] == "uncertain"


@pytest.mark.parametrize("change", [
    {"entitlement_status": "UNENTITLED"}, {"event_state": "UPCOMING"}, {"event_state": "ENDED"},
])
async def test_newer_evidence_revokes_trial_for_same_listing(change):
    (_, selected, _), _, _ = await scan([
        matched(availability_status="UNAVAILABLE"), matched(**change), irrelevant(),
    ])
    assert not selected or not selected.get("unavailable_trial")


async def test_trial_does_not_mask_transport_failure_in_later_query():
    with pytest.raises(ExecutorError, match="Response lost"):
        await scan([matched(availability_status="UNAVAILABLE"),
                    ExecutorError("prime_transport_unknown", "Response lost")])


@pytest.mark.parametrize("final", [irrelevant(), matched(entitlement_status="UNENTITLED")])
async def test_earlier_uncertainty_cannot_become_durable_exclusion(final):
    (_, selected, audit), _, _ = await scan([matched(entitlement_status=None)] + [final] * 3)
    assert selected is None and audit["match_status"] == "uncertain"
    assert audit["search_attempts"][0]["state"] == "access_unknown"


@pytest.mark.parametrize(
    "change,state",
    [
        ({"event_state": "UPCOMING"}, "waiting_for_feed"),
        ({"entitlement_status": "UNENTITLED"}, "feeds_locked"),
        ({"event_state": "UNAVAILABLE"}, "feeds_unavailable"),
    ],
)
async def test_positive_identity_with_access_outcome_does_not_repeat_queries(change, state):
    (_, selected, _), calls, _ = await scan([matched(**change)])
    assert selected["readiness"] == state
    assert calls == COLLEGE_QUERIES[:1]


@pytest.mark.parametrize("change", [{"generation": 2}, {"session_id": "new"}, {"query": "different"}])
async def test_fallback_results_cannot_cross_runtime_or_query_identity(change):
    with pytest.raises(ExecutorError, match="session, generation or query changed"):
        await scan([irrelevant(), {**matched(), **change}])


async def test_transport_error_stops_instead_of_becoming_no_match():
    with pytest.raises(ExecutorError, match="Response lost"):
        await scan([irrelevant(), ExecutorError("prime_transport_unknown", "Response lost")])


async def test_overall_deadline_does_not_turn_partial_scan_into_no_match():
    calls, history = [], []

    async def search(query):
        calls.append(query)
        if len(calls) > 1:
            await asyncio.sleep(10)
        return {"query": query, "session_id": "session", "generation": 1, **irrelevant()}

    async def fetch(result, cid):
        pytest.fail("No event should reach broadcast inspection")

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.02):
            await choose_search(
                college_request(),
                "America/Vancouver",
                EventMatcher(ExecutorConfig()),
                search,
                fetch,
                lambda audit: history.append(copy.deepcopy(audit)),
            )
    assert calls == COLLEGE_QUERIES[:2]
    assert history[-1]["match_status"] == "uncertain"
    assert history[-1]["search_attempts"][-1]["state"] == "pending"
