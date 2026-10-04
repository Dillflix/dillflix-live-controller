import json
from dataclasses import replace

import pytest
from playback_fixtures import payload

from controller.executor.config import ExecutorConfig
from controller.executor.models import ExecutorError
from controller.prime_player.matching import EventMatcher

GTI = "amzn1.dv.gti.live-event"


def tile(title="Jets vs. Lions", cid=GTI, **changes):
    return {
        "title": title,
        "content_id": cid,
        "content_type": "EVENT",
        "entitlement_status": "ENTITLED",
        "event_state": "LIVE",
        "entitlement_messaging": {},
        "starts_at": None,
        "ends_at": None,
        "synopsis": None,
        "artwork": {},
        "action": {"target": "detail"},
        "actions": [],
        "metadata": {},
        **changes,
    }


def results(*items):
    return {
        "containers": [{"items": list(items), "has_more": True}],
        "complete": False,
        "coverage": "find_initial_response",
        "warnings": [],
    }


class Model:
    def __init__(self, value):
        self.value, self.calls = value, []

    async def completion(self, model, messages, schema, **kwargs):
        self.calls.append(json.loads(messages[1]["content"]))
        return json.dumps(self.value)

    async def close(self):
        pass


@pytest.mark.parametrize("title", ["Jets vs. Lions", "Lions vs Jets", "New York Jets at Detroit Lions"])
async def test_deterministic_match_needs_only_normal_matchup_label(title):
    matcher = EventMatcher(ExecutorConfig())
    choice, audit = await matcher.choose(payload(), results(tile(title)), "America/Vancouver")
    assert choice["content_id"] == GTI
    assert audit["method"] == "deterministic"


@pytest.mark.parametrize(
    "changes",
    [
        {"content_type": "MOVIE"},
        {"content_type": None},
        {"content_id": "amzn1.dv.icid.collection"},
        {"content_id": None},
        {"title": None},
        {"title": ""},
        {"title": "Jets vs Lions Rapid Recap"},
        {"title": "Jets vs Lions Highlights"},
        {"starts_at": "2000-01-01T00:00:00Z"},
    ],
)
async def test_hard_exclusions_cannot_reach_model(changes):
    model = Model({})
    matcher = EventMatcher(ExecutorConfig(), model=model)
    choice, audit = await matcher.choose(payload(), results(tile(**changes)), "America/Vancouver")
    assert choice is None
    assert not model.calls
    assert audit["rejected"]


async def test_single_live_wrong_opponent_is_not_automatically_chosen():
    choice, _ = await EventMatcher(ExecutorConfig()).choose(
        payload(), results(tile("Jets vs Ravens")), "America/Vancouver"
    )
    assert choice is None


async def test_duplicate_content_does_not_create_false_ambiguity():
    choice, audit = await EventMatcher(ExecutorConfig()).choose(
        payload(), results(tile(), tile(metadata={"occurrence": "another-row"})), "America/Vancouver"
    )
    assert choice["content_id"] == GTI
    assert len(audit["candidates"]) == 1


async def test_llm_resolves_labels_without_structured_candidate_metadata():
    model = Model(
        {
            "content_id": GTI,
            "viewing_option_id": "prime-option",
            "reason": "NY Jets and Detroit identify the requested opponents",
            "evidence": [{"field": "title", "quote": "NY Jets @ Detroit"}],
        }
    )
    config = replace(ExecutorConfig(), prime_match_model="text-model")
    choice, audit = await EventMatcher(config, model=model).choose(
        payload(),
        results(tile("NY Jets @ Detroit"), tile("NY Jets @ Baltimore", cid=GTI + "-other")),
        "America/Vancouver",
    )
    assert choice["content_id"] == GTI
    assert audit["method"] == "llm"
    assert model.calls[0]["target"]["event"]["home_team_details"]["full_name"] == "Detroit Lions"
    assert "teams" not in model.calls[0]["candidates"][0]


