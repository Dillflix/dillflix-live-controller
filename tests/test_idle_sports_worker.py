"""Prime catalogue worker fences nominations without touching playback or feeds."""

import asyncio
import copy
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import controller.idle_sports as idle
from controller.database import Database
from controller.executor.models import ExecutorError
from controller.planner import content_view
from controller.prime_player.catalogue import CatalogueChecks
from test_idle_sports_chooser import candidate as sample_candidate


NOW = datetime(2026, 11, 23, 23, tzinfo=UTC)


def candidate(**changes):
    value = sample_candidate(**changes)
    value["candidate_id"] = idle.candidate_id(value)
    return value


def catalogue(item=None, **changes):
    item = item or candidate()
    return {"complete": True, "session_id": "session", "generation": 1, "network_context_id": "direct",
            "observed_at": NOW.isoformat(), "catalogue_coverage": {"partial": True},
            "station_summary": {"schema": "live_tv_epg", "stations": [{
                "station_id": item["station_id"], "name": item["station_name"],
                "entitlement_status": item["entitlement_status"], "has_watch_action": item["has_watch_action"],
                "airings": [item]}]}, **changes}


class MemoryDatabase:
    device = staticmethod(Database.device)
    save_device = staticmethod(Database.save_device)
    set_meta = staticmethod(Database.set_meta)
    meta = staticmethod(Database.meta)

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.writing = False
        self.connection.executescript("""
            CREATE TABLE devices(id TEXT PRIMARY KEY,revision INTEGER,payload TEXT);
            CREATE TABLE contents(id TEXT PRIMARY KEY,source TEXT,active INTEGER,seen_at TEXT,snapshot TEXT);
            CREATE TABLE jobs(device_id TEXT,content_id TEXT,state TEXT);
            CREATE TABLE executor_jobs(device_id TEXT,content_id TEXT,touched_device INTEGER,
                retired_at TEXT,cancel_requested INTEGER,state TEXT,report TEXT DEFAULT '{}');
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE content_status(content_id TEXT PRIMARY KEY,observation TEXT);
            CREATE TABLE team_directory(key TEXT PRIMARY KEY,payload TEXT);
        """)

    @contextmanager
    def snapshot(self):
        assert not self.writing, "A publish must read its own writer snapshot"
        yield self.connection

    @contextmanager
    def transaction(self):
        assert not self.writing
        self.writing = True
        try:
            with self.connection:
                yield self.connection
        finally:
            self.writing = False


class Player:
    def __init__(self):
        self.state = {"serial": "tv", "session_id": "session", "ownership": {"epoch": 1},
                      "compatibility": {"generation": 1}, "network": {"enabled": True, "context_id": "direct"}}
        self.result = catalogue()
        self.calls = []
        self.health_calls = 0

    async def health(self):
        self.health_calls += 1
        return copy.deepcopy(self.state)

    def require(self, health, capability):
        assert capability == "linear_catalog"

    def ownership(self, health, *, automatic):
        assert automatic
        return health["ownership"]

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        assert method == "linear_catalog", "Idle discovery must never navigate or play"
        return copy.deepcopy(self.result)


class Worker(idle.IdleSports):
    def __init__(self, database, root, device_id):
        self.db, self.root, self.device_id = database, root, device_id
        self.settings = SimpleNamespace(screen_adb_serial="tv")
        self.executor = SimpleNamespace(input_lock=asyncio.Lock(), player=Player(), matcher=object(),
            model_captures=SimpleNamespace(record=lambda value: None),
            require_catalogue_generation=CatalogueChecks.require_catalogue_generation)

    async def playback_work(self, function, *args):
        return function(*args)

    def entitlement_blocker(self, db, device, *, device_read=True):
        return bool(device.get("manual_control") or device.get("input_handoff") or device.get("player_recovery")
                    or device.get("playback_state") in {"navigating", "unverified"}
                    or device_read and self.executor.input_lock.locked())

    def now(self, db):
        return NOW

    def planning_items(self, db, device_id=None):
        result = []
        for row in db.execute("SELECT * FROM contents"):
            snapshot = json.loads(row["snapshot"])
            state = idle.lifecycle(snapshot, NOW, False) if row["source"] == "prime_idle" else {
                "state": snapshot.get("status", "live"), "stale": False}
            result.append({**content_view(snapshot, row["active"], state), "entitlement": {"status": "entitled"}})
        return result


