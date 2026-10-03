import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from playback_fixtures import payload
from test_prime_matching import GTI, Model, results, tile
from test_prime_workflow import Player, cleanup, row
from test_prime_workflow import rig as prime_rig

from controller.executor.config import ExecutorConfig
from controller.executor.models import ExecutorError, PlaybackReport
from controller.models import PageRefreshCommand, PrimeDiscovery
from controller.prime_player.discovery import DiscoverySelector


def discovery_request(intent=2):
    return dict(
        schema_version=2,
        request_id=uuid4().hex,
        device_id="living-room",
        intent_version=intent,
        content_id=None,
        mode="live",
        purpose="discovery",
        discovery={**PrimeDiscovery().model_dump(), "enabled_pages": ["sports", "dazn"]},
        interests={"priorities": ["live tennis"]},
        deadline_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )


class DiscoveryPlayer(Player):
    async def health(self):
        health = await super().health()
        health["api_version"] = 6
        health["capabilities"] += ["pages", "discover"]
        health["compatibility"]["capabilities"].update(
            pages={"available": True}, discover={"available": True}
        )
        return health

    async def discover(self, settings, ownership):
        self.calls.append(("discover", settings["enabled_pages"]))
        self.search_entered.set()
        if self.search_release:
            await self.search_release.wait()
        return {
            **results(
                tile(identity_status="structural_slot_correlation", occurrences=[{"page_id": "sports"}])
            ),
            "source": "discover",
            "pages": settings["enabled_pages"],
            "session_id": self.session,
            "search_id": "discovery-1",
            "generation": 1,
            "observed_at": datetime.now(UTC).isoformat(),
        }

    async def pages(self, ownership):
        self.calls.append(("pages",))
        return {
            "session_id": self.session,
            "observed_at": datetime.now(UTC).isoformat(),
            "pages": [{"id": p, "title": p, "available": True} for p in ["sports", "dazn", "tsn"]],
        }


def configure_workflow(workflow):
    workflow.player = DiscoveryPlayer()
    model = Model(
        dict(content_id=GTI, reason="Live matchup fits the brief", evidence_labels=["Jets vs. Lions"])
    )
    workflow.selector = DiscoverySelector(ExecutorConfig(), model=model)
    return model


async def test_discovery_request_has_no_teamarr_snapshot_and_uses_separate_selector(tmp_path):
    c, w = prime_rig(tmp_path)
    try:
        model = configure_workflow(w)
        request = discovery_request()
        report, _ = w.store.submit(request)
        assert report["content_id"] is None
        PlaybackReport.model_validate(report)
        token = report["token"]
        await w.navigate(row(w, token))
        report = w.store.report(token)
        assert report["operation"]["state"] == "playing_verified", report
        PlaybackReport.model_validate(report)
        assert report["content_id"] == "prime:" + GTI
        assert report["observation"]["content_id"] == "prime:" + GTI
        assert report["content_status"]["effective_state"] == "unknown"
        assert [v[0] for v in w.player.calls] == ["discover", "play", "attempt", "status"]
        assert "target" not in model.calls[0]
        assert model.calls[0]["brief"] == request["discovery"]["brief"]
        assert w.store.submit(request)[1] is False  # Same unresolved request, same resolved token.
        assert json.loads(row(w, token)["request"])["content_id"] is None
        calls = len(w.player.calls)
        await w.monitor(row(w, token))
        assert [v[0] for v in w.player.calls[calls:]] == ["status"]
    finally:
        await cleanup(c)


@pytest.mark.parametrize(
    "choice",
    [
        dict(content_id=None, reason="No relevant live sport", evidence_labels=[]),
        dict(content_id="invented", reason="Wrong", evidence_labels=["LIVE"]),
    ],
)
async def test_discovery_abstains_or_rejects_invented_identity(choice):
    selector = DiscoverySelector(ExecutorConfig(), model=Model(choice))
    if choice["content_id"]:
        with pytest.raises(ExecutorError, match="outside"):
            await selector.choose(discovery_request(), results(tile()), "UTC")
    else:
        selected, audit = await selector.choose(discovery_request(), results(tile()), "UTC")
        assert selected is None and audit["reason"] == choice["reason"]


async def test_discovery_filters_replay_upcoming_and_unbound_tiles_before_model():
    model = Model({})
    selector = DiscoverySelector(ExecutorConfig(), model=model)
    selected, audit = await selector.choose(
        discovery_request(),
        results(tile(availability="upcoming"), tile(title="Highlights"), tile(identity_status="ambiguous")),
        "UTC",
    )
    assert selected is None and len(audit["rejected"]) == 3 and not model.calls


