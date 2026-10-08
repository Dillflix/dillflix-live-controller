import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from broadcast_model import FixtureBroadcastModel, answer

from controller.executor.models import ExecutorError
from controller.prime_player.broadcast_matching import PROMPT
from controller.prime_player.broadcasts import choose_broadcast, parse, refine, select

CAPTURE = json.loads(Path(__file__).with_name("fixtures").joinpath("prime_broadcasts.json").read_text())
AVALANCHE = json.loads(
    Path(__file__).with_name("fixtures").joinpath("prime_broadcasts_avalanche.json").read_text()
)
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


class Model:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def completion(self, model, messages, schema, **kwargs):
        self.calls.append(json.loads(messages[1]["content"]))
        assert messages[0]["content"] == PROMPT
        assert kwargs["name"] == "broadcast_selection"
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, str) else json.dumps(value)


def matcher(model):
    return SimpleNamespace(model=model, config=SimpleNamespace(prime_match_model="fixture-model"))


async def choose(response=None, option=None, model=None, title="Lions vs. Panthers"):
    return await select(
        parent(),
        {"title": title, "synopsis": "Fixture synopsis"},
        CAPTURE if response is None else response,
        option or {"id": "prime"},
        target={"title": "Fixture event"},
        matcher=matcher(model or FixtureBroadcastModel()),
    )


def response(*indexes):
    value = copy.deepcopy(CAPTURE)
    row = value["resource"]["containerList"][0]
    row["items"] = [row["items"][i] for i in indexes]
    return value


async def test_captured_nhl_variants_are_sent_to_model_and_english_id_selected():
    items, more, row = parse(AVALANCHE)
    model = Model(answer(items[0], "en"))
    selected, audit = await choose(AVALANCHE, model=model)
    assert row and not more
    assert selected["content_id"] == "amzn1.dv.gti.cef1b941-b519-4013-9d2b-be5cd8bdb009"
    assert selected["readiness"] == "ready" and selected["language"] == "en"
    assert audit["method"] == "llm"
    assert [i["title"] for i in model.calls[0]["candidates"]] == ["English Broadcast", "French Broadcast"]
    assert all(i["synopsis"] for i in model.calls[0]["candidates"])
    assert model.calls[0]["permitted_viewing_option"] == {"id": "prime"}
    assert model.calls[0]["parent"]["content_id"] == PARENT
    assert "BEARD_SUPPORTED_CAROUSEL" not in json.dumps(model.calls)


async def test_existing_nfl_capture_still_selects_unlabeled_entitled_child():
    selected, audit = await choose()
    assert selected["content_id"] == ENGLISH and selected["parent_content_id"] == PARENT
    assert selected["language"] is None and selected["readiness"] == "ready"
    assert audit["candidates"][0]["content_id"] == FRENCH


async def test_single_french_broadcast_is_evaluated_and_excluded_by_model():
    value = copy.deepcopy(AVALANCHE)
    value["resource"]["containerList"][0]["items"] = value["resource"]["containerList"][0]["items"][1:]
    model = Model(answer())
    selected, audit = await choose(value, model=model)
    assert selected is None and audit["match_status"] == "no_match"
    assert len(model.calls) == 1
    assert model.calls[0]["candidates"][0]["title"] == "French Broadcast"


@pytest.mark.parametrize("presentation", ["BEARD_SUPPORTED_CAROUSEL", "FUTURE_LAYOUT", None])
def test_broadcast_contents_are_independent_of_presentation(presentation):
    value = copy.deepcopy(CAPTURE)
    row = value["resource"]["containerList"][0]
    if presentation is None:
        row.pop("type")
    else:
        row["type"] = presentation
    assert parse(value) == parse(CAPTURE)
    row["items"][0]["gti"] = "invalid"
    with pytest.raises(ExecutorError, match="Unrecognized broadcast item"):
        parse(value)


@pytest.mark.parametrize(
    "change", ["incomplete", "duplicate", "renamed", "bad_id", "malformed", "duplicate_row"]
)
def test_invalid_or_incomplete_broadcast_data_is_rejected(change):
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
    if change == "duplicate_row":
        value["resource"]["containerList"].append(copy.deepcopy(row))
    with pytest.raises(ExecutorError):
        parse(value)


@pytest.mark.parametrize(
    "state,expected",
    [("UPCOMING", "waiting_for_feed"), ("ENDED", "no_matching_feed"), (None, "access_unknown")],
)
async def test_parent_live_state_does_not_override_child(state, expected):
    value = response(1)
    value["resource"]["containerList"][0]["items"][0]["liveliness"] = state
    selected, _ = await choose(value)
    assert selected["readiness"] == expected


