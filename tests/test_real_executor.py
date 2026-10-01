"""Real executor code with controlled ADB/model boundaries; no physical TV claims."""

import asyncio
import copy
import hashlib
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from controller.api import create_app
from controller.config import Settings
from controller.device_input import DeviceInput
from controller.executor.adb import PACKAGE, Frame, media_sessions, prepare_image
from controller.executor.config import ExecutorConfig
from controller.executor.models import CancelRequest, CancelResult, ExecutorError, PlaybackReport, Scene
from controller.executor.verification import activation_allowed, completed, identity_matches, search_queries
from controller.executor.vision import VisionClient
from controller.models import ManualControlCommand
from controller.service import Controller

TOKEN = "test-service-credential-32-characters-long"
HEADERS = {"Authorization": "Bearer " + TOKEN}


def settings(tmp_path, **overrides):
    config = ExecutorConfig(
        mode="prime-video",
        api_token=TOKEN,
        base_url="http://model.invalid/v1",
        actor_model="actor",
        observer_model="observer",
        settle_seconds=0,
        monitor_interval=30,
        completion_interval=1,
        cancel_timeout=3,
    )
    return Settings(
        database=str(tmp_path / "executor.sqlite3"),
        mode="teamarr",
        teamarr_url="http://teamarr.invalid",
        screen_adb_serial="fixture",
        simulation_delay=0,
        executor=replace(config, **overrides),
    )


def payload(intent=2):
    return {
        "schema_version": 1,
        "request_id": uuid4().hex,
        "device_id": "living-room",
        "intent_version": intent,
        "content_id": "fixture-game",
        "mode": "live",
        "purpose": "selection",
        "previous_request_id": None,
        "content_snapshot_schema_version": 1,
        "deadline_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
        "content_snapshot": {
            "id": "fixture-game",
            "kind": "event",
            "title": "Jets vs. Lions",
            "competition": "nfl",
            "status": "live",
            "start_time": datetime.now(UTC).isoformat(),
            "event": {
                "league": "nfl",
                "away_team_details": {"name": "Jets", "full_name": "New York Jets", "abbreviation": "NYJ"},
                "home_team_details": {"name": "Lions", "full_name": "Detroit Lions", "abbreviation": "DET"},
            },
            "viewing_options": [
                {"id": "prime-option", "app": "prime_video", "presentation": "live", "decision": "eligible"}
            ],
        },
        "allowed_viewing_options": [
            {"id": "prime-option", "app": "prime_video", "presentation": "live", "decision": "eligible"}
        ],
    }


def identity():
    return {
        "title": "Jets vs. Lions",
        "teams": ["Jets", "Lions"],
        "competition": "NFL",
        "date_text": None,
        "kind": "game",
        "provider": None,
        "language": None,
    }


def scene(*, playing=False, menu=False, ended=False, availability="live"):
    return Scene.model_validate(
        {
            "surface": "player" if playing else "live_choice" if menu else "search",
            "search_state": "listings",
            "current_query": None,
            "blocker": "none",
            "focus": {
                "label": "Watch Live" if menu else "Jets vs. Lions",
                "role": "play_live" if menu else "event",
                "identity": identity(),
                "availability": availability,
                "live_text": "LIVE" if availability == "live" else availability.upper(),
            },
            "player": {
                "identity": identity(),
                "live_edge": True,
                "live_text": "LIVE",
                "transport": "playing",
                "position_seconds": time.monotonic(),
            }
            if playing
            else None,
            "completion": {
                "identity": identity() if ended else None,
                "scope": "game" if ended else "unknown",
                "final_text": "FINAL" if ended else None,
            },
            "action_menu": {
                "identity": identity(),
                "availability": availability,
                "live_text": "LIVE",
                "layout": "vertical",
                "items": [
                    {"label": "Watch Live", "provider": None, "language": None},
                    {"label": "Resume", "provider": None, "language": None},
                ],
            }
            if menu
            else None,
        }
    )


