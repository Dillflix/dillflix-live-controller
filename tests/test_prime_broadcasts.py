import copy
import json
from pathlib import Path

import pytest

from controller.executor.models import ExecutorError
from controller.prime_player.broadcasts import parse, refine, select, title_language
from controller.prime_player.client import PrimePlayerClient

CAPTURE = json.loads(Path(__file__).with_name("fixtures").joinpath("prime_broadcasts.json").read_text())
PARENT = CAPTURE["content_id"]
ENGLISH = "amzn1.dv.gti.a0f3772b-99fe-4b62-a454-a37d04a0b359"
FRENCH = "amzn1.dv.gti.87219723-d95a-4ff8-b30f-c9d9782badcd"


def parent():
    return dict(
        content_id=PARENT,
        readiness="ready",
        viewing_option_id="prime",
        event_state="LIVE",
        entitlement_status="ENTITLED",
        starts_at=None,
        ends_at=None,
    )


def choose(response=None, option=None, title="Lions vs. Panthers"):
    return select(parent(), title, CAPTURE if response is None else response, option or {"id": "prime"})


def response(*indexes):
    value = copy.deepcopy(CAPTURE)
    row = value["resource"]["containerList"][0]
    row["items"] = [row["items"][i] for i in indexes]
    return value


def test_real_capture_selects_unlabeled_entitled_feed_and_preserves_unknown_language():
    items, has_more, has_row = parse(CAPTURE)
    assert has_row and not has_more and len(items) == 8
    assert items[0]["language"] == "fr" and items[1]["language"] is None
    assert all(i["entitlement_status"] == "UNENTITLED" for i in items[2:])
    selected, audit = choose()
    assert selected["content_id"] == ENGLISH and selected["parent_content_id"] == PARENT
    assert selected["viewing_option_id"] == "prime" and selected["readiness"] == "ready"
    assert selected["language"] is None
    assert audit["parent"]["content_id"] == PARENT
    assert audit["candidates"][0]["content_id"] == FRENCH


def test_only_french_is_skipped_without_calling_it_locked():
    selected, audit = choose(response(0))
    assert selected is None and audit["match_status"] == "no_match"
    assert audit["candidates"][0]["entitlement_status"] == "ENTITLED"


def test_explicit_english_precedes_unlabeled_but_not_entitlement():
    value = response(0, 1)
    french = value["resource"]["containerList"][0]["items"][0]
    french["title"] = "Lions @ Panthers (In English)"
    assert choose(value)[0]["content_id"] == FRENCH
    french["entitlementMessaging"] = {"ENTITLEMENT_MESSAGE_SLOT": {"icon": "OFFER_ICON"}}
    assert choose(value)[0]["content_id"] == ENGLISH


@pytest.mark.parametrize(
    "title,expected",
    [
        ("French Open", None),
        ("Lions (In French)", "fr"),
        ("Lions (en français)", "fr"),
        ("Lions (In Spanish)", "es"),
        ("Lions (In English)", "en"),
    ],
)
def test_only_explicit_title_qualifiers_establish_language(title, expected):
    assert title_language(title) == expected


@pytest.mark.parametrize(
    "state,expected",
    [("UPCOMING", "waiting_for_feed"), ("ENDED", "no_matching_feed"), (None, "access_unknown")],
)
def test_parent_live_state_does_not_override_selected_broadcast(state, expected):
    value = response(0, 1)
    value["resource"]["containerList"][0]["items"][1]["liveliness"] = state
    assert choose(value)[0]["readiness"] == expected


@pytest.mark.parametrize("messages", [{}, {"a": {"icon": "ENTITLED_ICON"}, "b": {"icon": "OFFER_ICON"}}])
def test_missing_or_conflicting_entitlement_never_inherits_parent_access(messages):
    value = response(1)
    value["resource"]["containerList"][0]["items"][0]["entitlementMessaging"] = messages
    assert choose(value)[0]["readiness"] == "access_unknown"


def test_incomplete_language_absence_remains_unknown_but_known_good_feed_can_play():
    value = response(0)
    value["resource"]["containerList"][0]["paginationLink"] = {"next": True}
    assert choose(value)[1]["match_status"] == "uncertain"
    value = response(0, 1)
    value["resource"]["containerList"][0]["paginationLink"] = {"next": True}
    assert choose(value)[0]["content_id"] == ENGLISH


def test_explicit_route_language_is_not_satisfied_by_unknown_language():
    selected, audit = choose(option={"id": "prime", "language": "English"})
    assert selected is None and audit["match_status"] == "uncertain"


def test_broadcast_switch_does_not_bypass_channel_constraint():
    assert choose(option={"id": "prime", "channel": "DAZN"})[0]["content_id"] == ENGLISH
    selected, audit = choose(option={"id": "prime", "channel": "Sportsnet"})
    assert selected is None and audit["match_status"] == "uncertain"


def test_direct_event_without_alternatives_keeps_original_id_but_checks_language():
    value = copy.deepcopy(CAPTURE)
    value["resource"]["containerList"] = []
    assert choose(value)[0]["content_id"] == PARENT
    assert choose(value, title="Lions (In French)")[0] is None


@pytest.mark.parametrize("change", ["incomplete", "duplicate", "renamed", "bad_id", "malformed"])
def test_unrecognized_or_incomplete_broadcast_data_cannot_fall_back_to_parent(change):
    value = response(0, 1)
    row = value["resource"]["containerList"][0]
    if change == "incomplete":
        value["complete"] = False
    if change == "duplicate":
        row["items"].append(copy.deepcopy(row["items"][0]))
    if change == "renamed":
        row["title"] = "Unrecognized"
    if change == "bad_id":
        row["items"][0]["gti"] = "arbitrary"
    if change == "malformed":
        row["items"] = None
    with pytest.raises(ExecutorError):
        choose(value)


@pytest.mark.parametrize(
    "field,value", [("session_id", "different"), ("generation", 9), ("content_id", FRENCH)]
)
async def test_late_foreign_responses_never_select_a_broadcast(field, value):
    data = copy.deepcopy(CAPTURE)
    data[field] = value

    async def fetch(content_id):
        return data

    with pytest.raises(ExecutorError, match="another event or runtime"):
        await refine({}, {"session_id": CAPTURE["session_id"], "generation": 1}, parent(), {}, fetch)


async def test_unmatched_and_upcoming_parents_never_issue_broadcast_request():
    async def fetch(content_id):
        raise AssertionError("unnecessary broadcast request")

    for selected in (None, {**parent(), "readiness": "waiting_for_feed"}):
        assert await refine({}, {}, selected, {}, fetch) == (selected, {})


def test_broadcast_capability_requires_implementation_and_runtime_availability():
    health = {
        "api_version": 11,
        "capabilities": ["broadcasts"],
        "compatibility": {"capabilities": {"broadcasts": {"available": True}}},
    }
    PrimePlayerClient.require(health, "broadcasts")
    health["compatibility"]["capabilities"]["broadcasts"]["available"] = False
    with pytest.raises(ExecutorError, match="broadcasts"):
        PrimePlayerClient.require(health, "broadcasts")
    health["compatibility"]["capabilities"]["broadcasts"]["available"] = True
    health["capabilities"] = []
    with pytest.raises(ExecutorError, match="broadcasts"):
        PrimePlayerClient.require(health, "broadcasts")
