"""Idle TV is a bounded device nomination beneath normal playback intent."""

import copy
import json
from datetime import timedelta

import pytest

from controller.entitlement_store import identity
from controller.planner import choose, idle_blocked, scheduled_choice
from test_fleet_allocation import NOW, rig as fleet_rig


def setup():
    fleet = fleet_rig({"one": ["event", "idle-a", "idle-b"]}, plan=())
    device = fleet.devices["one"]
    device["preferences"].update(idle_sports_enabled=True, minimum_viewing_seconds=300,
                                 switch_cooldown_seconds=300)
    device["idle_sports"] = {"state": "ready", "content_id": "idle-a", "expires_at": NOW.timestamp() + 60}
    device["intent_version"] = 1
    items = fleet.local["one"]
    for item in items:
        item["title"] = item["content_id"]
        if item["content_id"].startswith("idle-"):
            item.update(source="prime_idle", kind="broadcast", station_id=item["content_id"])
            item["snapshot"] = {"id": item["content_id"], "source": "prime_idle", "kind": "broadcast",
                                "title": item["title"], "prime_idle": {"station_id": item["station_id"]}}
    return device, items


def decide(device, items):
    return choose(device, items, NOW, NOW)


def idle_items(items):
    return [item for item in items if item["source"] == "prime_idle"]


def observe(device, content_id="idle-a", verified=True):
    device.update(observed={"content_id": content_id, "viewing_option_id": content_id, "verified": verified},
                  desired=content_id, playback_state="verified", started_at=NOW.isoformat(),
                  last_switch_at=NOW.isoformat())


def readiness(device, item, state="waiting_for_feed"):
    device["prime_network_scope"] = "direct"
    device["prime_access"] = {item["content_id"]: {
        "scope": "direct", "state": state, "options": item["viewing_options"],
        "expires_at": NOW.timestamp() + 120, "retry_after": (NOW + timedelta(seconds=60)).isoformat(),
        "reason": "Awaiting the scheduled feed",
    }}


def test_only_selected_idle_airing_is_admitted_without_any_priority_rule():
    device, items = setup()
    device["rules"] = []
    result = decide(device, items)
    assert result["content_id"] == "idle-a"
    assert result["manual"] is False and result["rule_id"] is None
    assert result["playback_origin"] == "idle_sports"
    device["idle_sports"]["content_id"] = "absent"
    device["rules"] = [{"id": "all", "name": "Everything"}]
    assert decide(device, idle_items(items))["content_id"] is None


@pytest.mark.parametrize("condition", ["disabled", "legacy", "expired", "bad_expiry", "pending", "inactive",
                                        "unplayable", "unknown_access", "missing_access", "stale", "unknown",
                                        "paused", "manual", "handoff", "exhausted", "retry", "locked"])
def test_idle_admission_fails_closed(condition):
    device, items = setup()
    items = idle_items(items)
    selected = items[0]
    if condition == "disabled":
        device["preferences"]["idle_sports_enabled"] = False
    elif condition == "legacy":
        device["preferences"].pop("idle_sports_enabled")
    elif condition in {"expired", "bad_expiry"}:
        device["idle_sports"]["expires_at"] = NOW.timestamp() if condition == "expired" else "later"
    elif condition == "pending":
        device["idle_sports"]["state"] = "checking"
    elif condition in {"inactive", "unplayable"}:
        selected["active" if condition == "inactive" else "playable"] = False
    elif condition == "unknown_access":
        selected["entitlement"] = {"status": "unknown"}
    elif condition == "missing_access":
        selected["entitlement"] = None
    elif condition == "stale":
        selected["lifecycle"]["stale"] = True
    elif condition == "unknown":
        selected["lifecycle"]["state"] = "unknown"
    elif condition == "paused":
        device["automation"] = "paused"
    elif condition in {"manual", "handoff"}:
        device["manual_control" if condition == "manual" else "input_handoff"] = {"session": "remote"}
    elif condition == "exhausted":
        device["event_attempts"] = {"idle-a": {"attempts": 2}}
    elif condition == "retry":
        device["failures"] = {"idle-a": {"retry_after": (NOW + timedelta(seconds=60)).isoformat()}}
    else:
        readiness(device, selected, "feeds_locked")
    assert decide(device, items)["content_id"] is None