class Device:
    def __init__(self):
        self.actions, self.playing, self.ended = [], False, False
        self.menu = False
        self.captures = 0
        self.stop_error = False
        self.stop_release = None

    async def ready(self):
        pass

    async def close(self):
        pass

    async def prepare_input(self, action, *, frame=None, native=None):
        self.validate_frame(frame)
        if native:
            self.validate_native(native)

    async def after_input(self, action):
        pass

    def validate_frame(self, frame):
        pass

    def invalidate_focus(self):
        pass

    def native_focus(self):
        return None

    async def launch(self):
        self.actions.append("LAUNCH")

    async def search(self, query):
        self.actions.append("SEARCH:" + query)
        self.playing = False
        self.menu = False

    async def key(self, action):
        self.actions.append(action)
        if action == "SELECT":
            if self.menu:
                self.playing = True
                self.menu = False
            else:
                self.menu = True

    async def state(self):
        return PACKAGE, [
            {
                "package": PACKAGE,
                "active": True,
                "state": 3 if self.playing else 0,
                "position_ms": int(time.monotonic() * 1000),
                "updated": int(time.monotonic() * 1000),
            }
        ]

    async def capture(self):
        self.captures += 1
        image = json.dumps(
            {"playing": self.playing, "menu": self.menu, "ended": self.ended, "capture": self.captures}
        ).encode()
        return Frame(
            image,
            hashlib.sha256(image).hexdigest(),
            datetime.now(UTC),
            PACKAGE,
            (await self.state())[1],
            runtime={**await self.runtime(), "capture_association": "unchanged"},
        )

    async def runtime(self):
        return {
            "source": "media_probe",
            "source_health": "fresh",
            "foreground": PACKAGE,
            "boot_id": "fixture-boot",
            "probe_instance": "fixture-process",
            "identity_revision": 1,
            "active_confirmed": True,
            "observed_at": datetime.now(UTC).isoformat(),
            "device_elapsed_ms": time.monotonic() * 1000,
            "session": {
                "session_token": "fixture-session",
                "runtime_media_id": "fixture-media",
                "identity_status": "consistent",
                "transport": "playing" if self.playing else "none",
                "position_ms": 344511168,
            },
        }

    async def stop(self):
        if self.stop_release:
            await self.stop_release.wait()
        if self.stop_error:
            raise ExecutorError("stop_unconfirmed", "fixture stop failed")
        self.actions.append("STOP")
        self.playing = False


class Vision:
    def __init__(self):
        self.block = None
        self.entered = asyncio.Event()
        self.cancelled = False

    async def observe(self, frame):
        self.entered.set()
        if self.block:
            try:
                await self.block.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        fields = json.loads(frame.image)
        return scene(playing=fields["playing"], menu=fields["menu"], ended=fields["ended"])

    async def decide(self, frame, request, option, history, feedback=None):
        return "FINISH" if json.loads(frame.image)["playing"] else "SELECT"

    async def close(self):
        pass


async def until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
def api(tmp_path):
    app = create_app(settings(tmp_path), start_workers=False)
    engine = app.state.controller.executor
    engine.device, engine.vision = Device(), Vision()
    with TestClient(app) as client:
        yield client, app.state.controller, engine


