"""Shared allocation fills constrained streams without stealing retained work."""

import copy
import itertools
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from controller.fleet import select_for_stream
from controller.entitlement_store import identity


NOW = datetime(2026, 10, 25, 18, tzinfo=UTC)


def rig(capabilities, plan=("a", "b", "c", "d")):
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE jobs(device_id TEXT, content_id TEXT, state TEXT);
        CREATE TABLE executor_jobs(device_id TEXT, content_id TEXT, touched_device INTEGER,
          retired_at TEXT, cancel_requested INTEGER, state TEXT);
    """)
    devices = {key: {"id": key, "revision": 1, "automation": "active", "playback_state": "waiting",
        "observed": None, "desired": None, "plan": [{"content_id": cid} for cid in plan],
        "rules": [{"id": "all", "name": "All sports"}], "team_ranks": {}, "preferences": {
            "minimum_viewing_seconds": 0, "switch_cooldown_seconds": 0, "same_tier_switching": False,
        }} for key in capabilities}
    all_ids = sorted(set().union(*map(set, capabilities.values())))
    local = {key: [{"content_id": cid, "active": True, "playable": True,
        "league": "test", "phase": "regular", "source": "test", "kind": "event", "teams": [],
        "start_time": NOW.isoformat(), "lifecycle": {"state": "live"}, "viewing_options": [{"id": cid}],
        "entitlement": {"status": "entitled" if cid in allowed else "unentitled"},
    } for cid in all_ids] for key, allowed in capabilities.items()}

    class Database:
        saves = 0

        def device(self, _db, key):
            return copy.deepcopy(devices[key])

        def save_device(self, _db, device):
            self.saves += 1
            devices[device["id"]] = copy.deepcopy(device)

    database = Database()
    root = SimpleNamespace(runtimes={})
    for key in devices:
        root.runtimes[key] = SimpleNamespace(root=root, db=database,
            items=lambda _db, device_id, key=key: copy.deepcopy(local[device_id]))

    def select(key, reserve=True):
        runtime = root.runtimes[key]
        return select_for_stream(runtime, connection, database.device(connection, key),
                                 runtime.items(connection, key), NOW, NOW, reserve=reserve)

    return SimpleNamespace(select=select, devices=devices, db=connection, database=database, local=local, root=root)


@pytest.mark.parametrize("order", list(itertools.permutations(("one", "two"))))
def test_flexible_stream_leaves_only_eligible_event_for_constrained_stream(order):
    fleet = rig({"one": "ab", "two": "a"})
    result = {key: fleet.select(key)["content_id"] for key in order}
    assert result == {"one": "b", "two": "a"}
    assert {key: fleet.select(key)["content_id"] for key in reversed(order)} == result


@pytest.mark.parametrize("order", list(itertools.permutations(("one", "two", "three"))))
def test_three_stream_matching_is_full_and_independent_of_tick_order(order):
    fleet = rig({"one": "ab", "two": "ac", "three": "ac"})
    result = {key: fleet.select(key)["content_id"] for key in order}
    assert result == {"one": "b", "two": "a", "three": "c"}


def test_selection_preview_matches_reservation_without_writes():
    fleet = rig({"one": "ab", "two": "ac", "three": "ac"})
    before = copy.deepcopy(fleet.devices)
    preview = {key: fleet.select(key, reserve=False)["content_id"] for key in fleet.devices}
    assert fleet.devices == before and fleet.database.saves == 0
    assert {key: fleet.select(key)["content_id"] for key in reversed(fleet.devices)} == preview


def test_shared_plan_priority_wins_and_one_tick_reserves_all_idle_streams():
    fleet = rig({"one": "abcd", "two": "abcd"}, plan=("c", "a", "b", "d"))
    assert fleet.select("two")["content_id"] == "a"
    assert fleet.devices["one"]["stream_assignment"]["content_id"] == "c"
    assert fleet.devices["two"]["stream_assignment"]["content_id"] == "a"
    assert fleet.select("one")["content_id"] == "c"


@pytest.mark.parametrize("change", ["unchanged", "status_unknown", "ended", "cancelled", "delayed",
                                    "expired", "revision", "intent", "route", "denied", "paused", "manual"])
def test_catalogue_reservation_is_bounded_and_never_overrides_withdrawal(change):
    fleet = rig({"one": "ab", "two": "ab"}, plan=("a",))
    device = fleet.devices["one"]
    device["intent_version"] = 3
    item = fleet.local["one"][0]
    item["snapshot"] = {"id": "a", "kind": "event", "title": "A"}
    device["stream_assignment"] = {"content_id": "a", "revision": 1, "catalogue": {
        "id": "probe", "intent": 3, "expires": NOW.timestamp() + 60, "identity": identity(item)}}
    if change in {"status_unknown", "ended", "cancelled", "delayed"}:
        item.update(playable=False, lifecycle={"state": "unknown" if change == "status_unknown" else change})
    elif change == "expired":
        device["stream_assignment"]["catalogue"]["expires"] = NOW.timestamp()
    elif change == "revision":
        device["revision"] += 1
    elif change == "intent":
        device["intent_version"] += 1
    elif change == "route":
        item["viewing_options"] = [{"id": "replacement"}]
    elif change == "denied":
        item["entitlement"]["status"] = "unentitled"
    elif change == "paused":
        device["automation"] = "paused"
    elif change == "manual":
        device["manual_control"] = {"session": "remote"}
    from controller.fleet import catalogue_reservation
    held = catalogue_reservation(device, fleet.local["one"], NOW)
    assert held == ("a" if change in {"unchanged", "status_unknown"} else None)
    if held:
        assert fleet.select("one")["content_id"] == "a"
        assert fleet.select("two")["content_id"] == "b"
        assert fleet.devices["one"]["stream_assignment"]["catalogue"]["id"] == "probe"


@pytest.mark.parametrize("claim", ["observed", "pending", "physical", "assignment"])
def test_existing_claims_are_not_reassigned_to_fill_another_stream(claim):
    fleet = rig({"one": "ab", "two": "a"})
    first = fleet.devices["one"]
    if claim == "observed":
        first.update(observed={"content_id": "a", "verified": True}, playback_state="verified", automation="paused")
    elif claim == "pending":
        fleet.db.execute("INSERT INTO jobs VALUES ('one', 'a', 'pending')")
    elif claim == "physical":
        fleet.db.execute("INSERT INTO executor_jobs VALUES ('one','a',1,NULL,0,'failed')")
    else:
        first["stream_assignment"] = {"content_id": "a", "revision": 1}
    assert fleet.select("two")["content_id"] is None


def test_stale_reservations_and_unavailable_streams_do_not_take_new_capacity():
    fleet = rig({"one": "ab", "two": "a", "three": "ab"})
    fleet.devices["one"]["stream_assignment"] = {"content_id": "a", "revision": 0}
    fleet.devices["three"]["executor_health"] = {"state": "offline"}
    assert fleet.select("one")["content_id"] == "b"
    assert fleet.select("two")["content_id"] == "a"
    assert "stream_assignment" not in fleet.devices["three"]


def test_automatic_priorities_and_local_access_failures_remain_authoritative():
    fleet = rig({"one": "ab", "two": "ab"}, plan=())
    fleet.devices["one"]["prime_network_scope"] = "direct"
    fleet.devices["one"]["prime_access"] = {"a": {"scope": "direct", "state": "feeds_locked",
        "options": [{"id": "a"}]}}
    assert fleet.select("one")["content_id"] == "b"
    assert fleet.select("two")["content_id"] == "a"


@pytest.mark.parametrize("unavailable", ["player_recovery", "input_handoff", "stopped_executor"])
def test_unusable_stream_does_not_reserve_new_work_but_retains_physical_claim(unavailable):
    fleet = rig({"one": "ab", "two": "a"})
    if unavailable == "stopped_executor":
        fleet.root.runtimes["two"].executor = SimpleNamespace(stopping=True)
    else:
        fleet.devices["two"][unavailable] = {"reason": "Recovering"}
    fleet.devices["two"]["stream_assignment"] = {"content_id": "a", "revision": 1}
    assert fleet.select("one", reserve=False)["content_id"] == "a"
    # A physically touched attempt must still retire before another stream may take it.
    fleet.db.execute("INSERT INTO executor_jobs VALUES ('two','a',1,NULL,0,'failed')")
    assert fleet.select("one", reserve=False)["content_id"] == "b"
    fleet.db.execute("DELETE FROM executor_jobs")
    fleet.devices["two"].pop("player_recovery", None)
    fleet.devices["two"].pop("input_handoff", None)
    fleet.root.runtimes["two"].executor = None
    fleet.devices["two"].pop("stream_assignment")
    assert fleet.select("one")["content_id"] == "b"
    assert fleet.select("two")["content_id"] == "a"
