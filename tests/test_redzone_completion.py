"""RedZone completion uses linked game results, including after discovery ends."""

import copy
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from controller.broadcast_overlay import broadcast_overlay
from controller.content_status import InvalidStatus, TeamarrContentStatusAdapter, validate_report
from controller.teamarr import TeamarrClient


def snapshot():
    start = datetime.now(UTC) - timedelta(days=1)
    return {
        "id": "broadcast:nfl_redzone%3Aprevious-sunday", "kind": "broadcast",
        "source": "nfl_redzone", "title": "NFL RedZone", "competition": "nfl",
        "timing_basis": "rule", "status": "unknown", "start_time": start.isoformat(),
        "expected_end_time": (start + timedelta(hours=7)).isoformat(),
        "related_ids": ["event:espn:nfl:early", "event:espn:nfl:late"],
        "viewing_options": [{"id": "prime-redzone", "app": "prime_video", "decision": "eligible"}],
    }


def game(identity, state="final", **changes):
    return {**snapshot(), "id": identity, "kind": "event", "source": "games",
            "status": state, **changes}


def adapter(entries):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"schema_version": 1, "items": entries})

    return TeamarrContentStatusAdapter(
        TeamarrClient("https://teamarr.invalid", transport=httpx.MockTransport(respond)), 120
    ), calls


def request(original=None, fresh=False):
    original = original or snapshot()
    return {
        "schema_version": 1, "request_id": "redzone-status", "content_id": original["id"],
        "content_snapshot": original,
        "catalog_seen_at": (datetime.now(UTC) - timedelta(seconds=0 if fresh else 3600)).isoformat(),
    }


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("listing_present", [False, True])
async def test_all_linked_games_final_complete_redzone_even_without_broadcast_listing(fresh, listing_present):
    original = snapshot()
    entries = [game(identity) for identity in original["related_ids"]]
    entries += [original] if listing_present else []
    # The unrelated evening game must not keep afternoon RedZone alive.
    entries += [game("event:espn:nfl:evening", "live")]
    status, calls = adapter(entries)
    req = request(original, fresh)
    before = copy.deepcopy(req)
    report = await status.lookup(req)
    result = validate_report(req, report, datetime.now(UTC), 120)
    assert result["state"] == "ended" and result["source"] == "teamarr_related_events"
    assert result["simulated"] is False
    assert req == before and len(calls) == 1
    assert calls[0].url.params.get_list("source") == ["games", "nfl_redzone"]
    assert "as_of" not in calls[0].url.params


@pytest.mark.parametrize("remaining", ["live", "scheduled", "unknown", "delayed", "suspended", "postponed", "missing"])
@pytest.mark.parametrize("fresh", [False, True])
async def test_incomplete_games_never_complete_even_after_estimated_end(remaining, fresh):
    original = snapshot()
    # A partial refreshed listing must not erase the saved late-game identity.
    listing = {**original, "related_ids": original["related_ids"][:1]}
    entries = [listing, game(original["related_ids"][0])]
    if remaining != "missing":
        entries.append(game(original["related_ids"][1], remaining))
    status, _ = adapter(entries)
    result = (await status.lookup(request(original, fresh)))["observation"]
    assert result["state"] == "unknown"
    assert result["source"] == ("teamarr_feed" if fresh else "teamarr_history")


async def test_new_linked_game_blocks_completion_but_missing_link_set_is_not_completion():
    original = snapshot()
    new = "event:espn:nfl:new"
    entries = [{**original, "related_ids": [*original["related_ids"], new]}, game(new, "live")]
    entries += [game(identity) for identity in original["related_ids"]]
    status, _ = adapter(entries)
    assert (await status.lookup(request(original)))["observation"]["state"] == "unknown"
    status, _ = adapter([])
    assert (await status.lookup(request({**original, "related_ids": []})))["observation"]["state"] == "unknown"


@pytest.mark.parametrize("states,expected", [(["final", "cancelled"], "ended"), (["cancelled", "cancelled"], "cancelled")])
async def test_explicit_cancelled_games_are_terminal(states, expected):
    original = snapshot()
    status, _ = adapter([game(identity, state) for identity, state in zip(original["related_ids"], states)])
    assert (await status.lookup(request(original)))["observation"]["state"] == expected


async def test_stale_final_results_cannot_be_refreshed_by_new_http_receipt():
    original = snapshot()
    entries = [game(identity) for identity in original["related_ids"]]
    entries[0]["status_received_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    status, _ = adapter(entries)
    req = request(original)
    with pytest.raises(InvalidStatus, match="expired"):
        validate_report(req, await status.lookup(req), datetime.now(UTC), 120)


async def test_special_override_is_not_inferred_complete_from_related_games():
    original = {**snapshot(), "timing_basis": "override"}
    status, calls = adapter([original, *[game(identity) for identity in original["related_ids"]]])
    assert (await status.lookup(request(original)))["observation"]["state"] == "unknown"
    assert calls[0].url.params.get_list("source") == ["nfl_redzone"]


@pytest.mark.parametrize("changes", [{"kind": "broadcast"}, {"source": "special_events"}, {"competition": "nhl"}])
async def test_completion_requires_game_identity_and_type(changes):
    original = snapshot()
    entries = [game(identity) for identity in original["related_ids"]]
    entries[0].update(changes)
    status, _ = adapter(entries)
    assert (await status.lookup(request(original)))["observation"]["state"] == "unknown"


async def test_future_fresh_redzone_does_not_query_game_results():
    original = {**snapshot(), "start_time": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
    status, calls = adapter([])
    assert (await status.lookup(request(original, fresh=True)))["observation"]["state"] == "unknown"
    assert calls == []


@pytest.mark.parametrize("terminal", [False, True])
async def test_retained_redzone_plan_and_idle_recovery_reconcile_from_status_worker(rig, terminal):
    _, service, _ = rig
    original = snapshot()
    entries = [game(identity, "final" if terminal else "live") for identity in original["related_ids"]]
    service.status_adapter, _ = adapter(entries)
    with service.db.transaction() as db:
        db.execute("DELETE FROM contents")
        service.replace_catalog(db, [original], "teamarr")
        db.execute("UPDATE contents SET active=0,seen_at=?", (request()["catalog_seen_at"],))
        device = service.db.device(db)
        device.update(
            plan=[service.entry(original["id"])], desired=None, observed=None, playback_state="waiting",
            recovery={"content_id": original["id"], "since": original["start_time"], "attempts": 0},
            retry_playback=original["id"],
        )
        service.db.save_device(db, device)
    # The overlay is correct even before the coordinator repairs old durable state.
    assert broadcast_overlay(service, service.device_id).state == "waiting"
    await service.refresh_status(force=True)
    service.stage_playback()
    with service.db.snapshot() as db:
        device = service.db.device(db)
        assert bool(device["plan"]) is not terminal
        assert device["recovery"] is None and device["retry_playback"] is None
        assert device["observed"] is None and device["desired"] is None
        assert device["playback_state"] == "waiting"
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        result = json.loads(db.execute("SELECT observation FROM content_status WHERE content_id=?",
                                      (original["id"],)).fetchone()[0])
        assert result["state"] == ("ended" if terminal else "unknown")
    assert broadcast_overlay(service, service.device_id).state == "waiting"