def test_api_durable_play_get_active_cancel_and_authentication(api):
    client, service, engine = api
    request = payload()
    assert client.post("/v1/playbacks", json=request).status_code == 401
    result = client.post("/v1/playbacks", json=request, headers=HEADERS)
    assert result.status_code == 202, result.text
    token = result.json()["token"]
    PlaybackReport.model_validate(result.json())
    assert client.post("/v1/playbacks", json=request, headers=HEADERS).json()["token"] == token
    assert (
        client.post(
            "/v1/playbacks",
            json={**request, "content_snapshot": {**request["content_snapshot"], "title": "changed"}},
            headers=HEADERS,
        ).status_code
        == 409
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        report = client.get("/v1/playbacks/" + token, headers=HEADERS).json()
        if report["operation"]["state"] == "playing_verified":
            break
        time.sleep(0.02)
    assert report["operation"]["state"] == "playing_verified", report
    PlaybackReport.model_validate(report)
    assert report["observation"]["simulated"] is False and report["observation"]["verified"]
    assert engine.device.actions.count("SELECT") == 2
    assert "SEARCH:Jets Lions" in engine.device.actions
    cancelled = client.post(
        "/v1/playbacks/cancel", headers=HEADERS, json={"device_id": "living-room", "token": token}
    )
    assert cancelled.status_code == 200 and cancelled.json()["input_quiescent"]
    CancelResult.model_validate(cancelled.json())
    assert engine.device.actions[-1] == "STOP"
    again = client.post(
        "/v1/playbacks/cancel", headers=HEADERS, json={"device_id": "living-room", "token": token}
    )
    assert again.status_code == 200 and engine.device.actions.count("STOP") == 1
    report = client.get("/v1/playbacks/" + token, headers=HEADERS).json()
    assert report["observation"] is None
    assert report["content_status"]["effective_state"] != "ended"
    assert report["cancellation"]["state"] == "acknowledged"
    assert service.overview()["meta"]["playback_adapter"] == "prime-video"


def test_body_limits_validation_and_token_errors(api):
    client, _, _ = api
    assert client.get("/v1/playbacks/absent", headers=HEADERS).status_code == 404
    for change in (
        {"mode": "replay"},
        {"device_id": "different"},
        {"intent_version": -1},
        {"content_id": "different"},
    ):
        assert client.post("/v1/playbacks", headers=HEADERS, json={**payload(), **change}).status_code == 422
    assert (
        client.post("/v1/playbacks/cancel", headers=HEADERS, json={"device_id": "living-room"}).status_code
        == 422
    )
    assert (
        client.post("/v1/playbacks", headers=HEADERS, content=b"x" * (2 * 1024 * 1024 + 1)).status_code == 413
    )


async def test_cancel_interrupts_inference_and_old_device_fence_cannot_stop_newer(tmp_path):
    service = Controller(settings(tmp_path))
    engine = service.executor
    engine.device, engine.vision = Device(), Vision()
    engine.vision.block = asyncio.Event()
    await engine.start()
    try:
        first, _ = engine.submit(payload())
        await engine.vision.entered.wait()
        result = await engine.cancel(CancelRequest(device_id="living-room", through_intent_version=2))
        assert result["input_quiescent"] and engine.vision.cancelled
        assert "SELECT" not in engine.device.actions
        with pytest.raises(ExecutorError, match="obsolete"):
            engine.submit(payload(2))
        engine.vision.block = None
        second, _ = engine.submit(payload(3))
        await until(lambda: engine.store.report(second["token"])["operation"]["state"] == "playing_verified")
        stops = engine.device.actions.count("STOP")
        await engine.cancel(CancelRequest(device_id="living-room", through_intent_version=2))
        assert engine.device.actions.count("STOP") == stops
        assert engine.device.playing
    finally:
        await service.stop()


async def test_manual_input_waits_for_real_executor_stop_and_shares_gate(tmp_path):
    service = Controller(settings(tmp_path))
    engine = service.executor
    engine.device, engine.vision = Device(), Vision()
    engine.device.stop_release = asyncio.Event()
    manager = DeviceInput(service)
    await engine.start()
    task = None
    try:
        engine.submit(payload())
        await engine.vision.entered.wait()
        command = ManualControlCommand(
            command_id="take", expected_revision=0, action="take", session_id="s" * 32, owner_token="o" * 32
        )
        task = asyncio.create_task(manager.command("living-room", command))
        await until(lambda: service.overview()["device"].get("manual_control"))
        await asyncio.sleep(0.05)
        assert not task.done()
        assert service.overview()["device"]["manual_control"]["input_ready"] is False
        engine.device.stop_release.set()
        await asyncio.wait_for(task, 2)
        assert service.manual_authorized("living-room", command.session_id, command.owner_token)[
            "input_ready"
        ]
        assert engine.device.actions[-1] == "STOP"
    finally:
        engine.device.stop_release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await manager.stop()
        await service.stop()


async def test_manual_wake_and_key_invalidate_native_focus_before_delivery(tmp_path):
    from controller.device_input import WAKE_PACKET, Attach, Input
    from controller.executor.accessibility import FocusState

    service = Controller(settings(tmp_path))
    engine = service.executor
    engine.device, engine.vision = Device(), Vision()
    focus = FocusState()
    engine.device.invalidate_focus = focus.invalidate
    manager = DeviceInput(service)
    delivered = []

    class Source:
        async def send(self, packet):
            assert manager.lock.locked() and engine.input_lock.locked()
            assert not focus.snapshot()["usable"]
            delivered.append(packet)

    await engine.start()
    try:
        command = ManualControlCommand(
            command_id="take", expected_revision=0, action="take", session_id="s" * 32, owner_token="o" * 32
        )
        await manager.command("living-room", command)
        attach = Attach(session_id=command.session_id, owner_token=command.owner_token)
        manager.connection = (asyncio.current_task(), attach)
        packets = [WAKE_PACKET, Input(seq=1, key="right").encode()]
        for packet in packets:
            focus.begin_action("OBSERVE", 100)
            focus.ingest(
                "EventType: TYPE_VIEW_FOCUSED; EventTime: 101; "
                "PackageName: com.amazon.firebat; Text: [Previously focused]"
            )
            assert focus.snapshot()["usable"]
            async with manager.lock:
                await manager.send_locked(Source(), packet, "living-room", attach)
        assert delivered == packets
    finally:
        manager.connection = None
        await manager.stop()
        await service.stop()


async def test_restart_never_replays_uncertain_action_and_preserves_token(tmp_path):
    service = Controller(settings(tmp_path))
    store = service.executor.store
    request = payload()
    report, _ = store.submit(request)
    store.journal(report["token"], "SELECT", "old-frame")  # Crash before acknowledgement.
    service.executor.device, service.executor.vision = Device(), Vision()
    await service.executor.start()
    try:
        await until(lambda: store.report(report["token"])["cancellation"]["state"] == "acknowledged")
        assert store.by_request(request["request_id"])["token"] == report["token"]
        assert "SELECT" not in service.executor.device.actions
        assert service.executor.device.actions == ["STOP"]
        assert store.report(report["token"])["operation"]["error"]["code"] == "restart_interrupted"
    finally:
        await service.stop()


async def test_confirmed_completion_requires_two_readings_and_correct_scope(tmp_path):
    service = Controller(settings(tmp_path))
    engine = service.executor
    engine.device, engine.vision = Device(), Vision()
    await engine.start()
    try:
        request = payload()
        report, _ = engine.submit(request)
        await until(lambda: engine.store.report(report["token"])["operation"]["state"] == "playing_verified")
        engine.device.ended = True
        sample = (await engine.device.capture(), scene(playing=True, ended=True))
        engine.record_completion(report["token"], request, sample)
        assert engine.store.report(report["token"])["content_status"]["effective_state"] == "unknown"
        await asyncio.sleep(1.01)
        second = (await engine.device.capture(), scene(playing=True, ended=True))
        engine.record_completion(report["token"], request, second)
        lifecycle = engine.store.report(report["token"])["content_status"]
        assert lifecycle["effective_state"] == "ended"
        assert lifecycle["observation"]["evidence"]["decision"] == "confirmed"
        broadcast = copy.deepcopy(request)
        broadcast["content_snapshot"].update(kind="broadcast", title="NFL RedZone", event=None)
        assert not completed(second[1], second[0], broadcast, "America/Vancouver")
    finally:
        await service.stop()


@pytest.mark.parametrize("availability", ["upcoming", "replay", "ended", "unknown"])
def test_archived_upcoming_and_replay_focus_cannot_activate(availability):
    # The archive's Jets/Lions focus screenshot has UPCOMING, not live playback.
    request = payload()
    frame = Frame(b"fixture", "hash", datetime.now(UTC), PACKAGE, [])
    observed = scene(availability=availability)
    assert not activation_allowed(
        observed, request, request["allowed_viewing_options"][0], frame, "America/Vancouver"
    )


def test_identity_date_route_and_completion_negative_cases():
    request = payload()
    current = scene()
    frame = Frame(b"fixture", "hash", datetime.now(UTC), PACKAGE, [])
    assert search_queries(request["content_snapshot"])[0] == "Jets Lions"
    assert activation_allowed(
        current, request, request["allowed_viewing_options"][0], frame, "America/Vancouver"
    )
    current.focus.identity.competition = "NHL"
    assert not identity_matches(current.focus.identity, request["content_snapshot"], frame.captured_at)
    current = scene()
    current.focus.identity.date_text = "1999-09-01"
    assert not activation_allowed(
        current, request, request["allowed_viewing_options"][0], frame, "America/Vancouver"
    )
    current = scene()
    option = {**request["allowed_viewing_options"][0], "channel": "DAZN"}
    # Opening the matching card can reveal route details. Playing still needs them.
    assert activation_allowed(current, request, option, frame, "America/Vancouver")
    current = scene(menu=True)
    assert not activation_allowed(current, request, option, frame, "America/Vancouver")
    current.focus.role, current.focus.label = "navigation", "Watch replay"
    assert not activation_allowed(current, request, option, frame, "America/Vancouver")
    final = scene(ended=True)
    final.completion.final_text = "Not final; half time"
    assert not completed(final, frame, request, "America/Vancouver")


async def test_goal_blind_observer_http_schema_sampling_and_native_actor_parser():
    requests = []

    async def responder(request):
        body = json.loads(request.content)
        requests.append(body)
        answer = scene().model_dump_json() if body["model"] == "observer" else "<answer>OK</answer>"
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": answer}}]}
        )

    config = ExecutorConfig(
        base_url="http://model.invalid/v1",
        actor_model="actor",
        observer_model="observer",
        actor_protocol="tvtheseus",
        model_options={"temperature": 0, "top_p": 0.8, "top_k": 20},
    )
    client = VisionClient(config, transport=httpx.MockTransport(responder))
    frame = Frame(b"jpeg", "hash", datetime.now(UTC), PACKAGE, [])
    try:
        await client.observe(frame)
        assert "fixture-game" not in json.dumps(requests[0])
        assert requests[0]["response_format"]["json_schema"]["strict"] is True
        assert requests[0]["top_k"] == 20
        request = payload()
        assert await client.decide(frame, request, request["allowed_viewing_options"][0], []) == "SELECT"
        assert "fixture-game" in json.dumps(requests[1])
    finally:
        await client.close()