@pytest.mark.parametrize("state", ["verified", "navigating", "unknown", "recovery"])
def test_scheduled_content_immediately_preempts_idle_policy_and_recovery(state):
    device, items = setup()
    observe(device)
    if state == "navigating":
        device["playback_state"] = "navigating"
    elif state == "unknown":
        idle_items(items)[0].update(playable=False, lifecycle={"state": "unknown"})
    elif state == "recovery":
        device["observed"]["verified"] = False
        device["recovery"] = {"content_id": "idle-a", "retry_after": (NOW + timedelta(seconds=60)).isoformat()}
    assert decide(device, items)["content_id"] == "event"


def test_due_manual_retry_and_unknown_commitment_block_idle_but_future_plan_does_not():
    device, items = setup()
    event = items[0]
    device["plan"] = [{"content_id": "event"}]
    readiness(device, event)
    result = decide(device, items)
    assert result["content_id"] == "event" and result["manual"] and idle_blocked(result)
    device["prime_access"] = {}
    event.update(playable=False, lifecycle={"state": "unknown"})
    assert decide(device, items)["content_id"] is None
    assert idle_blocked(scheduled_choice(device, items, NOW, NOW))
    event.update(start_time=(NOW + timedelta(hours=1)).isoformat(), lifecycle={"state": "scheduled"})
    assert decide(device, items)["content_id"] == "idle-a"


def test_automatic_upcoming_retry_yields_until_due_but_access_unknown_does_not():
    device, items = setup()
    readiness(device, items[0])
    assert decide(device, items)["content_id"] == "idle-a"
    device["prime_access"]["event"]["retry_after"] = NOW.isoformat()
    assert decide(device, items)["content_id"] == "event"
    readiness(device, items[0], "access_unknown")
    assert decide(device, items)["content_id"] == "event"


def test_automatic_upcoming_retry_tries_other_scheduled_content_before_idle():
    device, items = setup()
    readiness(device, items[0])
    other = {**copy.deepcopy(items[0]), "content_id": "other-event", "start_time": (NOW + timedelta(seconds=1)).isoformat()}
    assert decide(device, items + [other])["content_id"] == "other-event"


def test_unknown_verified_scheduled_playback_is_retained_without_idle_takeover():
    device, items = setup()
    observe(device, "event")
    items[0].update(playable=False, lifecycle={"state": "unknown", "stale": True})
    assert decide(device, items)["content_id"] == "event"


@pytest.mark.parametrize('state', ['ready', 'checking', 'unavailable'])
def test_expired_active_idle_attempt_is_retained_without_authorizing_a_launch(state):
    device, items = setup()
    items = idle_items(items)
    observe(device, verified=False)
    device['observed']['request_id'] = 'already-issued'
    device['idle_sports'].update(state=state, expires_at=NOW.timestamp() - 1)
    items[0].update(active=False, playable=False, lifecycle={'state': 'unknown', 'stale': True},
                    expected_end_time=(NOW + timedelta(minutes=10)).isoformat())
    result = decide(device, items)
    assert result['content_id'] == 'idle-a' and result['selection_only']
    device['observed'].pop('request_id')
    assert decide(device, items)['content_id'] is None


@pytest.mark.parametrize('condition', ['slot_ended', 'disabled', 'manual_finished', 'withdrawn', 'native_ended', 'failed'])
def test_expired_active_idle_hold_cannot_outlive_programme_or_explicit_withdrawal(condition):
    device, items = setup()
    items = idle_items(items)
    observe(device)
    device['observed']['request_id'] = 'already-issued'
    device['idle_sports']['expires_at'] = NOW.timestamp() - 1
    items[0]['expected_end_time'] = (NOW + timedelta(minutes=10)).isoformat()
    if condition == 'slot_ended': items[0]['expected_end_time'] = NOW.isoformat()
    if condition == 'disabled': device['preferences']['idle_sports_enabled'] = False
    if condition == 'manual_finished': device['manual_completions'] = {'idle-a': NOW.isoformat()}
    if condition == 'withdrawn': device['idle_sports'] = {'state': 'unavailable'}
    if condition == 'native_ended': items[0]['lifecycle'] = {'state': 'ended'}
    if condition == 'failed': device['playback_state'] = 'failed'
    assert decide(device, items)['content_id'] is None


def test_invalid_legacy_idle_watch_plan_entry_cannot_bypass_nomination():
    device, items = setup()
    device["plan"] = [{"content_id": "idle-b"}]
    assert decide(device, idle_items(items))["content_id"] == "idle-a"


