import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from test_prime_matching import GTI, results, tile
from test_real_executor import payload, settings

from controller.executor.models import CancelRequest, ExecutorError, PlaybackReport
from controller.prime_player.client import PrimePlayerClient
from controller.service import Controller


async def test_prime_config_requires_no_vision_or_adb_settings(tmp_path):
    config = settings(
        tmp_path,
        mode="prime-player",
        prime_socket="/run/prime/player.sock",
        actor_model="",
        observer_model="",
        base_url="",
    )
    config.executor.validate()
    controller = Controller(config)
    assert not hasattr(controller.executor, "device")
    assert not hasattr(controller.executor, "vision")
    await cleanup(controller)


class Player:
    cancellation_ready = True
    require = staticmethod(PrimePlayerClient.require)

    def __init__(self):
        self.calls = []
        self.session = "service-session-1"
        self.suspended = False
        self.attempt_id = None
        self.state = "playing"
        self.age = 0
        self.lost_response = False
        self.cancel_ack = True
        self.search_entered = asyncio.Event()
        self.search_release = None

    async def close(self):
        pass

    async def health(self):
        return {
            "session_id": self.session,
            "closed": False,
            "failure": None,
            "capabilities": ["search", "play", "playback_status"],
            "suspended": self.suspended,
            "compatibility": {
                "capabilities": {x: {"available": True} for x in ("search", "play", "playback_status")}
            },
        }

    async def suspend(self, value):
        self.calls.append(("suspend", value))
        self.suspended = value
        return await self.health()

    async def search(self, query, timeout):
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
            "requested_id": GTI,
            "resolved_id": GTI + "-resolved",
            "state": "playing",
            "reason": None,
            "evidence": {"resolution": {"playbackClass": "live_watch_now"}},
        }

    async def play(self, handle, attempt_id):
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

    async def cancel_attempt(self, session_id, attempt_id):
        assert session_id == self.session and attempt_id == self.attempt_id
        self.calls.append(("cancel", attempt_id))
        return {"input_quiescent": self.cancel_ack, "active_playback": "stopped"}


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


@pytest.mark.parametrize(
    "state", ["paused", "buffering", "stopped", "not_current", "ended", "unknown", "error"]
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
        assert workflow.player.suspended
    finally:
        await cleanup(controller)


async def test_unacknowledged_stop_keeps_cancellation_pending(tmp_path):
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


async def test_unimplemented_stop_boundary_cannot_start_playback(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        workflow.player.cancellation_ready = False
        token = await launch(workflow)
        assert workflow.store.report(token)["operation"]["error"]["code"] == "prime_cancel_contract_pending"
        assert not workflow.player.calls
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
            await client.play("fresh-handle", "a" * 32)
        assert exc.value.code == "prime_operation_unknown"
        assert calls == [
            {"method": "play", "params": {"handle": "fresh-handle", "mode": "live", "attempt_id": "a" * 32}}
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