def test_adb_media_parser_and_blank_frame_rejection():
    import io

    from PIL import Image

    text = "MEDIA SESSION SERVICE\n  package=com.amazon.firebat\n  active=true\n  state=PlaybackState {state=3, position=2100, updated=999}\n"
    assert media_sessions(text)[0]["position_ms"] == 2100
    output = io.BytesIO()
    Image.new("RGB", (1280, 720)).save(output, "PNG")
    with pytest.raises(ExecutorError, match="blank or protected"):
        prepare_image(output.getvalue())
    with pytest.raises(ExecutorError):
        media_sessions("unknown dump")


async def test_unconfirmed_stop_is_durable_and_retry_can_acknowledge(tmp_path):
    service = Controller(settings(tmp_path, cancel_timeout=0.2))
    engine = service.executor
    engine.device, engine.vision = Device(), Vision()
    engine.vision.block = asyncio.Event()
    await engine.start()
    try:
        report, _ = engine.submit(payload())
        await engine.vision.entered.wait()
        engine.device.stop_error = True
        command = CancelRequest(device_id="living-room", token=report["token"])
        with pytest.raises(TimeoutError):
            await engine.cancel(command)
        assert engine.store.report(report["token"])["cancellation"] == {
            "state": "requested",
            "input_quiescent": False,
        }
        engine.device.stop_error = False
        assert (await engine.cancel(command))["input_quiescent"]
    finally:
        await service.stop()