async def test_cancellation_during_discovery_fences_input_without_launch(tmp_path):
    c, w = prime_rig(tmp_path)
    try:
        configure_workflow(w)
        w.player.search_release = asyncio.Event()
        report, _ = w.store.submit(discovery_request())
        token = report["token"]
        task = asyncio.create_task(w.navigate(row(w, token)))
        await w.player.search_entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not any(v[0] == "play" for v in w.player.calls)
        assert any(v[0] == "cancel" for v in w.player.calls)
        w.recover()
        assert w.store.report(token)["operation"]["phase"] == "restart_interrupted"
    finally:
        await cleanup(c)


async def test_admin_refresh_uses_pages_only_and_validates_its_contract(tmp_path):
    c, w = prime_rig(tmp_path)
    try:
        configure_workflow(w)
        request = {**discovery_request(), "purpose": "page_refresh"}
        report, _ = w.store.submit(request)
        await w.navigate(row(w, report["token"]))
        report = w.store.report(report["token"])
        PlaybackReport.model_validate(report)
        assert report["operation"]["state"] == "completed"
        assert [v[0] for v in w.player.calls] == ["pages"]
    finally:
        await cleanup(c)


def empty_device(s):
    with s.db.transaction() as db:
        d = s.db.device(db)
        d.update(plan=[], rules=[], observed=None, desired=None, playback_state="waiting")
        s.db.save_device(db, d)
    return d


def refresh(s):
    d = s.overview()["device"]
    command = PageRefreshCommand(command_id=uuid4().hex, expected_revision=d["revision"])
    s.refresh_pages_command(d["id"], command)
    s.tick()


def enable(s):
    with s.db.transaction() as db:
        d = s.db.device(db)
        d["preferences"]["prime_discovery"]["enabled_pages"] = ["sports", "dazn"]
        s.db.save_device(db, d)


def test_inventory_is_explicit_and_discovery_retains_verified_playback(rig):
    _, s, _ = rig
    empty_device(s)
    s.tick()
    assert s.overview()["device"]["prime_pages"]["state"] == "uninitialized"
    refresh(s)
    d = s.overview()["device"]
    assert d["prime_pages"]["state"] == "ready"
    assert d["preferences"]["prime_discovery"]["enabled_pages"] == []
    enable(s)
    s.tick()
    d = s.overview()["device"]
    assert d["playback_state"] == "verified" and d["discovered"]["title"]
    assert d["observed"]["simulated"]
    with s.db.transaction() as db:
        job = db.execute("SELECT * FROM jobs WHERE state='verified'").fetchone()
        assert json.loads(job["payload"])["content_id"] is None
        assert "content_snapshot" not in json.loads(job["payload"])
        count = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    refresh(s)  # queued while healthy; no navigation or polling pages
    assert s.overview()["device"]["prime_pages"]["state"] == "queued"
    s.tick()
    with s.db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == count