@pytest.mark.parametrize("messages", [{}, {"a": {"icon": "ENTITLED_ICON"}, "b": {"icon": "OFFER_ICON"}}])
async def test_missing_or_conflicting_child_entitlement_does_not_inherit_parent(messages):
    value = response(1)
    value["resource"]["containerList"][0]["items"][0]["entitlementMessaging"] = messages
    assert (await choose(value))[0]["readiness"] == "access_unknown"


async def test_model_cannot_choose_locked_english_from_live_entitled_group():
    value = response(0, 1)
    value["resource"]["containerList"][0]["items"][1]["entitlementMessaging"] = {"a": {"icon": "OFFER_ICON"}}
    items, _, _ = parse(value)
    model = Model(answer(items[1], "en"))
    with pytest.raises(ExecutorError, match="outside the supplied"):
        await choose(value, model=model)
    assert model.calls[0]["selectable_content_ids"] == [FRENCH]


async def test_upcoming_english_does_not_hide_live_unlabeled():
    value = response(0, 1)
    value["resource"]["containerList"][0]["items"][0].update(title="English Broadcast", liveliness="UPCOMING")
    model = Model(answer(parse(value)[0][1]))
    selected, _ = await choose(value, model=model)
    assert selected["content_id"] == ENGLISH and selected["readiness"] == "ready"
    assert model.calls[0]["selectable_content_ids"] == [ENGLISH]


async def test_incomplete_broadcast_list_cannot_establish_no_match():
    value = response(0)
    value["resource"]["containerList"][0]["paginationLink"] = {"next": True}
    assert (await choose(value, model=Model(answer())))[1]["match_status"] == "uncertain"
    value = response(1)
    value["resource"]["containerList"][0]["paginationLink"] = {"next": True}
    assert (await choose(value))[0]["readiness"] == "ready"


async def test_ambiguous_ready_candidate_cannot_establish_all_alternatives_locked():
    value = response(0, 2)
    items = parse(value)[0]
    model = Model(answer(status="uncertain"), answer(items[1]))
    selected, audit = await choose(value, model=model)
    assert selected is None and audit["match_status"] == "uncertain"


async def test_direct_event_without_broadcast_row_still_uses_model_and_preserves_id():
    value = copy.deepcopy(CAPTURE)
    value["resource"]["containerList"] = []
    item = {"content_id": PARENT, "title": "French Open"}
    model = Model(answer(item))
    selected, _ = await choose(value, model=model, title=item["title"])
    assert selected["content_id"] == PARENT and "parent_content_id" not in selected
    assert model.calls[0]["direct_event"] is True
    assert model.calls[0]["candidates"][0]["synopsis"] == "Fixture synopsis"


async def test_provider_language_constraints_are_supplied_to_model():
    model = Model(answer(status="uncertain"))
    option = {
        "id": "prime",
        "channel": "Sportsnet",
        "language": "English",
        "stream_title": "National coverage",
    }
    selected, audit = await choose(response(1), option=option, model=model)
    assert selected is None and audit["match_status"] == "uncertain"
    assert model.calls[0]["permitted_viewing_option"] == option


@pytest.mark.parametrize(
    "bad",
    [
        "unknown_id",
        "invented_quote",
        "missing_quote",
        "other_language",
        "unlabeled_required",
        "abstention_id",
        "invalid_json",
    ],
)
async def test_invalid_model_answers_cannot_authorize_playback(bad):
    item = parse(response(1))[0][0]
    choice = answer(item)
    option = None
    if bad == "unknown_id":
        choice["content_id"] = "amzn1.dv.gti.fabricated"
    if bad == "invented_quote":
        choice["evidence"][0]["quote"] = "English Broadcast"
    if bad == "missing_quote":
        choice["evidence"] = []
    if bad == "other_language":
        choice["language"] = "fr"
    if bad == "unlabeled_required":
        option = {"id": "prime", "language": "English"}
    if bad == "abstention_id":
        choice["match_status"] = "no_match"
    if bad == "invalid_json":
        choice = "not JSON"
    with pytest.raises(ExecutorError) as error:
        await choose(response(1), option=option, model=Model(choice))
    assert error.value.code == "prime_broadcast_selection_invalid"


async def test_model_failure_does_not_fall_back_to_regex_or_parent():
    with pytest.raises(ExecutorError) as error:
        await choose(response(1), model=Model(TimeoutError("model timed out")))
    assert error.value.code == "prime_broadcast_selection_failed"
    with pytest.raises(ExecutorError) as error:
        await select(
            parent(),
            {"title": "English Broadcast"},
            response(1),
            {"id": "prime"},
            target={},
            matcher=matcher(None),
        )
    assert error.value.code == "prime_broadcast_selection_unavailable"


