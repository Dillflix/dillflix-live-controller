import pytest
from playback_fixtures import payload
from test_prime_matching import GTI, UNAVAILABLE_BADGE, results, tile
from test_prime_network import CITY_POLICY, network_rig
from test_prime_workflow import Player, cleanup, rig, row

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


async def test_unknown_trial_does_not_authorize_network_fallback(tmp_path):
    c, w = network_rig(tmp_path)
    original = w.player.outcome
    def unknown():
        value = original()
        value.update(state="unknown", evidence={})
        return value
    w.player.outcome = unknown
    w.player.lost_response = True
    try:
        report, _ = w.store.submit(payload())
        await w.navigate(row(w, report["token"]))
        assert not w.player.transitions
        assert len([v for v in w.player.calls if v[0] == "play"]) == 1
        assert any(v[0] == "attempt" for v in w.player.calls)
    finally:
        await cleanup(c)


async def test_failed_trials_exhaust_locations_once_and_report_unavailable(tmp_path):
    c, w = network_rig(tmp_path)
    original_search, original_outcome = w.player.search, w.player.outcome
    async def search(*args):
        value = await original_search(*args)
        value["containers"][0]["items"][0]["availability_status"] = "UNAVAILABLE"
        return value
    def refused():
        value = original_outcome()
        value.update(state="failed", resolved_id=None,
                     evidence={"launch": {"disposition": "not_invoked"}})
        return value
    w.player.search, w.player.outcome = search, refused
    try:
        report, _ = w.store.submit(payload())
        await w.navigate(row(w, report["token"]))
        final = w.store.report(report["token"])
        assert final["operation"]["state"] == "feeds_unavailable"
        assert final["prime_player"]["network_acquisition"]["tried"] == ["direct", "calgary"]
        assert len(final["prime_player"]["unavailable_trials"]) == 2
        assert len([v for v in w.player.calls if v[0] == "play"]) == 2
    finally:
        await cleanup(c)


async def test_alternative_query_launches_on_current_network_without_failed_trial(tmp_path):
    c, w = network_rig(tmp_path)
    original_search = w.player.search
    searches = []

    async def search(*args):
        value = await original_search(*args)
        item = (tile(cid=GTI + "-unavailable", availability_status="UNAVAILABLE")
                if not searches else tile())
        searches.append(value["query"])
        return {**value, **results(item)}

    w.player.search = search
    w.player.outcome = lambda: {**Player.outcome(w.player), "network_context_id": w.player.context}
    try:
        report, _ = w.store.submit(payload())
        await w.navigate(row(w, report["token"]))
        final = w.store.report(report["token"])
        assert final["operation"]["state"] == "playing_verified"
        assert searches == ["Jets Lions", "New York Jets Detroit Lions"]
        assert not w.player.transitions
        assert [c[1] for c in w.player.calls if c[0] == "play"] == [GTI]
        assert not final["prime_player"].get("unavailable_trials")
    finally:
        await cleanup(c)


@pytest.mark.parametrize("network", [False, True])
async def test_launch_fallback_preserves_selected_query_and_catalogue(tmp_path, network):
    c, w = (network_rig if network else rig)(tmp_path)
    original_search = w.player.search
    responses = []

    async def search(*args):
        value = await original_search(*args)
        item = (tile(availability_status="UNAVAILABLE") if not responses
                else tile("Unrelated movie", cid=GTI + "-other", content_type="MOVIE"))
        value = {**value, **results(item), "search_id": str(len(responses) + 1)}
        responses.append(value)
        return value

    w.player.search = search
    try:
        request = payload()
        report, _ = w.store.submit(request)
        assert await w.select_and_launch(report["token"], request)
        saved = w.store.report(report["token"])["prime_player"]
        assert len(responses) == 2
        assert saved["query"] == responses[0]["query"]
        assert saved["catalogue"] == responses[0]
        assert saved["search_observed_at"] == responses[0]["observed_at"]
        assert saved["selected"]["content_id"] == GTI
        assert [c[1] for c in w.player.calls if c[0] == "play"] == [GTI]
    finally:
        await cleanup(c)
