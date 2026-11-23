from copy import deepcopy
from types import SimpleNamespace

import pytest

from controller.prime_player.matching import EventMatcher, candidates
from controller.prime_player.match_context import event_context
from controller.prime_player.entitlement import choose_entitlement


SNAPSHOT = {
    "kind": "broadcast", "source": "sportsnet_wpt", "competition": "wpt",
    "title": "World Poker Tour", "start_time": "2026-11-23T20:00:00Z",
    "expected_end_time": "2026-11-23T21:00:00Z",
    "broadcast": {"coverage_type": "linear_program"},
}
ITEM = {
    "content_id": "amzn1.dv.gti.wpt-airing", "title": "World Poker Tour",
    "content_type": "EVENT", "starts_at": SNAPSHOT["start_time"],
    "ends_at": SNAPSHOT["expected_end_time"], "event_state": "LIVE",
    "entitlement_status": "ENTITLED",
}


def test_exact_airing_retains_live_and_entitlement_policy():
    items, rejected = candidates({"containers": [{"items": [ITEM]}]}, SNAPSHOT, "UTC")
    assert not rejected and items[0]["event_state"] == "LIVE"
    assert event_context(SNAPSHOT)["coverage_type"] == "linear_program"


@pytest.mark.parametrize("change,reason", [
    ({"starts_at": "2026-11-23T21:00:00Z", "ends_at": "2026-11-23T22:00:00Z"},
     "conflicting_airing_window"),
    ({"starts_at": None}, "missing_airing_window"),
    ({"ends_at": None}, "missing_airing_window"),
    ({"content_type": "EPISODE"}, "not_event"),
    ({"content_type": "CHANNEL"}, "not_event"),
    ({"title": "World Poker Tour Replay"}, "non_live_variant"),
])
def test_no_same_title_episode_or_generic_channel_substitution(change, reason):
    item = {**deepcopy(ITEM), **change}
    items, rejected = candidates({"containers": [{"items": [item]}]}, SNAPSHOT, "UTC")
    assert not items and rejected[0]["reason"] == reason


async def test_missing_airing_window_is_uncertain_not_conclusive_absence():
    matcher = EventMatcher(SimpleNamespace(prime_match_model=None))
    selected, audit = await matcher.choose(
        {"content_snapshot": SNAPSHOT},
        {"containers": [{"items": [{**ITEM, "starts_at": None}]}]}, "UTC",
    )
    assert selected is None and audit["match_status"] == "uncertain"
    selected, audit = await choose_entitlement(
        matcher,
        {"content_snapshot": SNAPSHOT,
         "allowed_viewing_options": [{"id": "sportsnet", "app": "prime_video"}]},
        {"containers": [{"items": [{**ITEM, "starts_at": None}]}]}, "UTC",
    )
    assert selected is None and audit["match_status"] == "uncertain"
