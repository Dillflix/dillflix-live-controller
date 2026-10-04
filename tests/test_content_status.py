import asyncio
import copy
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_controller import command, overview, scenario

from controller.config import Settings
from controller.database import Database, encode
from controller.fixtures import fixtures
from controller.service import Controller


def card(service, content_id="demo:lions"):
    return next(e for e in service.overview()["events"] if e["content_id"] == content_id)


def report(request, state="live", **changes):
    now = datetime.now(UTC)
    return {
        "schema_version": 1,
        "request_id": request["request_id"],
        "content_id": request["content_id"],
        "observation": {
            "content_id": request["content_id"],
            "state": state,
            "source": "test_provider",
            "simulated": True,
            "timestamp_basis": "provider",
            "observed_at": now.isoformat(),
            "valid_until": (now + timedelta(seconds=120)).isoformat(),
            **changes,
        },
    }


def only_lions(service):
    with service.db.transaction() as db:
        d = service.db.device(db)
        d["plan"] = [service.entry("demo:lions")]
        service.db.save_device(db, d)


def stored(service, content_id="demo:lions"):
    with service.db.transaction() as db:
        return dict(db.execute("SELECT * FROM content_status WHERE content_id=?", (content_id,)).fetchone())


async def test_lookup_tracks_desired_observed_and_every_reservation_without_duplicates(rig, monkeypatch):
    _, s, _ = rig
    with s.db.transaction() as db:
        d = s.db.device(db)
        d["plan"] = [s.entry("demo:lions"), s.entry("demo:golf")]
        d["desired"] = "demo:lions"
        d["observed"] = {"content_id": "demo:redzone"}
        s.db.save_device(db, d)
        snapshots = {row["id"]: json.loads(row["snapshot"]) for row in db.execute("SELECT * FROM contents")}
    requests = []

    async def lookup(request):
        # Adapter calls must not hold the coordinator's write transaction.
        with s.db.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1
        requests.append(copy.deepcopy(request))
        return report(request)

    monkeypatch.setattr(s.status_adapter, "lookup", lookup)
    await s.refresh_status()
    assert {r["content_id"] for r in requests} == {"demo:lions", "demo:golf", "demo:redzone"}
    assert len(requests) == 3
    assert all(r["content_snapshot"] == snapshots[r["content_id"]] for r in requests)
    assert s.overview()["status_health"]["state"] == "ok"
    await s.refresh_status()
    assert len(requests) == 3  # Successful checks respect their independent cadence.


async def test_event_outside_discovery_remains_live_then_completes_from_lookup(rig, monkeypatch):
    c, s, _ = rig
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    with s.db.transaction() as db:
        entries = [
            json.loads(r[0]) for r in db.execute("SELECT snapshot FROM contents WHERE id!='demo:lions'")
        ]
        s.replace_catalog(db, entries, "demo")
    state = "live"

    async def lookup(request):
        return report(request, state if request["content_id"] == "demo:lions" else "scheduled")

    monkeypatch.setattr(s.status_adapter, "lookup", lookup)
    await s.refresh_status(force=True)
    s.tick()
    assert card(s)["active"] is False
    assert card(s)["lifecycle"]["state"] == "live"
    assert s.overview()["device"]["observed"]["content_id"] == "demo:lions"
    state = "ended"
    await s.refresh_status(force=True)
    s.tick()
    assert card(s)["lifecycle"]["state"] == "ended"
    assert s.overview()["device"]["observed"]["content_id"] == "demo:redzone"
    assert s.overview()["device"]["plan"][0]["content_id"] == "demo:lions"