async def test_missing_route_deadline_and_retention_do_not_relaunch(tmp_path):
    service = Controller(settings(tmp_path))
    engine = service.executor
    engine.device, engine.vision = Device(), Vision()
    await engine.start()
    try:
        request = payload()
        request["allowed_viewing_options"][0]["app"] = "unsupported"
        request["content_snapshot"]["viewing_options"][0]["app"] = "unsupported"
        report, _ = engine.submit(request)
        await until(lambda: engine.store.report(report["token"])["cancellation"]["state"] == "acknowledged")
        assert engine.store.report(report["token"])["operation"]["error"]["code"] == "unsupported_routes"
        assert engine.device.actions == []
        with engine.db.transaction() as db:
            db.execute(
                "UPDATE executor_jobs SET retired_at=? WHERE token=?",
                (time.time() - 8 * 86400, report["token"]),
            )
        engine.store.prune()
        with pytest.raises(ExecutorError) as error:
            engine.submit(request)
        assert error.value.status == 410
        expired = payload(3)
        expired["deadline_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with pytest.raises(ExecutorError) as error:
            engine.submit(expired)
        assert error.value.code == "deadline_expired"
        assert engine.device.actions == []
    finally:
        await service.stop()


async def test_second_executor_process_guard_is_rejected(tmp_path):
    from controller.executor.runtime import PlaybackExecutor

    service = Controller(settings(tmp_path))
    service.executor.device, service.executor.vision = Device(), Vision()
    service.executor.vision.block = asyncio.Event()
    await service.executor.start()
    other = PlaybackExecutor(service.db, service.settings, device=Device(), vision=Vision())
    try:
        report, _ = service.executor.submit(payload())
        await service.executor.vision.entered.wait()
        service.executor.store.request_cancel(CancelRequest(device_id="living-room", token=report["token"]))
        with pytest.raises(RuntimeError, match="Only one executor"):
            await other.start()
        await other.close()
        assert other.device.actions == []  # Failed startup has no authority to clean up another worker.
    finally:
        await service.stop()


async def test_controller_receives_real_token_observation_pause_and_completion(tmp_path):
    from controller.models import AutomationUpdate

    service = Controller(settings(tmp_path))
    engine = service.executor
    engine.device, engine.vision = Device(), Vision()
    engine.vision.block = asyncio.Event()
    request = payload()
    with service.db.transaction() as db:
        service.replace_catalog(db, [request["content_snapshot"]], "teamarr")
        device = service.db.device(db)
        device["plan"] = [service.entry(request["content_id"])]
        service.db.save_device(db, device)
    await engine.start()
    try:
        await service.refresh_status(force=True)
        await service.playback_work(service.tick)
        with service.db.transaction() as db:
            job = db.execute("SELECT * FROM jobs").fetchone()
        token = job["executor_job_id"]
        assert token and engine.store.report(token)["operation"]["state"] in {"accepted", "navigating"}
        engine.vision.block.set()  # Assert async acceptance without relying on inference being slow.
        await until(lambda: engine.store.report(token)["operation"]["state"] == "playing_verified")
        await service.playback_work(service.tick)
        observed = service.overview()["device"]["observed"]
        assert observed["verified"] and observed["simulated"] is False
        service.automation_command(
            "living-room", AutomationUpdate(command_id="pause", expected_revision=0, mode="paused")
        )
        await service.playback_work(service.tick)
        assert engine.device.playing and engine.device.actions.count("STOP") == 0
        with service.db.transaction() as db:
            row = dict(engine.store.get_row(db, token))
        await engine.monitor(row)  # Read-only monitoring is permitted after pause increments intent.
        assert engine.store.report(token)["observation"]["verified"]
        accepted = json.loads(row["request"])
        first = (await engine.device.capture(), scene(playing=True, ended=True))
        engine.record_completion(token, accepted, first)
        second = (replace(first[0], captured_at=first[0].captured_at + timedelta(seconds=1.1)), first[1])
        engine.record_completion(token, accepted, second)
        await service.refresh_status(force=True)
        overview = service.overview()
        assert overview["events"][0]["lifecycle"]["state"] == "ended"
        assert overview["events"][0]["lifecycle"]["timestamp_basis"] == "device_observed"
        service.automation_command(
            "living-room", AutomationUpdate(command_id="resume", expected_revision=1, mode="active")
        )
        await service.playback_work(service.tick)
        assert not engine.device.playing
        assert service.overview()["device"]["plan"]  # Completion never deletes a commitment.
    finally:
        await service.stop()


async def test_restore_preserves_newer_tokens_and_stops_playback_absent_from_backup(tmp_path):
    from controller import ops

    configuration = settings(tmp_path)
    service = Controller(configuration)
    device = Device()
    service.executor.device, service.executor.vision = device, Vision()
    await service.executor.start()
    backup = tmp_path / "before-play.sqlite3"
    ops.backup(configuration.database, backup)
    report, _ = service.executor.submit(payload(20))
    await until(
        lambda: service.executor.store.report(report["token"])["operation"]["state"] == "playing_verified"
    )
    await service.stop()
    assert device.playing
    ops.restore(backup, configuration.database, replace=True)
    restored = Controller(configuration)
    restored.executor.device, restored.executor.vision = device, Vision()
    before = len(device.actions)
    try:
        assert restored.overview()["device"]["input_handoff"]
        assert restored.overview()["device"]["intent_version"] > 20
        await restored.executor.start()
        await restored.playback_work(restored.tick)
        assert not device.playing
        assert device.actions[before:] == ["STOP"]
        assert restored.overview()["device"]["input_handoff"] is None
        assert restored.overview()["device"]["automation"] == "paused"
        assert restored.executor.store.report(report["token"])["cancellation"]["input_quiescent"]
    finally:
        await restored.stop()


async def test_cancel_drains_an_inflight_device_action_before_acknowledging(tmp_path):
    service = Controller(settings(tmp_path))
    engine = service.executor
    engine.device, engine.vision = Device(), Vision()
    entered, release = asyncio.Event(), asyncio.Event()
    original = engine.device.key

    async def slow_key(action):
        if action == "SELECT":
            entered.set()
            await release.wait()
        await original(action)

    engine.device.key = slow_key
    await engine.start()
    cancellation = None
    try:
        report, _ = engine.submit(payload())
        await asyncio.wait_for(entered.wait(), 2)
        cancellation = asyncio.create_task(
            engine.cancel(CancelRequest(device_id="living-room", token=report["token"]))
        )
        await asyncio.sleep(0.05)
        assert not cancellation.done() and "STOP" not in engine.device.actions
        assert service.overview()["meta"]["playback_adapter"] == "prime-video"
        engine.notify([report["token"]])  # A repeated cancellation must also wait for the in-flight input.
        await asyncio.sleep(0.01)
        assert not cancellation.done() and "STOP" not in engine.device.actions
        release.set()
        assert (await cancellation)["input_quiescent"]
        assert engine.device.actions[-2:] == ["SELECT", "STOP"]
        assert not engine.device.playing
    finally:
        release.set()
        if cancellation:
            await asyncio.gather(cancellation, return_exceptions=True)
        await service.stop()


async def test_inference_has_total_deadline_and_getter_does_not_refresh_evidence(tmp_path):
    async def stuck(_):
        await asyncio.Event().wait()

    configuration = settings(tmp_path, model_timeout=0.05)
    client = VisionClient(configuration.executor, transport=httpx.MockTransport(stuck))
    try:
        with pytest.raises(ExecutorError) as error:
            await client.observe(Frame(b"jpeg", "hash", datetime.now(UTC), PACKAGE, []))
        assert error.value.code == "model_unavailable" and not client.lock.locked()
    finally:
        await client.close()
    service = Controller(configuration)
    store = service.executor.store
    try:
        report, _ = store.submit(payload())
        token = report["token"]
        sample = (await Device().capture(), scene(playing=True))
        request = payload()
        request["request_id"] = report["request_id"]
        assert service.executor.save_observation(
            token, request, request["allowed_viewing_options"][0], sample, verified=True
        )
        with store.db.transaction() as db:
            row = store.get_row(db, token)
            saved = json.loads(row["report"])
            saved["observation"]["valid_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            store.write(db, token, saved)
        fresh = store.report(token)
        assert fresh["observation"]["observed_at"] == sample[0].captured_at.isoformat()
        assert not fresh["observation"]["verified"] and fresh["observation_status"]["state"] == "stale"
        assert fresh["content_status"]["effective_state"] == "unknown"
    finally:
        await service.stop()


@pytest.mark.parametrize(
    "reply",
    [
        {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
        {
            "choices": [
                {"finish_reason": "stop", "message": {"content": "<answer>UP</answer><answer>OK</answer>"}}
            ]
        },
        {"choices": [{"finish_reason": "stop", "message": {"content": "<answer>HOME</answer>"}}]},
    ],
)
async def test_actor_rejects_truncation_multiple_answers_and_global_commands(reply):
    config = ExecutorConfig(
        base_url="http://model.invalid/v1",
        actor_model="actor",
        observer_model="observer",
        actor_protocol="tvtheseus",
    )
    client = VisionClient(config, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=reply)))
    frame = Frame(b"jpeg", "hash", datetime.now(UTC), PACKAGE, [])
    try:
        request = payload()
        with pytest.raises(ExecutorError):
            await client.decide(frame, request, request["allowed_viewing_options"][0], [])
    finally:
        await client.close()


async def test_subprocess_adb_commands_quote_queries_capture_and_confirm_stop(tmp_path):
    import io
    import shlex
    import sys

    from PIL import Image

    from controller.executor.adb import AdbDevice

    image = io.BytesIO()
    Image.new("RGB", (1920, 1080), (75, 120, 200)).save(image, "PNG")
    (tmp_path / "screen.png").write_bytes(image.getvalue())
    program = tmp_path / "adb-fixture"
    program.write_text(
        "#!"
        + sys.executable
        + "\n"
        + """
import json, shlex, sys
from pathlib import Path
root=Path(__file__).parent
args=sys.argv[3:]
with (root/'calls.jsonl').open('a') as out: out.write(json.dumps(args)+'\\n')
if args == ['get-state']: print('device')
elif args == ['exec-out','screencap','-p']: sys.stdout.buffer.write((root/'screen.png').read_bytes())
elif args and args[0]=='shell':
 words=shlex.split(args[1])
 if words[:2]==['am','force-stop']: (root/'stopped').touch()
 elif words[:3]==['dumpsys','activity','activities']:
  print('mResumedActivity: ActivityRecord{ u0 '+('com.amazon.tv.launcher/.Home' if (root/'stopped').exists() else 'com.amazon.firebat/.Main')+' }')
 elif words==['dumpsys','media_session']:
  print('MEDIA SESSION SERVICE')
  if not (root/'stopped').exists(): print('  package=com.amazon.firebat\\n  active=true\\n  state=PlaybackState {state=3, position=2500, updated=999}')
"""
    )
    program.chmod(0o755)
    configuration = replace(settings(tmp_path), screen_adb_path=str(program))
    device = AdbDevice(configuration)
    await device.ready()
    malicious = "Jets Lions'; input keyevent 3; $(echo no)"
    await device.launch()
    await device.search(malicious)
    await device.key("RIGHT")
    frame = await device.capture()
    assert frame.foreground == PACKAGE and frame.image[:2] == b"\xff\xd8"
    await device.stop()
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    search = next(shlex.split(c[1]) for c in calls if c[0] == "shell" and "amzn://" in c[1])
    assert search[search.index("-d") + 1].startswith("amzn://pvde/search?phrase=Jets%20Lions")
    assert ";" not in search[search.index("-d") + 1]
    assert (tmp_path / "stopped").exists()


async def test_native_focus_changed_while_waiting_for_shared_input_gate_sends_no_key(tmp_path):
    from controller.executor.accessibility import FocusState, associate_capture
    from controller.executor.adb import AdbDevice

    service = Controller(settings(tmp_path))
    engine = service.executor
    state = FocusState()
    state.begin_action("OBSERVE", 100)

    def line(at, text):
        return (
            f"EventType: TYPE_VIEW_FOCUSED; EventTime: {at}; "
            f"PackageName: com.amazon.firebat; ClassName: button; Text: [{text}]"
        )

    state.ingest(line(101, "Before"))

    class NativeDevice(Device):
        accessibility = state
        validate_frame = AdbDevice.validate_frame

        async def prepare_input(self, action, *, frame=None):
            assert engine.input_lock.locked()
            self.validate_frame(frame)
            return self.accessibility.begin_action(action, 200)

    engine.device, engine.vision = NativeDevice(), Vision()
    report, _ = engine.store.submit(payload())
    snapshot = state.snapshot()
    frame = replace(
        await engine.device.capture(), native_focus=associate_capture(snapshot, snapshot, snapshot)
    )
    await engine.input_lock.acquire()
    task = asyncio.create_task(
        engine.input(report["token"], "SELECT", lambda: engine.device.key("SELECT"), frame=frame)
    )
    await asyncio.sleep(0.01)
    state.ingest(line(102, "External remote changed focus"))
    engine.input_lock.release()
    try:
        with pytest.raises(ExecutorError) as error:
            await task
        assert error.value.code == "stale_navigation_focus"
        assert engine.device.actions == []
        with engine.db.transaction() as db:
            assert db.execute("SELECT count(*) FROM executor_actions").fetchone()[0] == 0
        snapshot = state.snapshot()
        fresh = replace(frame, native_focus=associate_capture(snapshot, snapshot, snapshot))
        await engine.input(report["token"], "RIGHT", lambda: engine.device.key("RIGHT"), frame=fresh)
        assert engine.device.actions == ["RIGHT"]
    finally:
        await service.stop()


async def test_native_focus_changed_during_observer_rejects_reading(tmp_path):
    from controller.executor.accessibility import FocusState, associate_capture
    from controller.executor.adb import AdbDevice

    service = Controller(settings(tmp_path))
    engine = service.executor
    state = FocusState()
    state.begin_action("OBSERVE", 100)
    state.ingest("EventType: TYPE_VIEW_FOCUSED; EventTime: 101; PackageName: com.amazon.firebat; Text: [A]")

    class NativeDevice(Device):
        accessibility = state
        validate_frame = AdbDevice.validate_frame

        async def capture(self):
            snapshot = state.snapshot()
            return replace(
                await super().capture(), native_focus=associate_capture(snapshot, snapshot, snapshot)
            )

    engine.device, engine.vision = NativeDevice(), Vision()
    engine.vision.block = asyncio.Event()
    report, _ = engine.store.submit(payload())
    reading = asyncio.create_task(engine.read_scene(report["token"], navigation=True))
    await engine.vision.entered.wait()
    state.ingest("EventType: TYPE_VIEW_FOCUSED; EventTime: 102; PackageName: com.amazon.firebat; Text: [B]")
    engine.vision.block.set()
    try:
        with pytest.raises(ExecutorError) as error:
            await reading
        assert error.value.code == "stale_navigation_focus"
        assert engine.device.actions == []
    finally:
        await service.stop()


@pytest.mark.parametrize("protocol", ["json", "tvtheseus"])
async def test_native_metadata_reaches_actor_but_goal_blind_observer_remains_separate(tmp_path, protocol):
    from controller.executor.accessibility import FocusState, associate_capture

    state = FocusState()
    state.begin_action("RIGHT", 100)
    state.ingest(
        "EventType: TYPE_VIEW_FOCUSED; EventTime: 101; PackageName: com.amazon.firebat; "
        "ClassName: button; Text: [Native-only canary]; ContentDescription: [Search Suggestions] Native-only canary"
    )
    snapshot = state.snapshot()
    frame = Frame(
        b"jpeg", "hash", datetime.now(UTC), PACKAGE, [], associate_capture(snapshot, snapshot, snapshot)
    )
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        content = (
            json.dumps(scene().model_dump())
            if body["model"] == "observer"
            else ("<answer>RIGHT</answer>" if protocol == "tvtheseus" else '{"action":"RIGHT"}')
        )
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        )

    client = VisionClient(
        settings(tmp_path, actor_protocol=protocol).executor, transport=httpx.MockTransport(handle)
    )
    try:
        await client.observe(frame)
        request = payload()
        assert await client.decide(frame, request, request["allowed_viewing_options"][0], []) == "RIGHT"
    finally:
        await client.close()
    assert "Native-only canary" not in json.dumps(calls[0])
    text = calls[1]["messages"][-1]["content"][1]["text"]
    metadata = json.loads(text)["native_focus"]
    assert metadata["source"] == "prime-accessibility"
    assert metadata["descriptionContext"] == "Search Suggestions"
    assert metadata["currentFocusEstablished"] is True
    assert metadata["captureAssociation"]["status"] == "unchanged-during-capture"


