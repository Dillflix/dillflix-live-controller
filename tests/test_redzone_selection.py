"""Unknown-status Teamarr broadcasts must reach Prime's existing live check."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from playback_fixtures import payload
from test_prime_catalogue import saved
from test_prime_matching import results, tile
from test_prime_workflow import cleanup, rig, row

from controller.models import Command
from controller.planner import content_view


def redzone(now):
    # Same status/route shape as the incident export; times follow the test clock.
    content_id = "broadcast:nfl_redzone%3A2026-10-04"
    return {
        "id": content_id,
        "kind": "broadcast",
        "title": "NFL RedZone",
        "competition": "nfl",
        "source": "nfl_redzone",
        "status": "unknown",
        "start_time": (now - timedelta(minutes=40)).isoformat(),
        "expected_end_time": (now + timedelta(hours=6)).isoformat(),
        "viewing_options": [
            {
                "id": "view:nfl_redzone%3A2026-10-04",
                "app": "prime_video",
                "stream_title": "NFL RedZone",
                "broadcast_id": content_id,
                "coverage_type": "multi_event_coverage",
                "presentation": None,
                "basis": "rule",
                "decision": "eligible",
                "reasons": [],
            }
        ],
    }


@pytest.mark.parametrize("selection", ["priority", "watch_plan", "play_now"])
@pytest.mark.parametrize(
    "prime_changes,expected",
    [
        ({}, "ready"),
        ({"event_state": "UPCOMING"}, "waiting_for_feed"),
        ({"event_state": "UNAVAILABLE"}, "feeds_unavailable"),
        ({"entitlement_status": "UNENTITLED"}, "feeds_locked"),
        ({"event_state": "ENDED"}, "no_matching_feed"),
        ({"title": "NFL RedZone Replay"}, "no_matching_feed"),
    ],
)
async def test_redzone_uses_catalogue_before_switching(tmp_path, selection, prime_changes, expected):
    controller, workflow = rig(tmp_path)
    now = datetime.now(UTC)
    broadcast = redzone(now)
    game = payload()["content_snapshot"]
    original = workflow.player.search

    async def search(*args):
        return {**await original(*args), **results(tile(**{"title": "NFL RedZone", **prime_changes}))}

    workflow.player.search = search
    try:
        with controller.db.transaction() as db:
            controller.replace_catalog(db, [game, broadcast], "teamarr")
            device = controller.db.device(db)
            device.update(
                automation="active",
                desired=game["id"],
                playback_state="verified",
                observed={"content_id": game["id"], "verified": True},
                plan=[],
                rules=[
                    {"id": "redzone", "name": "NFL RedZone", "source": "nfl_redzone"},
                    {"id": "nfl", "name": "NFL", "league": "nfl"},
                ],
            )
            # Play Now must override priorities as well as reach the catalogue.
            if selection == "play_now":
                device["rules"].reverse()
            controller.db.save_device(db, device)
        if selection != "priority":
            command = Command(
                command_id="select-redzone",
                expected_revision=device["revision"],
                action={
                    "type": "play_now" if selection == "play_now" else "add",
                    "content_id": broadcast["id"],
                },
            )
            receipt = controller.plan_command(device["id"], command)
            assert receipt["accepted"]
            assert controller.plan_command(device["id"], command) == receipt

        controller.stage_playback()
        before, probe = saved(controller)
        assert probe and probe["content_id"] == broadcast["id"]
        assert before["desired"] == game["id"] and before["observed"]["verified"]
        with controller.db.transaction() as db:
            assert not db.execute("SELECT 1 FROM jobs").fetchone()

        await workflow.observe_catalogue(probe)
        checked, probe = saved(controller)
        assert probe["state"] == expected
        assert checked["desired"] == game["id"] and checked["observed"]["verified"]
        controller.stage_playback()
        device, _ = saved(controller)
        with controller.db.transaction() as db:
            jobs = db.execute("SELECT * FROM jobs").fetchall()
            item = next(i for i in controller.items(db) if i["content_id"] == broadcast["id"])
        assert item["lifecycle"]["state"] == "unknown"
        if expected == "ready":
            assert len(jobs) == 1 and jobs[0]["content_id"] == broadcast["id"]
            assert device["intent_version"] == before["intent_version"] + 1
            body = json.loads(jobs[0]["payload"])
            assert body["content_snapshot"] == broadcast
            assert body["allowed_viewing_options"] == broadcast["viewing_options"]
            body["deadline_at"] = datetime.fromtimestamp(jobs[0]["deadline_at"], UTC).isoformat()
            report, _ = workflow.store.submit(body)
            await workflow.navigate(row(workflow, report["token"]))
            assert workflow.store.report(report["token"])["operation"]["state"] == "playing_verified"
            assert [call[0] for call in workflow.player.calls].count("play") == 1
        else:
            assert not jobs
            assert device["desired"] == game["id"] and device["observed"]["verified"]
            assert device["intent_version"] == before["intent_version"]
            assert [call[0] for call in workflow.player.calls] == ["search"]
        assert workflow.player.calls[0] == ("search", "NFL RedZone")
        if selection != "priority":
            assert device["plan"][0]["content_id"] == broadcast["id"]
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("change", ["stale", "withdrawn", "completed"])
async def test_ready_catalogue_cannot_launch_a_now_ineligible_broadcast(tmp_path, change):
    controller, workflow = rig(tmp_path)
    broadcast = redzone(datetime.now(UTC))
    original = workflow.player.search

    async def search(*args):
        return {**await original(*args), **results(tile("NFL RedZone"))}

    workflow.player.search = search
    try:
        with controller.db.transaction() as db:
            controller.replace_catalog(db, [broadcast], "teamarr")
            device = controller.db.device(db)
            device.update(automation="active", plan=[controller.entry(broadcast["id"])])
            controller.db.save_device(db, device)
        controller.stage_playback()
        _, probe = saved(controller)
        assert probe and probe["content_id"] == broadcast["id"]
        await workflow.observe_catalogue(probe)
        _, probe = saved(controller)
        assert probe["state"] == "ready"
        with controller.db.transaction() as db:
            if change == "stale":
                db.execute(
                    "UPDATE contents SET seen_at=?",
                    ((datetime.now(UTC) - timedelta(hours=1)).isoformat(),),
                )
            elif change == "withdrawn":
                controller.replace_catalog(db, [], "teamarr")
            else:
                device = controller.db.device(db)
                device["manual_completions"][broadcast["id"]] = datetime.now(UTC).isoformat()
                controller.db.save_device(db, device)
        controller.stage_playback()
        with controller.db.transaction() as db:
            assert not db.execute("SELECT 1 FROM jobs").fetchone()
            assert controller.db.device(db)["plan"][0]["content_id"] == broadcast["id"]
        assert [call[0] for call in workflow.player.calls] == ["search"]
    finally:
        await cleanup(controller)


@pytest.mark.parametrize(
    "case,eligible",
    [
        ("fresh", True),
        ("elapsed_estimate", True),
        ("future", False),
        ("stale", False),
        ("expired_live", False),
        ("inactive", False),
        ("event", False),
        ("session", False),
        ("simulator", False),
        ("non_prime", False),
        ("excluded", False),
        ("replay", False),
        ("ended", False),
        ("cancelled", False),
        ("postponed", False),
        ("delayed", False),
        ("suspended", False),
    ],
)
def test_unknown_broadcast_search_boundaries(case, eligible):
    now = datetime.now(UTC)
    snapshot = redzone(now)
    lifecycle = {"state": "unknown", "stale": False, "source": "teamarr_feed"}
    active, search_at = True, now
    if case == "elapsed_estimate":
        snapshot["expected_end_time"] = (now - timedelta(minutes=1)).isoformat()
    elif case == "future":
        snapshot["start_time"] = (now + timedelta(minutes=1)).isoformat()
    elif case in {"stale", "expired_live"}:
        lifecycle["stale"] = True
        if case == "expired_live":
            snapshot["status"] = "live"
            lifecycle["last_known_state"] = "live"
    elif case == "inactive":
        active = False
    elif case in {"event", "session"}:
        snapshot["kind"] = case
    elif case == "simulator":
        search_at = None
    elif case == "non_prime":
        snapshot["viewing_options"][0]["app"] = "other"
    elif case == "excluded":
        snapshot["viewing_options"][0]["decision"] = "excluded"
    elif case == "replay":
        snapshot["viewing_options"][0]["presentation"] = "replay"
    elif case in {"ended", "cancelled", "postponed", "delayed", "suspended"}:
        lifecycle["state"] = case
    item = content_view(snapshot, active, lifecycle, prime_search_at=search_at)
    assert item["playable"] is eligible
    assert item["lifecycle"] == lifecycle
    if eligible:
        assert item["launch_eligibility"] == "broadcast_start_reached"
        assert item["availability_reason"] == "Prime live availability check required"