async def test_failure_preserves_fresh_evidence_then_expires_without_completing(rig, monkeypatch):
    c, s, _ = rig
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    await s.refresh_status(force=True)
    before = stored(s)

    async def unavailable(_):
        raise ConnectionError("https://credentials-must-not-leak@example.test/secret")

    monkeypatch.setattr(s.status_adapter, "lookup", unavailable)
    await s.refresh_status(force=True)
    assert stored(s)["observation"] == before["observation"]
    assert stored(s)["last_success"] == before["last_success"]
    assert card(s)["playable"]
    with s.db.transaction() as db:
        old = json.loads(before["observation"])
        old.update(observed_at="2000-01-01T00:00:00Z", valid_until="2000-01-01T00:02:00Z")
        db.execute("UPDATE content_status SET observation=? WHERE content_id='demo:lions'", (encode(old),))
    s.tick()
    data = s.overview()
    assert card(s)["lifecycle"]["state"] == "unknown"
    assert card(s)["lifecycle"]["last_known_state"] == "live"
    assert card(s)["lifecycle"]["observed_at"] == old["observed_at"]
    assert data["device"]["observed"]["content_id"] == "demo:lions"
    assert data["device"]["plan"][0]["content_id"] == "demo:lions"
    assert data["device"]["reason"] == "Event status is stale; retaining verified playback"
    assert data["health"]["state"] == "ok"
    assert data["status_health"]["state"] == "degraded"
    assert "credentials-must-not-leak" not in encode(data)


async def test_teamarr_cached_lookup_does_not_refresh_provider_time_or_extend_expiry(tmp_path):
    s = Controller(Settings(database=str(tmp_path / "feed.db"), mode="teamarr", teamarr_url="http://teamarr"))
    entry = fixtures()[0][1]
    entry["status"] = "live"
    with s.db.transaction() as db:
        s.replace_catalog(db, [entry], "teamarr")
    only_lions(s)
    await s.refresh_status(force=True)
    first = card(s)["lifecycle"]
    await s.refresh_status(force=True)
    second = card(s)["lifecycle"]
    assert first["observed_at"] is second["observed_at"] is None
    assert first["received_at"] == second["received_at"]
    assert first["valid_until"] == second["valid_until"]
    assert second["timestamp_basis"] == "feed_received"
    with s.db.transaction() as db:
        db.execute("UPDATE contents SET active=0,seen_at='2000-01-01T00:00:00Z'")
        old = json.loads(db.execute("SELECT observation FROM content_status").fetchone()[0])
        old.update(received_at="2000-01-01T00:00:00Z", valid_until="2000-01-01T00:02:00Z")
        db.execute("UPDATE content_status SET observation=?", (encode(old),))
    await s.refresh_status(force=True)
    assert card(s)["lifecycle"]["state"] == "unknown"
    assert card(s)["active"] is False
    assert s.overview()["device"]["plan"]


async def test_provider_timestamp_and_expiry_are_preserved_across_restart(rig, monkeypatch):
    _, s, settings = rig
    only_lions(s)
    observed = datetime.now(UTC) - timedelta(seconds=10)
    until = observed + timedelta(hours=1)
    object.__setattr__(settings, "status_ttl", 30)

    async def lookup(request):
        return report(request, observed_at=observed.isoformat(), valid_until=until.isoformat())

    monkeypatch.setattr(s.status_adapter, "lookup", lookup)
    await s.refresh_status()
    await s.stop()
    restarted = Controller(settings)
    lifecycle = card(restarted)["lifecycle"]
    assert lifecycle["observed_at"] == observed.isoformat()
    assert lifecycle["valid_until"] == until.isoformat()
    assert lifecycle["effective_valid_until"] == (observed + timedelta(seconds=30)).isoformat()
    assert lifecycle["state"] == "live"