async def test_read_only_diagnostic_closes_native_reader_when_capture_fails(tmp_path, monkeypatch):
    import controller.executor.check as diagnostic

    class FailedDevice(Device):
        closed = False

        async def capture(self):
            raise ExecutorError("protected_or_blank_frame", "fixture capture failed")

        async def close(self):
            self.closed = True

    device = FailedDevice()
    monkeypatch.setattr(diagnostic, "AdbDevice", lambda _: device)
    with pytest.raises(ExecutorError):
        await diagnostic.check(settings(tmp_path), observe=True)
    assert device.closed and device.actions == []


async def test_pre_input_io_cannot_extend_frame_lifetime(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import controller.executor.runtime as runtime

    service = Controller(settings(tmp_path, frame_max_age=0.1))
    engine = service.executor
    now = [0.01]
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))

    class SlowBoundary(Device):
        async def prepare_input(self, action, *, frame=None):
            now[0] = 0.2  # Device-clock I/O took the frame past its fixed lifetime.

    engine.device, engine.vision = SlowBoundary(), Vision()
    report, _ = engine.store.submit(payload())
    frame = replace(await engine.device.capture(), captured_monotonic=0)
    try:
        with pytest.raises(ExecutorError) as error:
            await engine.input(report["token"], "SELECT", lambda: engine.device.key("SELECT"), frame=frame)
        assert error.value.code == "stale_navigation_frame"
        assert engine.device.actions == []
        with engine.db.transaction() as db:
            assert db.execute("SELECT count(*) FROM executor_actions").fetchone()[0] == 0
    finally:
        await service.stop()


