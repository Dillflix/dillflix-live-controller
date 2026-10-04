import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from playback_fixtures import payload, settings
from test_prime_matching import GTI, results, tile

from controller.executor.models import CancelRequest, ExecutorError, PlaybackReport
from controller.prime_player.client import PrimePlayerClient
from controller.service import Controller


async def test_prime_config_requires_no_vision_or_adb_settings(tmp_path):
    config = settings(
        tmp_path,
        mode="prime-player",
        prime_socket="/run/prime/player.sock",
        base_url="",
    )
    config.executor.validate()
    controller = Controller(config)
    assert not hasattr(controller.executor, "device")
    assert not hasattr(controller.executor, "vision")
    await cleanup(controller)


class Player:
    require = staticmethod(PrimePlayerClient.require)
    ownership = staticmethod(PrimePlayerClient.ownership)
    control_ownership = staticmethod(PrimePlayerClient.control_ownership)

    def __init__(self):
        self.calls = []
        self.session = "service-session-1"
        self.suspended = False
        self.attempt_id = None
        self.state = "playing"
        self.age = 0
        self.lost_response = False
        self.cancel_ack = True
        self.stop_confirmed = True
        self.epoch = 0
        self.search_entered = asyncio.Event()
        self.search_release = None

    async def close(self):
        pass

    async def health(self):
        return {
            "session_id": self.session,
            "api_version": 9,
            "serial": "fixture",
            "ownership": {
                "session_id": self.session,
                "epoch": self.epoch,
                "mode": "manual" if self.suspended else "automatic",
                "handoff_id": str(self.epoch),
                "acknowledged": True,
            },
            "closed": False,
            "failure": None,
            "capabilities": ["search", "play", "playback_status", "cancel", "stop", "resolve"],
            "suspended": self.suspended,
            "compatibility": {
                "capabilities": {
                    x: {"available": True}
                    for x in ("search", "play", "playback_status", "javascript_navigation")
                }
            },
        }

    async def control_health(self):
        return await self.health()

    async def suspend(self, value):
        self.calls.append(("suspend", value))
        self.suspended = value
        return await self.health()

    async def search(self, query, timeout, ownership):
        assert ownership["epoch"] == self.epoch and ownership["session_id"] == self.session
        self.calls.append(("search", query))
        self.search_entered.set()
        if self.search_release:
            await self.search_release.wait()
        return {
            **results(tile()),
            "query": query,
            "session_id": self.session,
            "search_id": "search-1",
            "generation": 1,
            "observed_at": datetime.now(UTC).isoformat(),
        }

    def outcome(self):
        return {
            "attempt_id": self.attempt_id,
            "session_id": self.session,
            "requested_id": GTI,
            "resolved_id": GTI + "-resolved",
            "state": "playing",
            "reason": None,
            "evidence": {"resolution": {"playbackClass": "live_watch_now"}},
        }

    async def resolve(self, content_id, ownership):
        self.calls.append(("resolve", content_id))
        return {"session_id": self.session, "requested_id": content_id, "action": None, "status": "unknown"}

    async def play(self, handle, attempt_id, ownership):
        assert ownership["epoch"] == self.epoch and ownership["session_id"] == self.session
        self.calls.append(("play", handle, attempt_id))
        self.attempt_id = attempt_id
        if self.lost_response:
            raise ExecutorError("prime_transport_unknown", "Response lost after execution")
        return self.outcome()

    async def attempt(self, attempt_id):
        self.calls.append(("attempt", attempt_id))
        assert attempt_id == self.attempt_id
        return self.outcome()

    async def status(self, attempt_id, timeout):
        self.calls.append(("status", attempt_id))
        assert attempt_id == self.attempt_id
        return {
            "attempt_id": attempt_id,
            "session_id": self.session,
            "requested_id": GTI,
            "resolved_id": GTI + "-resolved",
            "current_content_id": GTI + "-resolved",
            "state": self.state,
            "is_playing": self.state == "playing",
            "matches_attempt": self.state != "not_current",
            "observed_at": datetime.now(UTC).isoformat(),
            "observation_age_seconds": self.age,
            "position": 100,
            "position_unit": "native",
            "state_event_age_seconds": 300,
            "evidence": {},
        }

    async def cancel_work(self, session_id, attempt_id, ownership):
        assert session_id == self.session
        assert ownership["epoch"] == self.epoch and ownership["session_id"] == self.session
        self.calls.append(("cancel", attempt_id))
        if not self.cancel_ack:
            raise ExecutorError("prime_handoff_unconfirmed", "Cancellation not confirmed")
        self.epoch += 1
        if self.search_release:
            self.search_release.set()
        return {**(await self.health())["ownership"], "cancelled": True, "playback_stopped": False}

    async def stop_attempt(self, session_id, attempt_id, ownership):
        assert session_id == self.session and attempt_id == self.attempt_id
        assert ownership["epoch"] == self.epoch and ownership["session_id"] == self.session
        self.calls.append(("stop", attempt_id))
        return {
            "stopped": self.stop_confirmed,
            "page_exited": True,
            "stop_requested": True,
            "session_id": session_id,
            "attempt_id": attempt_id,
        }