@pytest.fixture
def worker(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(idle, "datetime", Clock)
    monkeypatch.setattr(idle, "time", SimpleNamespace(time=lambda: NOW.timestamp()))
    database = MemoryDatabase()
    root = SimpleNamespace(runtimes={})
    for device_id in ("one", "two"):
        device = {"id": device_id, "revision": 1, "intent_version": 1, "automation": "active",
                  "playback_state": "waiting", "observed": None, "desired": None, "plan": [],
                  "rules": [{"id": "all", "name": "Sports"}], "team_ranks": {}, "preferences": {
                      "idle_sports_enabled": True, "minimum_viewing_seconds": 300,
                      "switch_cooldown_seconds": 60, "same_tier_switching": False}}
        database.save_device(database.connection, device)
        root.runtimes[device_id] = Worker(database, root, device_id)
    value = root.runtimes["one"]
    value.model_calls = []

    async def choose(candidates, **kwargs):
        value.model_calls.append((copy.deepcopy(candidates), copy.deepcopy({k: v for k, v in kwargs.items() if k != "matcher"})))
        return (candidates[0], {"match_status": "matched", "reason": "Hockey coverage"}) if candidates else (
            None, {"match_status": "uncertain", "reason": "No candidates"})

    monkeypatch.setattr(idle, "choose", choose)
    yield value
    database.connection.close()


def change(worker, **fields):
    with worker.db.transaction() as db:
        device = worker.db.device(db, worker.device_id)
        device.update(fields)
        worker.db.save_device(db, device)


def state(worker):
    return worker.db.device(worker.db.connection, worker.device_id).get("idle_sports", {})


def insert_event(worker):
    snapshot = {"id": "scheduled", "source": "games", "kind": "event", "title": "Scheduled sports",
                "status": "live", "start_time": NOW.isoformat(), "viewing_options": [{"id": "route", "app": "prime_video"}]}
    worker.db.connection.execute("INSERT INTO contents VALUES (?,?,?,?,?)", ("scheduled", "teamarr", 1,
                                NOW.isoformat(), json.dumps(snapshot)))


def test_partial_catalogue_admits_positive_current_rows_and_rejects_conflicting_station():
    result = catalogue()
    assert idle.catalogue_candidates(result, NOW) == [candidate()]
    duplicate = copy.deepcopy(result["station_summary"]["stations"][0])
    duplicate["entitlement_status"] = "UNENTITLED"
    result["station_summary"]["stations"].append(duplicate)
    assert idle.catalogue_candidates(result, NOW) == []


@pytest.mark.parametrize("change", [{"complete": False}, {"observed_at": (NOW - timedelta(seconds=60)).isoformat()},
                                    {"observed_at": (NOW + timedelta(seconds=1)).isoformat()}])
def test_incomplete_or_stale_read_grants_nothing(change):
    assert idle.catalogue_candidates(catalogue(**change), NOW) == []


def test_airing_expiry_withdraws_eligibility_without_completion():
    item = candidate(ends_at=(NOW + timedelta(seconds=10)).isoformat())
    assert idle.expires_at(item) == NOW.timestamp() + 10
    view = idle.lifecycle(idle.content_snapshot(item), NOW + timedelta(seconds=10), True)
    assert view["state"] == "unknown" and view["stale"]
    assert view["last_known_state"] == "live"


def test_peer_scheduled_event_native_mapping_excludes_same_idle_event(worker):
    insert_event(worker)
    native_id = "amzn1.dv.gti.already-playing"
    worker.db.connection.execute(
        "INSERT INTO executor_jobs VALUES (?,?,1,NULL,0,'playing_verified',?)",
        ("two", "scheduled", json.dumps({"prime_player": {"selected": {"content_id": native_id}}})),
    )
    context = worker.idle_context()
    assert context is not None
    assert any(peer.get("content_id") == native_id for peer in context["peers"])


async def test_worker_passes_actual_viewing_dwell_to_model_after_metadata_change(worker):
    current = candidate()
    assert worker.idle_publish(worker.idle_context(), current, {}, {})
    nomination = state(worker)
    nomination["next_check_at"] = 0
    nomination["candidate"]["synopsis"] = "Previously described hockey coverage"
    change(worker, idle_sports=nomination, observed={"content_id": current["candidate_id"]},
           playback_state="verified", started_at=(NOW - timedelta(seconds=15)).isoformat(),
           last_switch_at=(NOW - timedelta(seconds=15)).isoformat())
    await worker.refresh_idle_sports()
    assert worker.model_calls[-1][1]["allow_switch"] is False


async def test_refresh_publishes_native_identity_with_no_playback_or_feed_replacement(worker):
    worker.db.set_meta(worker.db.connection, "feed_health", {"state": "healthy", "source": "teamarr"})
    assert await worker.refresh_idle_sports()
    nomination = state(worker)
    assert nomination["state"] == "ready" and nomination["content_id"] == candidate()["candidate_id"]
    assert nomination["expires_at"] == NOW.timestamp() + 60
    assert worker.db.meta(worker.db.connection, "feed_health")["source"] == "teamarr"
    assert worker.db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert [call[0] for call in worker.executor.player.calls] == ["linear_catalog"]
    assert worker.executor.player.calls[0][1]["facet"] == "sports"
    assert worker.executor.player.calls[0][1]["max_pages"] == 3
    assert not await worker.refresh_idle_sports()  # Enforced persisted pacing.


async def test_no_selection_widens_next_scan_and_preserves_depth_for_renewal(worker, monkeypatch):
    original = idle.choose
    async def no_match(*args, **kwargs):
        return None, {"match_status": "no_match"}
    monkeypatch.setattr(idle, "choose", no_match)
    assert await worker.refresh_idle_sports()
    assert state(worker)["catalogue_max_pages"] == 8
    monkeypatch.setattr(idle, "choose", original)
    for _ in range(2):
        change(worker, idle_sports={**state(worker), "next_check_at": 0})
        assert await worker.refresh_idle_sports()
        assert state(worker)["state"] == "ready"
    assert [params["max_pages"] for _, params in worker.executor.player.calls] == [3, 8, 8]


def test_catalogue_timeout_is_visible_without_claiming_an_empty_catalogue(worker):
    worker.idle_failure(worker.idle_context(), ExecutorError("prime_linear_catalog_timeout", "private raw error"))
    value = state(worker)
    assert value["state"] == "unavailable"
    assert value["error_code"] == "prime_linear_catalog_timeout"
    assert value["reason"] == "Prime sports catalogue timed out; retrying automatically"
    assert "private" not in json.dumps(value)


def test_idle_audit_retains_specific_chooser_validation_reason(worker):
    error = ExecutorError('idle_sports_selection_invalid', 'Private model output must not become the audit')
    error.validation_reason = 'invented_current_program'
    worker.idle_failure(worker.idle_context(), error)
    audit = worker.db.meta(worker.db.connection, 'idle_sports_audit:' + worker.device_id)
    assert audit['error'] == {'code': 'idle_sports_selection_invalid', 'validation_reason': 'invented_current_program'}
    assert 'Private model output' not in json.dumps(audit)


@pytest.mark.parametrize("mutation", ["intent", "manual", "paused", "network", "schedule"])
def test_publish_rechecks_intent_and_new_scheduled_work(worker, mutation):
    context = worker.idle_context()
    if mutation == "intent":
        change(worker, intent_version=2)
    elif mutation == "manual":
        change(worker, manual_control={"session": "remote"})
    elif mutation == "paused":
        change(worker, automation="paused")
    elif mutation == "network":
        change(worker, prime_network_scope={"context_id": "new"})
    else:
        insert_event(worker)
    assert not worker.idle_publish(context, candidate(), {}, {})
    assert not state(worker)


def test_simultaneous_models_cannot_publish_same_station_before_playback_assignment(worker):
    peer = worker.root.runtimes["two"]
    first_context, second_context = worker.idle_context(), peer.idle_context()
    assert worker.idle_publish(first_context, candidate(), {}, {})
    other_airing = candidate(airing_id="amzn1.pv.linear.airing.other")
    assert not peer.idle_publish(second_context, other_airing, {}, {})
    assert not state(peer)


def test_publish_rechecks_airing_expiry_and_exhausted_attempts(worker):
    context = worker.idle_context()
    assert not worker.idle_publish(context, candidate(observed_at=(NOW - timedelta(seconds=60)).isoformat()), {}, {})
    change(worker, event_attempts={candidate()["candidate_id"]: {"attempts": 2}})
    assert not worker.idle_publish(context, candidate(), {}, {})


def test_confirmed_native_event_end_blocks_stale_guide_but_station_signal_does_not(worker):
    event = candidate(kind="event", content_type="EVENT", event_state="LIVE", content_id="amzn1.dv.gti.event",
                      station_id=None, airing_id=None)
    ended = json.dumps({"source": "prime_player", "state": "ended", "evidence": {"decision": "confirmed"}})
    for item in (event, candidate()):
        worker.db.connection.execute("INSERT INTO content_status VALUES (?,?)", (item["candidate_id"], ended))
    context = worker.idle_context()
    assert worker.idle_filter_candidates(context, [event, candidate()]) == [candidate()]
    change(worker, manual_completions={candidate()["candidate_id"]: NOW.isoformat()})
    assert worker.idle_filter_candidates(worker.idle_context(), [candidate()]) == []


def test_failure_backoff_persists_without_renewing_valid_evidence(worker):
    context = worker.idle_context()
    assert worker.idle_publish(context, candidate(), {}, {})
    old_expiry = state(worker)["expires_at"]
    for _ in range(3):
        worker.idle_failure(context, RuntimeError("private raw error"))
    value = state(worker)
    assert value["state"] == "ready" and value["expires_at"] == old_expiry
    assert value["next_check_at"] == NOW.timestamp() + 80
    assert value["last_error"] == "RuntimeError" and "private" not in json.dumps(value)
    audit = worker.db.meta(worker.db.connection, "idle_sports_audit:" + worker.device_id)
    assert audit["error"]["code"] == "RuntimeError" and "private" not in json.dumps(audit)
    assert worker.idle_context() is None
    worker.idle_failure(context, ExecutorError("prime_stale_result", "Session replaced"))
    assert state(worker)["state"] == "unavailable" and state(worker)["expires_at"] == 0


def test_uncertain_result_preserves_only_remaining_valid_window(worker):
    context = worker.idle_context()
    assert worker.idle_publish(context, candidate(), {}, {})
    assert worker.idle_publish(context, None, {"match_status": "uncertain"}, {})
    assert state(worker)["state"] == "ready" and state(worker)["expires_at"] == NOW.timestamp() + 60
    assert worker.idle_publish(context, None, {"match_status": "no_match"}, {})
    assert state(worker)["state"] == "unavailable"


@pytest.mark.parametrize("field", ["session", "generation", "network", "ownership"])
async def test_model_latency_cannot_publish_replaced_runtime_or_ownership(worker, monkeypatch, field):
    async def delayed(candidates, **kwargs):
        health = worker.executor.player.state
        if field == "session":
            health["session_id"] = "replacement"
        elif field == "generation":
            health["compatibility"]["generation"] = 2
        elif field == "network":
            health["network"]["context_id"] = "elsewhere"
        else:
            health["ownership"]["epoch"] = 2
        return candidates[0], {"match_status": "matched"}

    monkeypatch.setattr(idle, "choose", delayed)
    assert not await worker.refresh_idle_sports()
    assert state(worker)["state"] == "unavailable"
    assert worker.db.connection.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 0


async def test_old_session_catalogue_never_reaches_model(worker):
    worker.executor.player.result["session_id"] = "prior-session"
    assert not await worker.refresh_idle_sports()
    assert not worker.model_calls


async def test_scheduled_work_arriving_during_model_wins_before_another_player_read(worker, monkeypatch):
    async def delayed(candidates, **kwargs):
        insert_event(worker)
        return candidates[0], {"match_status": "matched"}

    monkeypatch.setattr(idle, "choose", delayed)
    assert not await worker.refresh_idle_sports()
    assert worker.executor.player.health_calls == 2
    assert not state(worker).get("content_id")


async def test_model_receives_referenced_team_names_only(worker):
    change(worker, team_ranks={"nhl": ["espn:nhl:tor"]})
    for key, name in (("espn:nhl:tor", "Toronto Maple Leafs"), ("espn:nhl:other", "Unranked team")):
        worker.db.connection.execute("INSERT INTO team_directory VALUES (?,?)",
            (key, json.dumps({"key": key, "league": "nhl", "full_name": name, "logo": "irrelevant"})))
    assert await worker.refresh_idle_sports()
    teams = worker.model_calls[0][1]["preferences"]["teams"]
    assert teams == [{"key": "espn:nhl:tor", "league": "nhl", "full_name": "Toronto Maple Leafs"}]


async def test_unchanged_current_program_renews_without_model_churn(worker):
    assert await worker.refresh_idle_sports()
    value = state(worker)
    value["next_check_at"] = 0
    value["selected_at"] = NOW.timestamp() - 5000  # Long playback is not a reason to call the model every tick.
    change(worker, idle_sports=value)
    assert await worker.refresh_idle_sports()
    assert len(worker.model_calls) == 1
    assert worker.db.meta(worker.db.connection, "idle_sports_audit:one")["method"] == "renewal"


async def test_settings_change_reclassifies_current_program_before_renewing(worker):
    assert await worker.refresh_idle_sports()
    previous = state(worker)
    previous["next_check_at"] = 0
    change(worker, idle_sports=previous, rules=[{"id": "new", "name": "New sports interests"}])
    assert await worker.refresh_idle_sports()
    assert len(worker.model_calls) == 2
    assert state(worker)["preferences_digest"] != previous["preferences_digest"]
    assert state(worker)["selected_at"] == previous["selected_at"]


def test_expiry_withdraws_only_old_idle_rows_without_completing_or_deleting_them(worker):
    insert_event(worker)
    old = candidate(observed_at=(NOW - timedelta(seconds=60)).isoformat())
    fresh = candidate(airing_id="amzn1.pv.linear.airing.fresh")
    for item in (old, fresh):
        snapshot = idle.content_snapshot(item)
        worker.db.connection.execute("INSERT INTO contents VALUES (?,?,?,?,?)",
            (item["candidate_id"], "prime_idle", 1, item["observed_at"], json.dumps(snapshot)))
    worker.expire_idle_rows()
    active = dict(worker.db.connection.execute("SELECT id,active FROM contents"))
    assert active == {"scheduled": 1, old["candidate_id"]: 0, fresh["candidate_id"]: 1}
    assert worker.db.connection.execute("SELECT COUNT(*) FROM content_status").fetchone()[0] == 0


def test_expiry_preserves_another_streams_still_fresh_nomination(worker):
    old = candidate(observed_at=(NOW - timedelta(seconds=60)).isoformat())
    worker.db.connection.execute("INSERT INTO contents VALUES (?,?,?,?,?)", (old["candidate_id"], "prime_idle", 1,
        old["observed_at"], json.dumps(idle.content_snapshot(old))))
    change(worker.root.runtimes["two"], idle_sports={"state": "ready", "content_id": old["candidate_id"],
                                                   "expires_at": NOW.timestamp() + 30})
    worker.expire_idle_rows()
    assert worker.db.connection.execute("SELECT active FROM contents").fetchone()[0] == 1


def test_late_older_catalogue_cannot_replace_a_newer_shared_row(worker):
    original = candidate()
    assert worker.idle_publish(worker.idle_context(), original, {}, {})
    change(worker, idle_sports={})
    older = candidate(observed_at=(NOW - timedelta(seconds=5)).isoformat())
    assert worker.idle_publish(worker.idle_context(), older, {}, {})
    snapshot = json.loads(worker.db.connection.execute("SELECT snapshot FROM contents").fetchone()[0])
    assert snapshot["prime_idle"]["observed_at"] == NOW.isoformat()