def fleet_setup(same_station=True):
    fleet = fleet_rig({"one": ["idle-a", "idle-b"], "two": ["idle-a", "idle-b"]}, plan=())
    fleet.db.execute("CREATE TABLE contents(id TEXT PRIMARY KEY, snapshot TEXT)")
    for key, device in fleet.devices.items():
        device["preferences"]["idle_sports_enabled"] = True
        device["intent_version"] = 1
        device["idle_sports"] = {"state": "ready", "content_id": "idle-a" if key == "one" else "idle-b",
                                "expires_at": NOW.timestamp() + 60}
        for item in fleet.local[key]:
            station = "station" if same_station else item["content_id"]
            item.update(source="prime_idle", kind="broadcast", station_id=station)
            item["expected_end_time"] = (NOW + timedelta(hours=1)).isoformat()
            item["snapshot"] = {"id": item["content_id"], "source": "prime_idle", "kind": "broadcast",
                                "title": item["content_id"], "prime_idle": {"station_id": station}}
            fleet.db.execute("INSERT OR REPLACE INTO contents VALUES (?, ?)",
                             (item["content_id"], json.dumps(item["snapshot"])))
    return fleet


@pytest.mark.parametrize("order", [("one", "two"), ("two", "one")])
def test_fleet_reserves_at_most_one_airing_per_station(order):
    fleet = fleet_setup()
    result = {key: fleet.select(key)["content_id"] for key in order}
    assert result == {"one": "idle-a", "two": None}
    assert fleet.select("two", reserve=False)["content_id"] is None


@pytest.mark.parametrize("claim", ["observed", "pending", "physical", "assignment"])
def test_retained_station_claim_blocks_different_airing_id_even_if_old_row_not_projected(claim):
    fleet = fleet_setup()
    first = fleet.devices["one"]
    if claim == "observed":
        observe(first)
        first["automation"] = "paused"
    elif claim == "pending":
        fleet.db.execute("INSERT INTO jobs VALUES ('one', 'idle-a', 'pending')")
    elif claim == "physical":
        fleet.db.execute("INSERT INTO executor_jobs VALUES ('one','idle-a',1,NULL,0,'failed')")
    else:
        first["stream_assignment"] = {"content_id": "idle-a", "revision": 1}
    fleet.local["two"] = [item for item in fleet.local["two"] if item["content_id"] != "idle-a"]
    assert fleet.select("two")["content_id"] is None


def test_distinct_stations_fill_both_streams_and_preview_is_read_only():
    fleet = fleet_setup(same_station=False)
    before = copy.deepcopy(fleet.devices)
    preview = {key: fleet.select(key, reserve=False)["content_id"] for key in fleet.devices}
    assert preview == {"one": "idle-a", "two": "idle-b"}
    assert fleet.devices == before
    assert {key: fleet.select(key)["content_id"] for key in reversed(fleet.devices)} == preview


def test_scheduled_event_preempts_inflight_idle_catalogue_reservation():
    fleet = fleet_setup(same_station=False)
    device = fleet.devices["one"]
    item = fleet.local["one"][0]
    device["stream_assignment"] = {"content_id": "idle-a", "revision": 1, "catalogue": {
        "intent": 1, "expires": NOW.timestamp() + 60, "identity": identity(item)}}
    event = {**copy.deepcopy(item), "content_id": "event", "source": "teamarr", "kind": "event"}
    event.pop("snapshot")
    fleet.local["one"].append(event)
    assert fleet.select("one")["content_id"] == "event"


def test_fleet_does_not_reintroduce_deferred_scheduled_candidate_ahead_of_idle_nomination():
    fleet = fleet_setup(same_station=False)
    for key in fleet.devices:
        event = {**copy.deepcopy(fleet.local[key][0]), "content_id": "event", "source": "teamarr", "kind": "event"}
        event.pop("snapshot")
        fleet.local[key].append(event)
        readiness(fleet.devices[key], event)
    assert fleet.select("one")["content_id"] == "idle-a"
    assert fleet.select("two")["content_id"] == "idle-b"


def test_expired_idle_reservation_retains_only_issued_read_not_launch_eligibility():
    fleet = fleet_setup(same_station=False)
    device = fleet.devices["one"]
    item = fleet.local["one"][0]
    device["stream_assignment"] = {"content_id": "idle-a", "revision": 1, "catalogue": {
        "intent": 1, "expires": NOW.timestamp() + 60, "identity": identity(item)}}
    device["idle_sports"]["expires_at"] = NOW.timestamp()
    decision = fleet.select("one")
    assert decision["content_id"] == "idle-a" and decision["selection_only"] is True
    device["stream_assignment"]["catalogue"]["expires"] = NOW.timestamp()
    assert fleet.select("one")["content_id"] is None
