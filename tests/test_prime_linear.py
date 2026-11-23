"""TV airing identity and verification, separate from station playback identity."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from playback_fixtures import payload
from test_prime_workflow import Player, cleanup, rig, row

from controller.executor.models import ExecutorError, PlaybackReport
from controller.prime_player.client import PrimePlayerClient
from controller.prime_player.linear import airing_valid_until, choose_airing

# Real station ID from the November 23 guide/resolver capture. WPT below is a
# synthetic airing for behavior tests; no WPT was present in that guide capture.
STATION = "amzn1.dv.gti.3717339d-6507-49e0-9e31-fd7c0588fd1d"
AIRING = "amzn1.pv.linear.airing.test-wpt"


def request():
    value = payload()
    now = datetime.now(UTC)
    value["allowed_viewing_options"][0].update(channel="Sportsnet Pacific", stream_title="World Poker Tour")
    value["content_snapshot"].update(
        kind="broadcast", source="sportsnet_wpt", competition="wpt", title="World Poker Tour", event=None,
        start_time=(now - timedelta(minutes=10)).isoformat(),
        expected_end_time=(now + timedelta(minutes=50)).isoformat(),
        broadcast={"coverage_type": "linear_program"},
        viewing_options=deepcopy(value["allowed_viewing_options"]),
    )
    return value


def catalogue(req):
    snap = req["content_snapshot"]
    return {"session_id": "service-session-1", "generation": 1, "complete": True,
            "query": snap["title"], "observed_at": datetime.now(UTC).isoformat(),
            "coverage": "initial_live_page_response", "station_summary": {
                "schema": "live_tv_epg", "has_more": True, "stations": [{
                    "station_id": STATION, "name": "Sportsnet Pacific", "entitlement_status": "ENTITLED",
                    "has_watch_action": True, "has_live_content": False, "airings": [{
                        "airing_id": AIRING, "station_id": STATION, "title": snap["title"],
                        "starts_at": snap["start_time"], "ends_at": snap["expected_end_time"],
                    }],
                }],
            }}


def test_exact_tv_airing_has_separate_station_and_program_ids():
    req = request()
    result = catalogue(req)
    choice, audit = choose_airing(req, result)
    assert choice["content_id"] == STATION
    assert choice["linear_airing"]["airing_id"] == AIRING
    assert choice["readiness"] == "ready" and audit["has_more"] is True
    assert "event_state" not in choice  # Never claim the underlying competition is live.
    assert airing_valid_until(choice, datetime.now(UTC)) <= datetime.now(UTC) + timedelta(seconds=60)


@pytest.mark.parametrize("case", ["wrong_channel", "next_episode", "wrong_title", "missing_window", "naive_time",
                                  "no_watch", "unknown_access", "signup", "overlap", "conflicting_access", "conflicting_window",
                                  "missing_station", "stale", "future_observation", "partial_transfer", "unknown_schema",
                                  "unknown_language", "unknown_channel"])
def test_unproven_station_or_slot_cannot_launch_or_claim_absence(case):
    req = request()
    result = catalogue(req)
    station = result["station_summary"]["stations"][0]
    airing = station["airings"][0]
    if case == "wrong_channel": station["name"] = "Sportsnet Ontario"
    if case == "next_episode": airing["starts_at"] = airing["ends_at"]
    if case == "wrong_title": airing["title"] = "Hockey Central"
    if case == "missing_window": airing.pop("ends_at")
    if case == "naive_time": airing["starts_at"] = "2026-11-23T20:00:00"
    if case == "no_watch": station["has_watch_action"] = False
    if case == "unknown_access": station["entitlement_status"] = None
    if case == "signup": station.update(entitlement_status="UNENTITLED", has_watch_action=False)
    if case == "overlap": station["airings"].append({**airing, "airing_id": AIRING + "-other"})
    if case == "conflicting_access": result["station_summary"]["stations"].append({**station, "entitlement_status": "UNENTITLED"})
    if case == "conflicting_window": station["airings"].append({**airing, "starts_at": None})
    if case == "missing_station": result["station_summary"]["stations"] = []
    if case == "stale": result["observed_at"] = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
    if case == "future_observation": result["observed_at"] = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    if case == "partial_transfer": result["complete"] = False
    if case == "unknown_schema": result["station_summary"]["schema"] = "unrecognized"
    if case == "unknown_language": req["allowed_viewing_options"][0]["language"] = "English"
    if case == "unknown_channel": req["allowed_viewing_options"][0].pop("channel")
    selected, audit = choose_airing(req, result)
    assert selected is None
    assert audit["match_status"] == "uncertain"


def test_upcoming_airing_proves_station_access_but_cannot_play_yet():
    req = request()
    req["content_snapshot"]["start_time"] = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    choice, _ = choose_airing(req, catalogue(req))
    assert choice["entitlement_status"] == "ENTITLED"
    assert choice["readiness"] == "waiting_for_feed"
    assert airing_valid_until(choice, datetime.now(UTC)) is None


class LinearPlayer(Player):
    def __init__(self, req):
        super().__init__()
        self.guide = catalogue(req)
        self.after_read = None

    async def health(self):
        value = await super().health()
        value["capabilities"].append("linear_catalog")
        value["compatibility"]["capabilities"]["linear_catalog"] = {"available": True}
        return value

    async def rpc(self, method, **params):
        assert method == "linear_catalog"
        assert params["ownership"] == (await self.health())["ownership"]
        self.calls.append((method,))
        result = {**deepcopy(self.guide), "observed_at": datetime.now(UTC).isoformat()}
        if self.after_read:
            self.after_read()
        return result

    def outcome(self):
        return {**super().outcome(), "requested_id": STATION, "resolved_id": STATION}

    async def status(self, *args):
        return {**await super().status(*args), "requested_id": STATION,
                "resolved_id": STATION, "current_content_id": STATION}


async def launch_linear(workflow, req):
    workflow.player = LinearPlayer(req)
    report, _ = workflow.store.submit(req)
    await workflow.navigate(row(workflow, report["token"]))
    assert workflow.store.report(report["token"])["operation"]["state"] == "playing_verified"
    return report["token"]


async def test_tv_launch_and_monitor_keep_teamarr_identity_and_never_find_search(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        req = request()
        token = await launch_linear(workflow, req)
        report = workflow.store.report(token)
        PlaybackReport.model_validate(report)
        assert report["content_id"] == req["content_id"]
        assert report["observation"]["content_id"] == req["content_id"]
        await workflow.monitor(row(workflow, token))
        assert workflow.store.report(token)["observation"]["verified"]
        assert len([c for c in workflow.player.calls if c[0] == "play"]) == 1
        assert not any(c[0] in {"search", "resolve", "broadcasts"} for c in workflow.player.calls)
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("case", ["missing_airing", "wrong_airing", "ownership", "native_ended", "expired_window"])
async def test_station_playback_cannot_keep_old_program_verified_or_complete_it(tmp_path, case):
    controller, workflow = rig(tmp_path)
    try:
        req = request()
        token = await launch_linear(workflow, req)
        station = workflow.player.guide["station_summary"]["stations"][0]
        if case == "missing_airing": station["airings"] = []
        if case == "wrong_airing": station["airings"][0]["airing_id"] += "-next"
        if case == "ownership": workflow.player.after_read = lambda: setattr(workflow.player, "suspended", True)
        if case == "native_ended": workflow.player.state = "ended"
        if case == "expired_window":
            selected = workflow.store.report(token)["prime_player"]["selected"]
            selected["linear_airing"]["ends_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            workflow.save_workflow(token, selected=selected)
            workflow.record_status(token, await workflow.player.status(workflow.player.attempt_id, 60))
        else:
            await workflow.monitor(row(workflow, token))
        report = workflow.store.report(token)
        assert report["observation"]["verified"] is False
        assert report["operation"]["state"] != "completed"
        assert report["content_status"]["effective_state"] != "ended"
        assert len([c for c in workflow.player.calls if c[0] == "play"]) == 1
    finally:
        await cleanup(controller)


async def test_background_tv_entitlement_only_reads_guide(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        req = request()
        workflow.player = LinearPlayer(req)
        with controller.db.transaction() as db:
            controller.replace_catalog(db, [req["content_snapshot"]], "teamarr")
        await controller.check_entitlement_once()
        with controller.db.snapshot() as db:
            assert db.execute("SELECT status FROM event_entitlements").fetchone()[0] == "entitled"
        assert [c[0] for c in workflow.player.calls] == ["linear_catalog"]
    finally:
        await cleanup(controller)


def test_optional_guide_absence_does_not_disable_ordinary_search():
    health = {"api_version": 11, "capabilities": ["search"],
              "compatibility": {"capabilities": {"search": {"available": True}}}}
    PrimePlayerClient.require(health, "search")
    with pytest.raises(ExecutorError) as exc:
        PrimePlayerClient.require(health, "linear_catalog")
    assert exc.value.code == "prime_linear_catalog_unavailable"


async def test_foreground_guide_check_does_not_change_intent_or_play(tmp_path, monkeypatch):
    from test_prime_catalogue import setup_selection, saved
    from controller.planner import content_view

    controller, workflow = rig(tmp_path)
    try:
        req = request()
        _, item, intent = setup_selection(controller, monkeypatch)
        item.clear()
        item.update(content_view(req["content_snapshot"], True, {"state": "live"}))
        workflow.player = LinearPlayer(req)
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        device, probe = saved(controller)
        assert probe["state"] == "ready"
        assert probe["selected"]["linear_airing"]["airing_id"] == AIRING
        assert device["intent_version"] == intent
        assert [c[0] for c in workflow.player.calls] == ["linear_catalog"]
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("changed_context", [False, True])
async def test_network_acquisition_uses_guide_and_rejects_changed_context(tmp_path, changed_context):
    from dataclasses import replace
    from test_prime_network import POLICY

    controller, workflow = rig(tmp_path)
    try:
        req = request()
        workflow.config = replace(workflow.config, prime_network_policy=POLICY)
        player = workflow.player = LinearPlayer(req)
        original_health = player.health

        async def health():
            value = await original_health()
            value["api_version"] = 12
            value["network"] = {"enabled": True, "state": "ready", "context_id": "network-direct",
                                "context": {"target_id": "direct"}}
            value["ownership"]["network_context_id"] = "network-direct"
            return value

        player.health = health
        player.guide["network_context_id"] = "network-old" if changed_context else "network-direct"
        original_outcome = player.outcome
        player.outcome = lambda: {**original_outcome(), "network_context_id": "network-direct"}
        report, _ = workflow.store.submit(req)
        token = report["token"]
        await workflow.navigate(row(workflow, token))
        actual = workflow.store.report(token)
        if changed_context:
            assert actual["operation"]["state"] != "playing_verified"
            assert not any(c[0] == "play" for c in player.calls)
        else:
            assert actual["operation"]["state"] == "playing_verified"
        assert any(c[0] == "linear_catalog" for c in player.calls)
        assert not any(c[0] == "search" for c in player.calls)
    finally:
        await cleanup(controller)