@pytest.mark.parametrize(
    "bad",
    [
        "request",
        "content",
        "identity",
        "future",
        "expired",
        "naive",
        "expiry",
        "state",
        "source",
        "missing_time",
    ],
)
async def test_invalid_or_expired_final_cannot_complete_a_commitment(rig, monkeypatch, bad):
    _, s, _ = rig
    only_lions(s)
    await s.refresh_status()

    async def lookup(request):
        result = report(request, "ended")
        obs = result["observation"]
        if bad == "request":
            result["request_id"] = "old-request"
        elif bad == "content":
            result["content_id"] = "another-event"
        elif bad == "identity":
            obs["content_id"] = "another-event"
        elif bad == "future":
            obs["observed_at"] = "2999-01-01T00:00:00Z"
        elif bad == "expired":
            obs.update(observed_at="2000-01-01T00:00:00Z", valid_until="2000-01-01T00:01:00Z")
        elif bad == "naive":
            obs["observed_at"] = "2026-09-30T12:00:00"
        elif bad == "expiry":
            obs["valid_until"] = None
        elif bad == "state":
            obs["state"] = "probably_over"
        elif bad == "source":
            obs["source"] = None
        elif bad == "missing_time":
            obs["observed_at"] = None
        return result

    monkeypatch.setattr(s.status_adapter, "lookup", lookup)
    await s.refresh_status(force=True)
    assert card(s)["lifecycle"]["state"] == "live"
    assert stored(s)["error"]
    assert s.overview()["device"]["plan"][0]["content_id"] == "demo:lions"


@pytest.mark.parametrize("result_kind", ["missing", "unknown", "older"])
async def test_no_result_unknown_and_older_evidence_are_distinct(rig, monkeypatch, result_kind):
    _, s, _ = rig
    only_lions(s)
    observed = datetime.now(UTC) - timedelta(seconds=2)

    async def initial(request):
        return report(request, observed_at=observed.isoformat())

    monkeypatch.setattr(s.status_adapter, "lookup", initial)
    await s.refresh_status()

    async def lookup(request):
        if result_kind == "missing":
            return None
        if result_kind == "unknown":
            return report(request, "unknown")
        return report(request, "ended", observed_at=(observed - timedelta(seconds=1)).isoformat())

    monkeypatch.setattr(s.status_adapter, "lookup", lookup)
    await s.refresh_status(force=True)
    assert card(s)["lifecycle"]["state"] == ("unknown" if result_kind == "unknown" else "live")
    assert bool(stored(s)["error"]) == (result_kind != "unknown")
    assert s.overview()["device"]["plan"]


async def test_timeout_is_bounded_and_other_pinned_events_still_refresh(rig, monkeypatch):
    c, s, settings = rig
    command(c, {"type": "add", "content_id": "demo:lions"})
    object.__setattr__(settings, "status_lookup_timeout", 0.01)

    async def lookup(request):
        if request["content_id"] == "demo:lions":
            await asyncio.Event().wait()
        return report(request, "scheduled")

    monkeypatch.setattr(s.status_adapter, "lookup", lookup)
    await asyncio.wait_for(s.refresh_status(), 1)
    assert stored(s)["error"] == "TimeoutError: status lookup failed"
    assert stored(s, "demo:canadiens")["last_success"]
    assert stored(s)["next_check"] > time.time()


async def test_late_response_cannot_overwrite_a_newer_request(rig, monkeypatch):
    _, s, _ = rig
    only_lions(s)
    started, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def lookup(request):
        requests.append(request)
        if len(requests) == 1:
            started.set()
            await release.wait()
            return report(request, "live")
        return report(request, "ended")

    monkeypatch.setattr(s.status_adapter, "lookup", lookup)
    first = asyncio.create_task(s.refresh_status())
    await asyncio.wait_for(started.wait(), 1)
    await s.refresh_status(force=True)
    release.set()
    await first
    assert card(s)["lifecycle"]["state"] == "ended"
    assert stored(s)["request_id"] == requests[1]["request_id"]


async def test_lease_loss_rejects_the_result(rig, monkeypatch):
    _, s, _ = rig
    only_lions(s)

    async def lookup(request):
        with s.db.transaction() as db:
            db.execute(
                "UPDATE leases SET owner='another-worker',expires=? WHERE resource='content-status'",
                (time.time() + 60,),
            )
        return report(request, "ended")

    monkeypatch.setattr(s.status_adapter, "lookup", lookup)
    await s.refresh_status()
    assert stored(s)["observation"] is None
    assert card(s)["lifecycle"]["state"] == "live"