@pytest.mark.parametrize(
    "field,value", [("session_id", "different"), ("generation", 9), ("content_id", FRENCH)]
)
async def test_foreign_response_never_reaches_model(field, value):
    data = copy.deepcopy(CAPTURE)
    data[field] = value

    async def fetch(content_id):
        return data

    with pytest.raises(ExecutorError, match="another event or runtime"):
        await refine({}, {"session_id": CAPTURE["session_id"], "generation": 1}, parent(), {}, fetch)


async def test_unmatched_and_upcoming_parents_do_not_fetch_broadcasts():
    async def fetch(content_id):
        raise AssertionError("unnecessary request")

    for selected in (None, {**parent(), "readiness": "waiting_for_feed"}):
        assert await refine({}, {}, selected, {}, fetch) == (selected, {})


async def test_parse_failure_survives_final_unmatched_pass():
    class Matcher:
        async def choose(self, request, results, timezone):
            return (
                (parent(), {"match_status": "matched"})
                if results["containers"][0]["items"]
                else (None, {"match_status": "no_match"})
            )

    async def fetch(content_id):
        value = response(0)
        value["resource"]["containerList"][0]["items"] = None
        return value

    selected, audit = await choose_broadcast(
        {"allowed_viewing_options": [{"id": "prime"}], "content_snapshot": {}},
        {
            "session_id": CAPTURE["session_id"],
            "generation": 1,
            "containers": [{"items": [{"content_id": PARENT, "title": "Lions vs. Panthers"}]}],
        },
        "America/Vancouver",
        Matcher(),
        fetch,
        lambda audit: None,
    )
    assert selected is None and audit["match_status"] == "uncertain"
    assert audit["error"]["code"] == "prime_invalid_broadcasts"
    assert audit["parent_selections"][0]["error"] == audit["error"]


async def test_unavailable_child_does_not_hide_available_alternative():
    from test_prime_matching import UNAVAILABLE_BADGE

    value = response(0, 1)
    raw = value["resource"]["containerList"][0]["items"][0]
    raw["title"] = "English Broadcast"
    raw["entitlementMessaging"].update(UNAVAILABLE_BADGE)
    model = Model(answer(parse(value)[0][1]))
    selected, _ = await choose(value, model=model)
    assert selected["content_id"] == ENGLISH and selected["readiness"] == "ready"
    blocked = model.calls[0]["candidates"][0]
    assert blocked["entitlement_status"] == "ENTITLED" and blocked["event_state"] == "LIVE"
    assert blocked["availability_status"] == "UNAVAILABLE"
    assert model.calls[0]["selectable_content_ids"] == [ENGLISH]
    with pytest.raises(ExecutorError, match="outside the supplied"):
        await choose(value, model=Model(answer(parse(value)[0][0], "en")))


@pytest.mark.parametrize("has_more", [False, True])
async def test_all_suitable_broadcasts_unavailable_preserves_completeness(has_more):
    from test_prime_matching import UNAVAILABLE_BADGE

    value = response(1)
    container = value["resource"]["containerList"][0]
    container["items"][0]["entitlementMessaging"].update(UNAVAILABLE_BADGE)
    if has_more:
        container["paginationLink"] = {"next": True}
    selected, audit = await choose(value)
    if has_more:
        assert selected is None and audit["match_status"] == "uncertain"
    else:
        assert selected["readiness"] == "feeds_unavailable"


async def test_unavailable_parent_still_inspects_independently_available_child():
    from test_prime_matching import UNAVAILABLE_BADGE

    blocked = {**parent(), "availability_status": "UNAVAILABLE", "readiness": "feeds_unavailable"}
    catalogue = {
        "session_id": CAPTURE["session_id"], "generation": CAPTURE["generation"],
        "containers": [{"items": [{"content_id": PARENT, "title": "Lions vs. Panthers",
                                  "entitlement_messaging": UNAVAILABLE_BADGE}]}]
    }
    fetched = []

    async def fetch(cid):
        fetched.append(cid)
        return CAPTURE

    selected, _ = await refine(
        {"allowed_viewing_options": [{"id": "prime"}], "content_snapshot": {"title": "Fixture event"}},
        catalogue, blocked, {}, fetch, matcher(FixtureBroadcastModel())
    )
    assert fetched == [PARENT]
    assert selected["content_id"] == ENGLISH and selected["readiness"] == "ready"
    assert selected["availability_status"] is None
