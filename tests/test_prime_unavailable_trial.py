import pytest

from playback_fixtures import payload
from test_prime_workflow import rig, cleanup, row
from test_prime_network import network_rig, CITY_POLICY
from test_prime_matching import UNAVAILABLE_BADGE
from controller.prime_player.matching import unavailable_live_trial


@pytest.mark.parametrize("entitlement,state,eligible", [
    ("ENTITLED", "LIVE", True), ("UNENTITLED", "LIVE", False),
    (None, "LIVE", False), ("ENTITLED", "UPCOMING", False),
    ("ENTITLED", "ENDED", False),
])
def test_trial_requires_explicit_live_entitlement(entitlement, state, eligible):
    assert unavailable_live_trial({"entitlement_status": entitlement, "event_state": state,
                                   "availability_status": "UNAVAILABLE"}) is eligible


@pytest.mark.parametrize("result", ["playing", "refused", "failed", "unknown"])
async def test_unavailable_direct_trial_uses_actual_playback_outcome(tmp_path, result):
    c, w = rig(tmp_path)
    original_search, original_outcome = w.player.search, w.player.outcome
    async def search(*args):
        value = await original_search(*args)
        value["containers"][0]["items"][0]["entitlement_messaging"] = UNAVAILABLE_BADGE
        return value
    def outcome():
        value = original_outcome()
        if result != "playing":
            value.update(state="failed" if result == "refused" else result, reason="Fixture failure")
        if result == "refused":
            value.update(resolved_id=None, evidence={"launch": {"disposition": "not_invoked"}})
        return value
    w.player.search, w.player.outcome = search, outcome
    try:
        report, _ = w.store.submit(payload())
        await w.navigate(row(w, report["token"]))
        final = w.store.report(report["token"])
        assert len([c for c in w.player.calls if c[0] == "play"]) == 1
        assert final["operation"]["state"] == {
            "playing": "playing_verified", "refused": "feeds_unavailable",
            "failed": "feeds_unavailable", "unknown": "failed",
        }[result]
        assert any(c[0] == "stop" for c in w.player.calls) == (result == "failed")
        if result in {"refused", "failed"}:
            assert final["prime_player"]["unavailable_trials"][0]["outcome"]["state"] == "failed"
    finally:
        await cleanup(c)


async def test_refused_trial_restart_continues_to_toronto_without_replaying_direct(tmp_path):
    c, w = network_rig(tmp_path)
    try:
        with c.db.transaction() as db:
            device = c.db.device(db)
            device["preferences"]["vpn_policy"] = CITY_POLICY
            c.db.save_device(db, device)
        request = payload()
        request["content_snapshot"]["event"]["home_team_details"]["city"] = "Toronto"
        report, _ = w.store.submit(request)
        token = report["token"]
        assert await w.select_and_launch(token, request)
        assert await w.verify_launch(token) is True
        saved = w.store.report(token)["prime_player"]
        assert saved["attempt_id"] is None
        assert saved["network_acquisition"]["tried"] == ["direct"]
        w.recover()
        await w.navigate(row(w, token))
        assert w.store.report(token)["operation"]["state"] == "playing_verified"
        assert [t["target_id"] for t in w.player.transitions] == ["toronto"]
        calls = [c for c in w.player.calls if c[0] == "play"]
        assert len(calls) == 2 and calls[0][2] != calls[1][2]
    finally:
        await cleanup(c)


async def test_unsettled_failed_trial_cannot_switch_network(tmp_path):
    c, w = network_rig(tmp_path)
    base = w.player.outcome
    def outcome():
        value = base()
        value["evidence"] = {"resolution": {"playbackClass": "live_watch_now"}}
        return value
    w.player.outcome = outcome
    w.player.stop_confirmed = False
    try:
        report, _ = w.store.submit(payload())
        await w.navigate(row(w, report["token"]))
        assert not w.player.transitions
        assert w.store.report(report["token"])["operation"]["error"]["code"] == "prime_trial_cleanup_unconfirmed"
    finally:
        await cleanup(c)
