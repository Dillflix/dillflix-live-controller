"""Source projection and native playback use the same idle identity end to end."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from controller.database import encode
from controller.executor.models import PlaybackReport
from controller.idle_sports import candidate_id, content_snapshot, expires_at
from controller.models import Action
from controller.prime_player.linear import choose_idle_event, choose_airing
from controller.public import public_overview
from playback_fixtures import payload
from test_prime_workflow import rig, cleanup, row, Player
from test_prime_linear import LinearPlayer, STATION, AIRING
from test_prime_matching import GTI


def candidate(kind="linear_program"):
    now = datetime.now(UTC)
    result = {"kind": kind, "title": "Hockey analysis", "synopsis": "Analysis of today's hockey games.",
        "starts_at": (now - timedelta(minutes=10)).isoformat(), "ends_at": (now + timedelta(minutes=30)).isoformat(),
        "entitlement_status": "ENTITLED", "observed_at": now.isoformat()}
    result.update({"station_id": STATION, "airing_id": AIRING, "station_name": "Sportsnet Pacific",
                   "has_watch_action": True} if kind == "linear_program" else
                  {"content_id": GTI, "content_type": "EVENT", "event_state": "LIVE"})
    result["candidate_id"] = candidate_id(result)
    return result


def seed(controller, value):
    snapshot = content_snapshot(value)
    with controller.db.transaction() as db:
        db.execute("INSERT INTO contents VALUES (?,?,1,?,?)", (snapshot["id"], "prime_idle", value["observed_at"], encode(snapshot)))
        device = controller.db.device(db, controller.device_id)
        device["preferences"]["idle_sports_enabled"] = True
        device["idle_sports"] = {"state": "ready", "content_id": snapshot["id"], "expires_at": expires_at(value)}
        controller.db.save_device(db, device)
    request = payload()
    request.update(content_id=snapshot["id"], content_snapshot=snapshot, allowed_viewing_options=snapshot["viewing_options"])
    return request


def event_catalogue(value):
    return {"complete": True, "events": [value], "observed_at": datetime.now(UTC).isoformat(),
        "session_id": "service-session-1", "generation": 1, "request_id": "idle-catalogue"}


class EventPlayer(Player):
    def __init__(self, value):
        super().__init__()
        self.value = value

    async def health(self):
        health = await super().health()
        health["capabilities"].append("linear_catalog")
        health["compatibility"]["capabilities"]["linear_catalog"] = {"available": True}
        return health

    async def rpc(self, method, **params):
        assert method == "linear_catalog" and params["facet"] == "sports" and params["max_pages"] in {3, 8}
        self.calls.append((method,))
        return event_catalogue(self.value)


@pytest.mark.parametrize("kind", ["event", "linear_program"])
async def test_idle_source_launches_exact_native_identity_without_find_and_has_bounded_lifecycle(tmp_path, kind):
    controller, workflow = rig(tmp_path)
    try:
        value = candidate(kind)
        request = seed(controller, value)
        workflow.player = EventPlayer(value) if kind == "event" else LinearPlayer(request)
        if kind == "linear_program":
            workflow.player.guide["station_summary"]["stations"][0]["airings"][0]["synopsis"] = value["synopsis"]
        report, _ = workflow.store.submit(request)
        await workflow.navigate(row(workflow, report["token"]))
        report = workflow.store.report(report["token"])
        PlaybackReport.model_validate(report)
        assert report["operation"]["state"] == "playing_verified"
        assert report["observation"]["content_id"] == value["candidate_id"]
        assert report["content_status"]["observation"]["source"] == "prime_live_catalogue"
        assert not any(c[0] in {"search", "broadcasts"} for c in workflow.player.calls)
        assert [c[1] for c in workflow.player.calls if c[0] == "play"] == [STATION if kind == "linear_program" else GTI]
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("change", ["different_id", "ended", "locked", "changed_program", "stale"])
def test_exact_idle_event_revalidation_rejects_changed_or_stale_evidence(change):
    value = candidate("event")
    snapshot = content_snapshot(value)
    request = {"content_snapshot": snapshot, "allowed_viewing_options": snapshot["viewing_options"]}
    result = event_catalogue(deepcopy(value))
    assert choose_idle_event(request, result)[0]
    if change == "different_id": result["events"][0]["content_id"] += "-other"
    if change == "ended": result["events"][0]["event_state"] = "ENDED"
    if change == "locked": result["events"][0]["entitlement_status"] = "UNENTITLED"
    if change == "changed_program": result["events"][0]["synopsis"] = "Movie"
    if change == "stale": result["observed_at"] = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
    assert choose_idle_event(request, result)[0] is None


def test_conflicting_program_description_cannot_renew_a_sports_airing():
    value = candidate()
    snapshot = content_snapshot(value)
    request = {"content_snapshot": snapshot, "allowed_viewing_options": snapshot["viewing_options"]}
    player = LinearPlayer(request)
    station = player.guide["station_summary"]["stations"][0]
    station["airings"][0]["synopsis"] = value["synopsis"]
    assert choose_airing(request, player.guide)[0]
    station["airings"].append({**station["airings"][0], "synopsis": "A crime drama."})
    assert choose_airing(request, player.guide)[0] is None


async def test_idle_projection_expiry_public_list_and_manual_actions(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        value = candidate()
        request = seed(controller, value)
        with controller.db.transaction() as db:
            controller.db.set_meta(db, "feed_health", {"state": "error", "error": "Teamarr unavailable"})
            device = controller.db.device(db, controller.device_id)
            item = next(i for i in controller.items(db) if i["content_id"] == value["candidate_id"])
            assert item["playable"] and item["entitlement"]["status"] == "entitled"
            assert item["station_id"] == STATION
            with pytest.raises(HTTPException, match="Idle sports"):
                controller.apply_plan(db, device, Action(type="play_now", content_id=item["content_id"]).model_dump())
            device["observed"] = {"content_id": item["content_id"]}
            controller.db.save_device(db, device)
        assert controller.overview(events_only=True)["events"] == []
        public = public_overview(controller, {"id": "guest:anonymous", "name": "Guest"})
        assert public["events"] == [] and public["now_playing"]["event"]["source"] == "prime_idle"
        with controller.db.snapshot() as db:
            assert controller.db.meta(db, "feed_health")["error"] == "Teamarr unavailable"
            stored = db.execute("SELECT * FROM contents WHERE id=?", (value["candidate_id"],)).fetchone()
            later = datetime.now(UTC) + timedelta(seconds=61)
            lifecycle = controller.content_lifecycle(stored, None, later, later, True)
            assert lifecycle["state"] == "unknown" and lifecycle["stale"]
    finally:
        await cleanup(controller)