async def test_focus_change_in_watch_live_confirmation_recaptures_without_reactivating(tmp_path, monkeypatch):
    service = Controller(settings(tmp_path))
    engine = service.executor
    engine.device, engine.vision = Device(), Vision()
    report, _ = engine.store.submit(payload())
    with engine.db.transaction() as db:
        row = dict(engine.store.get_row(db, report["token"]))
    request, timezone = engine.context(row)
    original = engine.read_scene
    calls = 0

    async def reading(token, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:  # Result, result confirmation, action menu, Watch Live confirmation.
            raise ExecutorError("stale_navigation_focus", "fixture focus changed during observer")
        return await original(token, **kwargs)

    monkeypatch.setattr(engine, "read_scene", reading)
    try:
        assert await engine.navigate_query(
            report["token"], request, request["allowed_viewing_options"][0], "Jets Lions", timezone
        )
        assert calls == 7 and engine.device.actions == ["SELECT", "SELECT"]
        assert engine.store.report(report["token"])["operation"]["state"] == "playing_verified"
    finally:
        await service.stop()


@pytest.mark.parametrize("newer_owner", [False, True])
async def test_cancel_before_first_input_closes_reader_but_old_cancel_preserves_new_owner(
    tmp_path, newer_owner
):
    service = Controller(settings(tmp_path))
    engine = service.executor

    class ReaderDevice(Device):
        closed = False
        close_under_gate = False

        async def close(self):
            self.closed = True
            self.close_under_gate = engine.input_lock.locked()

    engine.device, engine.vision = ReaderDevice(), Vision()
    first, _ = engine.store.submit(payload())
    engine.store.request_cancel(CancelRequest(device_id="living-room", token=first["token"]))
    if newer_owner:
        newer, _ = engine.store.submit(payload(3))
        engine.store.journal(newer["token"], "RIGHT", "newer-frame")
    try:
        await engine.cancel_one(first["token"])
        assert engine.device.closed is (not newer_owner)
        if not newer_owner:
            assert engine.device.close_under_gate
        assert engine.device.actions == []
        assert engine.store.report(first["token"])["cancellation"]["state"] == "acknowledged"
    finally:
        await service.stop()
