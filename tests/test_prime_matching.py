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
        "handle": "handle:" + cid,
        "title": title,
        "content_id": cid,
        "identity_status": "structure_slot_artwork_correlated",
        "availability": "live",
        "is_locked": False,
        "action": "watch",
        "resolution_status": "resolved",
        "labels": [title, "LIVE"],
        "date_label": None,
        "collection": "Top Sports",
        **changes,
    }


def results(*tiles):
    return {"tiles": list(tiles), "complete": False, "coverage": "loaded_renderer", "warnings": []}


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
        {"availability": "replay"},
        {"availability": None},
        {"content_id": "amzn1.dv.icid.collection"},
        {"content_id": None},
        {"identity_status": "ambiguous"},
        {"handle": None},
        {"labels": ["Jets vs Lions", "Rapid Recap"]},
        {"title": "Jets vs Lions Highlights"},
        {"date_label": "2000-01-01"},
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
        payload(), results(tile(), tile(handle="another-row")), "America/Vancouver"
    )
    assert choice["content_id"] == GTI
    assert len(audit["candidates"]) == 1


async def test_llm_resolves_labels_without_structured_candidate_metadata():
    model = Model(
        {
            "content_id": GTI,
            "viewing_option_id": "prime-option",
            "reason": "NY Jets and Detroit identify the requested opponents",
            "evidence_labels": ["NY Jets @ Detroit"],
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
        {"evidence_labels": ["invented label"]},
        {"evidence_labels": []},
        {"content_id": None},
    ],
)
async def test_llm_must_select_supplied_live_candidate_and_evidence(changes):
    answer = {
        "content_id": GTI,
        "viewing_option_id": "prime-option",
        "reason": "Match",
        "evidence_labels": ["NYJ @ DET"],
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
            "evidence_labels": [],
        }
    )
    choice, audit = await EventMatcher(ExecutorConfig(), model=model).choose(
        payload(), choices, "America/Vancouver"
    )
    assert choice is None and audit["method"] == "llm"