def test_known_target_retry_never_turns_into_discovery(rig):
    _, s, _ = rig
    with s.db.transaction() as db:
        d = s.db.device(db)
        d["prime_pages"]["pages"] = [{"id": "sports", "available": True}]
        d["preferences"]["prime_discovery"]["enabled_pages"] = ["sports"]
        for item in s.items(db):
            d["failures"][item["content_id"]] = {
                "retry_after": (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            }
        s.db.save_device(db, d)
    s.tick()
    with s.db.transaction() as db:
        assert not db.execute("SELECT * FROM jobs").fetchall()


def test_discovery_result_cannot_survive_changed_preferences_or_new_target(rig):
    _, s, _ = rig
    empty_device(s)
    refresh(s)
    enable(s)
    s.stage_playback()
    with s.db.transaction() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE state='pending'").fetchone())
        d = s.db.device(db)
        d["preferences"]["prime_discovery"]["enabled_pages"] = []
        s.db.save_device(db, d)
    report = s.playback.submit(json.loads(job["payload"]))
    report = s.playback.inspect(job["id"])
    assert not s.receive_playback_report(job["id"], report)
    assert s.overview()["device"].get("discovered") is None
    s.cancel_obsolete()
    assert s.playback.observe("living-room") is None


def test_page_preferences_roundtrip_undo_and_stale_revision(rig):
    client, s, _ = rig
    empty_device(s)
    refresh(s)
    device = s.overview()["device"]
    settings = {
        **device["preferences"],
        "prime_discovery": {
            **device["preferences"]["prime_discovery"],
            "enabled_pages": ["dazn"],
            "brief": "Live tennis first",
        },
    }
    body = dict(
        command_id=uuid4().hex,
        expected_revision=device["revision"],
        rules=device["rules"],
        team_ranks=device["team_ranks"],
        preferences=settings,
    )
    url = "/api/v1/devices/living-room"
    assert client.put(url + "/rules", json=body).status_code == 200
    assert client.put(url + "/rules", json=body).status_code == 200
    assert client.put(url + "/rules", json={**body, "command_id": uuid4().hex}).status_code == 409
    exported = client.get(url + "/configuration").json()
    assert exported["configuration"]["preferences"]["prime_discovery"] == settings["prime_discovery"]
    state = s.overview()
    assert (
        client.post(
            url + "/undo",
            json=dict(
                command_id=uuid4().hex,
                expected_revision=state["device"]["revision"],
                history_id=state["undo"]["id"],
            ),
        ).status_code
        == 200
    )
    assert s.overview()["device"]["preferences"]["prime_discovery"]["enabled_pages"] == []


def test_page_refresh_failure_retains_last_inventory_and_selection(rig):
    _, s, _ = rig
    empty_device(s)
    refresh(s)
    enable(s)
    prior = s.overview()["device"]["prime_pages"].copy()
    command = PageRefreshCommand(command_id=uuid4().hex, expected_revision=s.overview()["device"]["revision"])
    s.refresh_pages_command("living-room", command)
    s.stage_playback()
    with s.db.transaction() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE state='pending'").fetchone())
    report = dict(
        request_id=job["id"],
        device_id=job["device_id"],
        intent_version=job["intent"],
        content_id=None,
        state="failed",
        reason="Device unavailable",
    )
    assert not s.receive_playback_report(job["id"], report)
    d = s.overview()["device"]
    assert d["prime_pages"]["pages"] == prior["pages"]
    assert d["prime_pages"]["last_success"] == prior["last_success"]
    assert d["prime_pages"]["state"] == "error"
    assert d["preferences"]["prime_discovery"]["enabled_pages"] == ["sports", "dazn"]


async def test_real_workflow_resolution_is_accepted_and_new_teamarr_target_preempts(tmp_path):
    from controller.executor.integration import IntegratedPlaybackAdapter

    c, w = prime_rig(tmp_path)
    try:
        configure_workflow(w)
        with c.db.transaction() as db:
            d = c.db.device(db)
            d["prime_pages"]["pages"] = [{"id": p, "available": True} for p in ["sports", "dazn"]]
            d["preferences"]["prime_discovery"]["enabled_pages"] = ["sports", "dazn"]
            c.db.save_device(db, d)
        c.stage_playback()
        with c.db.transaction() as db:
            job = dict(db.execute("SELECT * FROM jobs WHERE state='pending'").fetchone())
        request = {
            **json.loads(job["payload"]),
            "deadline_at": datetime.fromtimestamp(job["deadline_at"], UTC).isoformat(),
        }
        report, _ = w.store.submit(request)
        token = report["token"]
        await w.navigate(row(w, token))
        assert c.receive_playback_report(job["id"], IntegratedPlaybackAdapter.project(w.store.report(token)))
        assert c.overview()["device"]["observed"]["content_id"] == "prime:" + GTI
        c.stage_playback()
        with c.db.transaction() as db:
            assert not db.execute("SELECT 1 FROM jobs WHERE state='pending'").fetchone()
            d = c.db.device(db)
            d["rules"] = [dict(id="all", name="Scheduled target", enabled=True, league="all")]
            c.db.save_device(db, d)
            c.replace_catalog(db, [payload()["content_snapshot"]], "teamarr")
        c.stage_playback()
        with c.db.transaction() as db:
            next_job = dict(db.execute("SELECT * FROM jobs WHERE state='pending'").fetchone())
        assert json.loads(next_job["payload"])["schema_version"] == 1
        assert next_job["content_id"] == "fixture-game"
    finally:
        await cleanup(c)


def test_pause_keeps_admin_refresh_queued_and_manual_handoff_clears_discovered_display(rig):
    from controller.models import AutomationUpdate

    _, s, _ = rig
    empty_device(s)
    command = PageRefreshCommand(command_id=uuid4().hex, expected_revision=s.overview()["device"]["revision"])
    s.refresh_pages_command("living-room", command)
    s.stage_playback()
    assert s.overview()["device"]["prime_pages"]["state"] == "refreshing"
    d = s.overview()["device"]
    s.automation_command(
        "living-room",
        AutomationUpdate(command_id=uuid4().hex, expected_revision=d["revision"], mode="paused"),
    )
    assert s.overview()["device"]["prime_pages"]["state"] == "queued"
    with s.db.transaction() as db:
        d = s.db.device(db)
        d["discovered"] = {"title": "Old discovery"}
        s.clear_playback_for_manual(db, d)
        assert d["discovered"] is None