async def test_terminal_evidence_survives_unknown_but_allows_explicit_correction(rig, monkeypatch):
    _, s, _ = rig
    only_lions(s)
    state = "ended"

    async def lookup(request):
        return report(request, state)

    monkeypatch.setattr(s.status_adapter, "lookup", lookup)
    await s.refresh_status()
    state = "unknown"
    await s.refresh_status(force=True)
    assert card(s)["lifecycle"]["state"] == "ended"
    state = "live"
    await s.refresh_status(force=True)
    assert card(s)["lifecycle"]["state"] == "live"


async def test_status_refresh_continues_while_automation_is_paused(rig):
    c, s, _ = rig
    d = overview(c)["device"]
    c.post(
        "/api/v1/devices/living-room/automation",
        json={"command_id": str(uuid4()), "expected_revision": d["revision"], "mode": "paused"},
    )
    await s.refresh_status()
    assert stored(s, "demo:canadiens")["last_success"]
    assert s.overview()["device"]["automation"] == "paused"


def test_v3_upgrade_preserves_device_jobs_and_catalog(rig):
    c, s, settings = rig
    s.tick()
    before = s.overview()["device"]
    before_jobs = c.get("/api/v1/devices/living-room/jobs").json()
    with s.db.transaction() as db:
        snapshots = [tuple(row) for row in db.execute("SELECT * FROM contents")]
        db.execute("DROP TABLE content_status")
        db.execute("PRAGMA user_version=3")
    restarted = Controller(settings)
    assert restarted.overview()["device"] == before
    assert c.get("/api/v1/devices/living-room/jobs").json() == before_jobs
    with restarted.db.transaction() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM contents")] == snapshots
        assert db.execute("PRAGMA user_version").fetchone()[0] == Database.SCHEMA_VERSION


def test_outside_feed_demo_reaches_completion_without_losing_the_plan(rig):
    c, s, _ = rig
    scenario(c, "outside_feed")
    s.tick()
    assert card(s, "demo:canadiens")["active"] is False
    assert s.overview()["device"]["observed"]["content_id"] == "demo:canadiens"
    c.post("/api/v1/simulation", json={"action": "advance", "minutes": 120})
    s.tick()
    assert card(s, "demo:canadiens")["lifecycle"]["state"] == "ended"
    assert s.overview()["device"]["plan"][0]["content_id"] == "demo:canadiens"


@pytest.mark.parametrize('pinned', [False, True])
@pytest.mark.parametrize('feed_age, expected', [(30, 'live'), (180, 'unknown')])
def test_expired_feed_copy_does_not_change_eligibility_when_selected(pinned, feed_age, expected):
    from types import SimpleNamespace

    from controller.content_status import ContentStatusCoordinator, source_observation

    service = ContentStatusCoordinator()
    service.settings = SimpleNamespace(mode="teamarr", status_ttl=120)
    now = datetime.now(UTC)
    snapshot = {'id': 'event:test', 'status': 'live'}
    old = source_observation(snapshot, (now - timedelta(seconds=240)).isoformat(),
                             now, now, 'teamarr', 120)
    old.update(source='teamarr_feed', simulated=False)
    row = {'snapshot': json.dumps(snapshot), 'seen_at': (now - timedelta(seconds=feed_age)).isoformat()}
    check = {'observation': json.dumps(old), 'error': None, 'last_attempt': None,
             'last_success': None, 'next_check': 0}
    result = service.content_lifecycle(row, check, now, now, pinned)
    assert result['state'] == expected
    assert result['received_at'] == row['seen_at']
    old['state'] = 'ended'
    check['observation'] = json.dumps(old)
    assert service.content_lifecycle(row, check, now, now, pinned)['state'] == 'ended'