@pytest.mark.parametrize(
    "changes",
    [
        {"content_id": GTI + "-invented"},
        {"viewing_option_id": "invented-route"},
        {"evidence": [{"field": "title", "quote": "invented label"}]},
        {"evidence": []},
        {"content_id": None},
    ],
)
async def test_llm_must_select_supplied_live_candidate_and_evidence(changes):
    answer = {
        "content_id": GTI,
        "viewing_option_id": "prime-option",
        "reason": "Match",
        "evidence": [{"field": "title", "quote": "NYJ @ DET"}],
        **changes,
    }
    with pytest.raises(ExecutorError, match="supplied evidence"):
        await EventMatcher(ExecutorConfig(), model=Model(answer)).choose(
            payload(), results(tile("NYJ @ DET")), "America/Vancouver"
        )


async def test_identical_labels_different_ids_require_model_or_abstention():
    choices = results(tile(), tile(cid=GTI + "-different"))
    choice, audit = await EventMatcher(ExecutorConfig()).choose(payload(), choices, "America/Vancouver")
    assert choice is None and audit["method"] == "abstain"
    model = Model(
        {
            "content_id": None,
            "viewing_option_id": None,
            "reason": "Both labels are equally plausible",
            "evidence": [],
        }
    )
    choice, audit = await EventMatcher(ExecutorConfig(), model=model).choose(
        payload(), choices, "America/Vancouver"
    )
    assert choice is None and audit["method"] == "llm"


async def test_rich_catalogue_fields_and_field_attributed_synopsis_evidence():
    description = "New York Jets visit the Detroit Lions with live pregame coverage."
    model = Model(
        {
            "content_id": GTI,
            "viewing_option_id": "prime-option",
            "reason": "Synopsis identifies both opponents",
            "evidence": [{"field": "synopsis", "quote": description}],
        }
    )
    choice, audit = await EventMatcher(ExecutorConfig(), model=model).choose(
        payload(),
        results(
            tile(
                "Sunday football",
                synopsis=description,
                entitlement_messaging={"message": "Included with your DAZN subscription"},
            )
        ),
        "America/Vancouver",
    )
    assert choice["content_id"] == GTI
    candidate = model.calls[0]["candidates"][0]
    assert candidate["synopsis"] == description
    assert candidate["titles"] == ["Sunday football"]
    assert candidate["entitlement_messaging"]["message"].endswith("DAZN subscription")
    assert "labels" not in candidate and "handle" not in candidate and "action" not in candidate
    assert audit["evidence"] == [{"field": "synopsis", "quote": description}]


@pytest.mark.parametrize(
    "evidence",
    [
        [{"field": "synopsis", "quote": "invented competitors"}],
        [{"field": "synopsis", "quote": "NYJ @ DET"}],  # Real text, wrong source field.
        [{"field": "event_state", "quote": "LIVE"}],  # Not identity evidence.
        [{"field": "entitlement_messaging.message", "quote": "Included"}],
    ],
)
async def test_rich_evidence_must_come_from_its_claimed_field_and_establish_identity(evidence):
    model = Model(
        {"content_id": GTI, "viewing_option_id": "prime-option", "reason": "Match", "evidence": evidence}
    )
    with pytest.raises(ExecutorError, match="supplied evidence"):
        await EventMatcher(ExecutorConfig(), model=model).choose(
            payload(),
            results(
                tile("NYJ @ DET", synopsis="Sunday football", entitlement_messaging={"message": "Included"})
            ),
            "America/Vancouver",
        )


async def test_model_can_select_equivalent_valid_feed_instead_of_abstaining_for_duplicate_identity():
    model = Model(
        {
            "content_id": GTI,
            "viewing_option_id": "prime-option",
            "reason": "Both feeds describe the same event; stable content ID tie-break",
            "evidence": [{"field": "title", "quote": "Jets vs. Lions"}],
        }
    )
    selected, _ = await EventMatcher(ExecutorConfig(), model=model).choose(
        payload(), results(tile(cid=GTI + "-other"), tile()), "America/Vancouver"
    )
    assert selected["content_id"] == GTI and selected["readiness"] == "ready"