def rig(tmp_path):
    configuration = settings(
        tmp_path, mode="prime-player", prime_socket="/test/player.sock", monitor_interval=100
    )
    controller = Controller(configuration)
    workflow = controller.executor
    workflow.player = Player()
    return controller, workflow


def row(workflow, token):
    with workflow.db.transaction() as db:
        return dict(workflow.store.get_row(db, token))


async def launch(workflow):
    report, _ = workflow.store.submit(payload())
    await workflow.navigate(row(workflow, report["token"]))
    return report["token"]


async def cleanup(controller):
    await controller.executor.close_resources()
    await controller.stop()


@pytest.mark.parametrize("lost", [False, True])
async def test_search_match_play_verify_and_read_only_monitor(tmp_path, lost):
    controller, workflow = rig(tmp_path)
    try:
        workflow.player.lost_response = lost
        token = await launch(workflow)
        report = workflow.store.report(token)
        PlaybackReport.model_validate(report)
        assert report["operation"]["state"] == "playing_verified"
        assert report["content_id"] == "fixture-game"
        assert report["prime_player"]["selected"]["content_id"] == GTI
        assert report["observation"]["verified"]
        assert len([c for c in workflow.player.calls if c[0] == "play"]) == 1
        calls = len(workflow.player.calls)
        await workflow.monitor(row(workflow, token))
        assert [c[0] for c in workflow.player.calls[calls:]] == ["status"]
    finally:
        await cleanup(controller)


async def test_resolver_refusal_reason_reaches_controller_failure_and_export(tmp_path):
    from controller.executor.integration import IntegratedPlaybackAdapter
    from controller.prime_player.diagnostics import collect

    controller, workflow = rig(tmp_path)
    reason = "resolver returned a non-playback result (for example details, entitlement or error page)"
    try:
        original = workflow.player.outcome
        workflow.player.outcome = lambda: {
            **original(),
            "state": "unknown",
            "resolved_id": None,
            "reason": reason,
            "evidence": {},
        }
        token = await launch(workflow)
        report = workflow.store.report(token)
        assert report["operation"]["state"] == "failed"
        assert reason in IntegratedPlaybackAdapter.project(report)["reason"]
        exported = next(p for p in collect(controller.settings.database)["playbacks"] if p["token"] == token)
        assert exported["workflow"]["launch_outcome"]["reason"] == reason
        assert reason in exported["operation"]["error"]["message"]
        assert len([call for call in workflow.player.calls if call[0] == "play"]) == 1
        assert not any(call[0] == "status" for call in workflow.player.calls)
    finally:
        await cleanup(controller)


