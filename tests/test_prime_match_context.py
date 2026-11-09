"""Matcher inputs separate event evidence from executor and scheduling policy."""
import copy
import json

import pytest
from playback_fixtures import payload
from test_prime_matching import GTI, Model, results, tile

from controller.executor.config import ExecutorConfig
from controller.executor.models import ExecutorError
from controller.prime_player.match_context import event_context
from controller.prime_player.matching import EventMatcher


async def test_routes_are_opaque_and_only_explicit_constraints_reach_event_model():
    request = payload()
    original = copy.deepcopy(request)
    request["content_snapshot"].update(provider="espn", source="games", artwork={"app": "prime_video"})
    request["content_snapshot"]["event"]["broadcasts"] = [{"network": "FOX"}]
    options = request["allowed_viewing_options"] = [
        {"id": "route:nfl:prime_video", "app": "prime_video", "basis": "configured_route",
         "decision": "eligible", "reasons": ["user_configured_league_route"]},
        {"id": "route:channel:dazn", "app": "prime_video", "channel": "DAZN", "language": "English"},
        {"id": "other-app", "app": "unrelated"},
    ]
    model = Model({"content_id": GTI, "viewing_option_id": "option_0", "reason": "Same matchup",
                   "evidence": [{"field": "title", "quote": "NYJ @ DET"}]})
    selected, _ = await EventMatcher(ExecutorConfig(), model=model).choose(
        request, results(tile("NYJ @ DET", entitlement_messaging={
            "SLOT": {"message": "DAZN English", "icon": "ENTITLED_ICON", "action": {"app": "prime_video"}},
        })), "UTC")
    context = model.calls[0]
    encoded = json.dumps(context)
    for excluded in ("prime_video", "route:", "configured_route", "eligible", "ENTITLED_ICON", "FOX", "espn"):
        assert excluded not in encoded
    assert context["allowed_viewing_options"] == [
        {"id": "option_0", "channel": "DAZN", "language": "English"}, {"id": "option_1"},
    ]
    assert context["candidates"][0]["entitlement_messaging"] == {"SLOT": {"message": "DAZN English"}}
    assert selected["viewing_option_id"] == "route:channel:dazn"
    assert request["allowed_viewing_options"] == options  # Controller retains all original routing data.
    assert request["content_snapshot"]["viewing_options"] == original["content_snapshot"]["viewing_options"]


async def test_unconstrained_app_route_does_not_expose_subscription_labels():
    model = Model({"content_id": GTI, "viewing_option_id": "prime-option", "reason": "Same event",
                   "evidence": [{"field": "title", "quote": "NYJ @ DET"}]})
    await EventMatcher(ExecutorConfig(), model=model).choose(
        payload(), results(tile("NYJ @ DET", entitlement_messaging={"message": "Included with Prime"})), "UTC")
    assert "entitlement_messaging" not in model.calls[0]["candidates"][0]
    assert model.calls[0]["allowed_viewing_options"] == [{"id": "option_0", "presentation": "live"}]


async def test_exact_matchup_cannot_bypass_explicit_channel_constraint():
    request = payload()
    request["allowed_viewing_options"][0]["channel"] = "TSN"
    model = Model({"content_id": None, "viewing_option_id": None, "reason": "Channel is unknown", "evidence": []})
    selected, _ = await EventMatcher(ExecutorConfig(), model=model).choose(request, results(tile()), "UTC")
    assert selected is None and len(model.calls) == 1


@pytest.mark.parametrize("relation", ["includes_event", "different_event", "uncertain"])
async def test_aggregate_inclusion_cannot_authorize_individual_game(relation):
    request = payload()
    request["content_snapshot"].update(title="Tampa Bay Buccaneers at Chicago Bears", event={
        "away_team_details": {"full_name": "Tampa Bay Buccaneers", "name": "Buccaneers"},
        "home_team_details": {"full_name": "Chicago Bears", "name": "Bears"},
    })
    # Reproduce the incident rationale, including a real quote and a valid supplied ID.
    model = Model({"content_id": GTI, "viewing_option_id": "prime-option", "event_relation": relation,
                   "reason": "RedZone includes the requested game among all NFL games",
                   "evidence": [{"field": "title", "quote": "RedZone - Week 9"}]})
    with pytest.raises(ExecutorError, match="supplied evidence"):
        await EventMatcher(ExecutorConfig(), model=model).choose(
            request, results(tile("RedZone - Week 9", synopsis="NFL Redzone - Week 9"),
                             tile("Buccaneers vs. Bears", cid=GTI + "-tsn"),
                             tile("Buccaneers vs. Bears", cid=GTI + "-dazn")), "UTC")


async def test_actual_redzone_request_can_match_redzone():
    request = payload()
    request["content_snapshot"].update(kind="broadcast", title="RedZone - Week 9", event={})
    selected, _ = await EventMatcher(ExecutorConfig()).choose(request, results(tile("RedZone - Week 9")), "UTC")
    assert selected["event_relation"] == "exact_event"


def test_session_and_competitor_identity_survives_projection():
    target = payload()["content_snapshot"]
    target["event"].update(event_name="French Open", tournament_name="Roland Garros",
                           round_name="Final", draw_type="singles", event_note="Court 1")
    context = event_context(target)
    assert context["event"]["away_team_details"]["full_name"] == "New York Jets"
    assert context["event"]["tournament_name"] == "Roland Garros"
    assert context["event"]["round_name"] == "Final"
    assert context["event"]["event_note"] == "Court 1"