@pytest.mark.parametrize(
    "state", ["paused", "buffering", "stopped", "not_current", "unknown", "error"]
)
async def test_nonplaying_status_never_completes_event(tmp_path, state):
    controller, workflow = rig(tmp_path)
    try:
        token = await launch(workflow)
        workflow.player.state = state
        await workflow.monitor(row(workflow, token))
        report = workflow.store.report(token)
        assert report["prime_player"]["playback_status"]["state"] == state
        assert not report["observation"]["verified"]
        assert report["content_status"]["effective_state"] == "unknown"
        assert report["observation_status"]["error"]["code"] == "prime_playback_unverified"
        with workflow.db.transaction() as db:
            assert db.execute(
                "SELECT 1 FROM activity WHERE message='Playback monitoring lost verification'"
            ).fetchone()
        assert len([c for c in workflow.player.calls if c[0] == "play"]) == 1
    finally:
        await cleanup(controller)


async def test_original_sample_age_not_refreshed_by_polling(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        token = await launch(workflow)
        original = workflow.store.report(token)["observation"]["observed_at"]
        workflow.player.age = 20
        await workflow.monitor(row(workflow, token))
        report = workflow.store.report(token)
        assert not report["observation"]["verified"]
        assert report["observation"]["observed_at"] == original
    finally:
        await cleanup(controller)


async def test_verified_evidence_expires_after_five_minutes(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    try:
        token = await launch(workflow)
        for monitored in (False, True):
            if monitored:
                await workflow.monitor(row(workflow, token))
            report = workflow.store.report(token)
            observation = report["observation"]
            start = datetime.fromisoformat(observation["observed_at"])
            until = datetime.fromisoformat(observation["valid_until"])
            lifetime = controller.settings.playback_evidence_ttl
            assert lifetime == 300
            assert (until - start).total_seconds() == 300
            request = payload()
            job = {
                "device_id": report["device_id"],
                "id": report["request_id"],
                "intent": observation["intent_version"],
                "content_id": report["content_id"],
                "payload": json.dumps(request),
            }
            # A full status request can finish after the old 15s evidence deadline.
            now = start + timedelta(seconds=299)
            monkeypatch.setattr("controller.coordinator.datetime", SimpleNamespace(now=lambda _: now))
            assert controller.observation_error(job, observation) is None
            now = until
            assert "stale" in controller.observation_error(job, observation)
    finally:
        await cleanup(controller)


def test_status_budget_and_evidence_lifetime_follow_deployment_settings(monkeypatch):
    from controller.config import Settings
    from controller.executor.config import ExecutorConfig

    monkeypatch.delenv("PRIME_PLAYER_STATUS_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("PLAYBACK_ADAPTER", "prime-player")
    monkeypatch.setenv("PRIME_PLAYER_SOCKET", "/test/player.sock")
    config = ExecutorConfig.from_env()
    assert config.prime_status_timeout == 60
    assert Settings(executor=config).playback_evidence_ttl == 300
    assert Settings().playback_evidence_ttl == 15
    monkeypatch.setenv("PRIME_PLAYER_STATUS_TIMEOUT_SECONDS", "30")
    config = ExecutorConfig.from_env()
    assert config.prime_status_timeout == 30
    assert Settings(executor=config).playback_evidence_ttl == 300


async def test_service_restart_withdraws_verification_without_replay(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        token = await launch(workflow)
        workflow.player.session = "new-service"
        await workflow.monitor(row(workflow, token))
        report = workflow.store.report(token)
        assert report["observation_status"]["error"]["code"] == "prime_session_changed"
        assert not report["observation"]["verified"]
        assert len([c for c in workflow.player.calls if c[0] == "play"]) == 1
    finally:
        await cleanup(controller)


async def test_restart_reconciles_attempt_and_never_replays_search_or_play(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        token = await launch(workflow)
        with workflow.db.transaction() as db:
            db.execute("UPDATE executor_jobs SET state='navigating' WHERE token=?", (token,))
        workflow.recover()
        calls = len(workflow.player.calls)
        await workflow.navigate(row(workflow, token))
        assert all(c[0] in {"attempt", "status"} for c in workflow.player.calls[calls:])
        assert workflow.store.report(token)["observation"]["verified"]
    finally:
        await cleanup(controller)


async def test_manual_takeover_during_search_prevents_late_play(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        report, _ = workflow.store.submit(payload())
        token = report["token"]
        workflow.player.search_release = asyncio.Event()
        task = asyncio.create_task(workflow.navigate(row(workflow, token)))
        await workflow.player.search_entered.wait()
        workflow.store.request_cancel(CancelRequest(device_id="living-room", token=token))
        task.cancel()
        await asyncio.sleep(0)
        assert workflow.input_lock.locked()
        workflow.player.search_release.set()
        await asyncio.gather(task, return_exceptions=True)
        await workflow.cancel_one(token)
        assert workflow.store.report(token)["cancellation"]["input_quiescent"]
        assert not any(c[0] == "play" for c in workflow.player.calls)
        assert any(c[0] == "cancel" for c in workflow.player.calls)
    finally:
        await cleanup(controller)


async def test_unacknowledged_input_barrier_keeps_cancellation_pending(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        token = await launch(workflow)
        workflow.store.request_cancel(CancelRequest(device_id="living-room", token=token))
        workflow.player.cancel_ack = False
        with pytest.raises(ExecutorError, match="not confirmed"):
            await workflow.cancel_one(token)
        assert row(workflow, token)["cancel_requested"] == 1
        workflow.player.cancel_ack = True
        await workflow.cancel_one(token)
        assert row(workflow, token)["cancel_requested"] == 2
    finally:
        await cleanup(controller)


async def test_page_exit_does_not_claim_native_stop_or_event_completion(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        token = await launch(workflow)
        workflow.player.stop_confirmed = None
        workflow.store.request_cancel(CancelRequest(device_id="living-room", token=token))
        await workflow.cancel_one(token)
        report = workflow.store.report(token)
        assert report["cancellation"] == {
            "state": "acknowledged",
            "input_quiescent": True,
            "active_playback": "unknown",
        }
        assert report["content_status"]["effective_state"] != "ended"
        await workflow.cancel_one(token)
        assert [c[0] for c in workflow.player.calls].count("stop") == 1
    finally:
        await cleanup(controller)


async def test_transport_matches_service_wire_and_never_retries_mutations():
    calls = []

    async def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == "/rpc"
        return httpx.Response(409, json={"error": {"type": "OutcomeUnknownError", "message": "uncertain"}})

    client = PrimePlayerClient("/test/player.sock", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ExecutorError) as exc:
            await client.play("fresh-handle", "a" * 32, {"epoch": 2})
        assert exc.value.code == "prime_operation_unknown"
        assert calls == [
            {
                "method": "play",
                "params": {
                    "handle": "fresh-handle",
                    "mode": "live",
                    "attempt_id": "a" * 32,
                    "ownership": {"epoch": 2},
                },
            }
        ]
    finally:
        await client.close()


async def test_late_or_wrong_session_status_is_rejected(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        token = await launch(workflow)
        status = await workflow.player.status(workflow.player.attempt_id, 10)
        for key, value in [
            ("session_id", "different"),
            ("attempt_id", "different"),
            ("requested_id", "different"),
        ]:
            with pytest.raises(ExecutorError, match="another session"):
                workflow.record_status(token, {**status, key: value})
        workflow.store.request_cancel(CancelRequest(device_id="living-room", token=token))
        with pytest.raises(ExecutorError, match="no longer owns"):
            workflow.record_status(token, status)
    finally:
        await cleanup(controller)


async def test_background_worker_and_manual_cancel_share_same_durable_token(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        await workflow.start()
        report, created = workflow.submit(payload())
        assert created
        token = report["token"]
        async with asyncio.timeout(2):
            while workflow.store.report(token)["operation"]["state"] != "playing_verified":
                await asyncio.sleep(0.01)
        result = await workflow.cancel(CancelRequest(device_id="living-room", token=token))
        assert result["input_quiescent"]
        assert workflow.store.report(token)["cancellation"]["state"] == "acknowledged"
        assert [c[0] for c in workflow.player.calls].count("play") == 1
        assert [c[0] for c in workflow.player.calls].count("cancel") == 1
    finally:
        await cleanup(controller)


async def test_durable_workflow_reopens_database_without_new_launch(tmp_path):
    controller, workflow = rig(tmp_path)
    token = await launch(workflow)
    player = workflow.player
    await cleanup(controller)
    restored = Controller(controller.settings)
    restored.executor.player = player
    try:
        restored.executor.recover()
        assert not restored.executor.store.report(token)["observation"]["verified"]
        calls = len(player.calls)
        await restored.executor.monitor(row(restored.executor, token))
        assert restored.executor.store.report(token)["observation"]["verified"]
        assert [c[0] for c in player.calls[calls:]] == ["status"]
    finally:
        await cleanup(restored)


async def test_old_cancel_does_not_stop_newer_owned_attempt(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        old = await launch(workflow)
        workflow.store.request_cancel(CancelRequest(device_id="living-room", token=old))
        await workflow.cancel_one(old)
        report, _ = workflow.store.submit(payload(intent=3))
        new = report["token"]
        await workflow.navigate(row(workflow, new))
        calls = len(workflow.player.calls)
        await workflow.cancel_one(old)
        assert workflow.player.calls[calls:] == []
        assert workflow.store.report(new)["observation"]["verified"]
    finally:
        await cleanup(controller)


async def test_lost_stop_response_is_fenced_and_never_replayed_after_restart(tmp_path):
    controller, workflow = rig(tmp_path)
    token = await launch(workflow)
    player = workflow.player
    stop_calls = []

    async def lost(*args):
        stop_calls.append(args)
        raise ExecutorError("prime_transport_unknown", "Lost stop response")

    player.stop_attempt = lost
    workflow.store.request_cancel(CancelRequest(device_id="living-room", token=token))
    await workflow.cancel_one(token)
    assert workflow.store.report(token)["cancellation"]["active_playback"] == "unknown"
    assert len([c for c in player.calls if c[0] == "cancel"]) == 2
    await cleanup(controller)
    restored = Controller(controller.settings)
    restored.executor.player = player
    try:
        restored.executor.recover()
        await restored.executor.cancel_one(token)
        assert len(stop_calls) == 1
    finally:
        await cleanup(restored)


async def test_manual_owner_cannot_be_resumed_by_automatic_search(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        workflow.player.suspended = True
        token = await launch(workflow)
        assert workflow.store.report(token)["operation"]["error"]["code"] == "prime_ownership_unavailable"
        assert not workflow.player.calls
    finally:
        await cleanup(controller)


async def test_restore_preserves_newer_tokens_and_fences_service_attempt(tmp_path):
    from controller import ops

    controller, workflow = rig(tmp_path)
    backup = tmp_path / "before-play.sqlite3"
    ops.backup(controller.settings.database, backup)
    token = await launch(workflow)
    player = workflow.player
    await cleanup(controller)
    ops.restore(backup, controller.settings.database, replace=True)
    restored = Controller(controller.settings)
    restored.executor.player = player
    before = len(player.calls)
    try:
        assert restored.overview()["device"]["input_handoff"]
        await restored.executor.start()
        await restored.playback_work(restored.tick)
        assert [c[0] for c in player.calls[before:]] == ["cancel", "stop"]
        assert restored.overview()["device"]["input_handoff"] is None
        assert restored.overview()["device"]["automation"] == "paused"
        assert restored.executor.store.report(token)["cancellation"]["input_quiescent"]
    finally:
        await cleanup(restored)


async def test_service_restart_cancel_never_sends_old_stop_to_new_service(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        token = await launch(workflow)
        workflow.player.session = "new-session"
        workflow.store.request_cancel(CancelRequest(device_id="living-room", token=token))
        before = len(workflow.player.calls)
        await workflow.cancel_one(token)
        assert not workflow.player.calls[before:]
        assert workflow.store.report(token)["cancellation"]["active_playback"] == "not_current"
    finally:
        await cleanup(controller)


async def test_startup_accepts_player_proof_without_status_inspection(tmp_path):
    controller, workflow = rig(tmp_path)
    try:

        async def unavailable(*args):
            raise AssertionError("startup must not inspect playback again")

        workflow.player.status = unavailable
        token = await launch(workflow)
        report = workflow.store.report(token)
        assert report["operation"]["state"] == "playing_verified"
        assert report["observation"]["verified"]
        assert "playback_status" not in report["prime_player"]
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("blocked", [False, True])
async def test_cancellation_recovers_with_failed_runtime_health(tmp_path, blocked):
    controller, workflow = rig(tmp_path)
    try:
        token = await launch(workflow)
        player = workflow.player
        original_health = player.health

        async def control_health():
            health = await original_health()
            health["failure"] = "runtime evidence unavailable"
            if blocked:
                health["ownership"].update(mode="blocked", acknowledged=False)
            return health

        async def unhealthy():
            raise ExecutorError("prime_unavailable", "Runtime unavailable")

        async def barrier(session_id, attempt_id, ownership):
            player.calls.append(("cancel", attempt_id))
            player.epoch += 1
            return (await original_health())["ownership"]

        async def uncertain_stop(*args):
            player.calls.append(("stop", args[1]))
            raise ExecutorError("prime_operation_unknown", "Stop outcome unknown")

        player.control_health, player.health = control_health, unhealthy
        player.cancel_work, player.stop_attempt = barrier, uncertain_stop
        workflow.store.request_cancel(CancelRequest(device_id="living-room", token=token))
        await workflow.cancel_one(token)
        assert workflow.store.report(token)["cancellation"] == {
            "state": "acknowledged",
            "input_quiescent": True,
            "active_playback": "unknown",
        }
        assert [c[0] for c in player.calls].count("stop") == 1
        assert [c[0] for c in player.calls].count("cancel") == 2
        await workflow.cancel_one(token)
        assert [c[0] for c in player.calls].count("stop") == 1
    finally:
        await cleanup(controller)


@pytest.mark.parametrize(
    "state,offset,eligible",
    [
        ("scheduled", -60, True),
        ("scheduled", 60, False),
        ("ended", -60, False),
        ("cancelled", -60, False),
        ("postponed", -60, False),
        ("unknown", -60, False),
    ],
)
async def test_due_scheduled_prime_event_can_be_selected_without_claiming_live(
    tmp_path, state, offset, eligible
):
    from datetime import timedelta

    from controller.planner import choose

    controller, workflow = rig(tmp_path)
    try:
        snapshot = payload()["content_snapshot"]
        snapshot["status"] = state
        snapshot["start_time"] = (datetime.now(UTC) + timedelta(seconds=offset)).isoformat()
        with controller.db.transaction() as db:
            controller.replace_catalog(db, [snapshot], "teamarr")
            items = controller.items(db)
            device = controller.db.device(db, "living-room")
            decision = choose(device, items, datetime.now(UTC), datetime.now(UTC))
        assert items[0]["lifecycle"]["state"] == state
        assert items[0]["playable"] is eligible
        assert bool(decision["content_id"]) is eligible
    finally:
        await cleanup(controller)


async def test_model_failure_prompt_and_response_survive_into_export(tmp_path):
    from controller.llm import ChatClient
    from controller.prime_player.diagnostics import collect
    from controller.prime_player.matching import EventMatcher

    controller, workflow = rig(tmp_path)
    try:

        async def rejected_choice(request, results, timezone):
            await client.completion("test", [{"role": "user", "content": "actual matching prompt"}])

        client = ChatClient(
            workflow.config,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(400, json={"error": {"message": "failed to parse grammar"}})
            ),
        )
        await workflow.matcher.close()
        workflow.matcher = EventMatcher(workflow.config, model=client)
        workflow.matcher.choose = rejected_choice
        token = await launch(workflow)
        bundle = collect(controller.settings.database)
        job = next(j for j in bundle["playbacks"] if j["token"] == token)
        evidence = job["workflow"]["model_calls"][0]
        assert evidence["request"]["json"]["messages"][0]["content"] == "actual matching prompt"
        assert evidence["response"]["json"]["error"]["message"] == "failed to parse grammar"
        assert evidence["http_status"] == 400
        assert evidence["state"] == "failed"
        assert job["operation"]["error"]["code"] == "model_http_error"
    finally:
        await cleanup(controller)
