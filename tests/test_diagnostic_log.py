import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from controller.diagnostic_log import LogHandler, Recorder, read_logs
from controller.executor.models import ExecutorError
from controller.prime_player.client import PrimePlayerClient, rpc_context


def test_persistent_rotation_truncation_and_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECUTOR_LLM_API_KEY", "test-private-credential")
    monkeypatch.setattr("controller.diagnostic_log.FILE_BYTES", 2000)
    recorder = Recorder(tmp_path)
    for i in range(25):
        recorder.record(
            "rpc",
            "response",
            i=i,
            body="x" * 300,
            api_key="hidden",
            error="Bearer abcdefg test-private-credential",
        )
    recorder.close()
    snapshot = read_logs(tmp_path, limit=1500)
    wire = json.dumps(snapshot)
    assert "test-private-credential" not in wire and "hidden" not in wire and "abcdefg" not in wire
    assert snapshot["sources"]["rpc"]["export_truncated"]
    assert snapshot["sources"]["rpc"]["records"][-1]["data"]["i"] == 24
    assert len(list(tmp_path.glob("rpc.jsonl*"))) == 3
    assert (tmp_path / "rpc.jsonl").stat().st_mode & 0o777 == 0o640
    second = Recorder(tmp_path)
    second.record("rpc", "after_restart")
    second.close()
    records = read_logs(tmp_path)["sources"]["rpc"]["records"]
    assert len({r["process_id"] for r in records}) == 2


def test_missing_partial_and_oversized_records_are_explicit(tmp_path):
    assert read_logs(tmp_path)["sources"]["rpc"]["state"] == "missing"
    recorder = Recorder(tmp_path)
    recorder.record("rpc", "huge", text="☃" * 300000)
    recorder.close()
    path = tmp_path / "rpc.jsonl"
    assert path.stat().st_size <= 256 * 1024
    with path.open("a") as handle:
        handle.write('{"interrupted":')
    source = read_logs(tmp_path)["sources"]["rpc"]
    assert source["state"] == "partial"
    assert source["records"][0]["data"]["truncated"]


def test_storage_failure_does_not_break_producer(tmp_path):
    directory = tmp_path / "file"
    directory.write_text("not a directory")
    recorder = Recorder(directory)
    recorder.record("rpc", "failure")
    snapshot = recorder.snapshot()
    recorder.close()
    assert snapshot["capture_status"]["error"]
    assert snapshot["capture_status"]["dropped_records"]


@pytest.mark.asyncio
async def test_rpc_request_response_errors_and_cancellation_are_captured(tmp_path):
    recorder = Recorder(tmp_path)
    logger = logging.getLogger("controller.prime_rpc")
    handler = LogHandler(recorder)
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    seen = []

    async def serve(request):
        seen.append(request.headers["x-dillflix-call-id"])
        method = json.loads(request.content)["method"]
        if method == "play":
            return httpx.Response(409, json={"error": {"type": "PrimeError", "message": "resolver details"}})
        if method == "search":
            raise asyncio.CancelledError()
        return httpx.Response(200, json={"result": {"session_id": "session"}})

    client = PrimePlayerClient("unused", transport=httpx.MockTransport(serve))
    token = rpc_context.set({"token": "pb_test", "request_id": "request"})
    try:
        await client.rpc("manual_input", text="private typing")
        with pytest.raises(ExecutorError):
            await client.rpc("play", attempt_id="attempt")
        with pytest.raises(asyncio.CancelledError):
            await client.rpc("search", query="White Sox")
    finally:
        rpc_context.reset(token)
        await client.close()
        logger.removeHandler(handler)
        logger.setLevel(previous)
        recorder.close()
    records = [r["data"] for r in read_logs(tmp_path)["sources"]["rpc"]["records"]]
    assert len(records) == 6
    assert records[0]["call_id"] == seen[0] == records[1]["call_id"]
    assert records[0]["token"] == "pb_test"
    assert "private typing" not in json.dumps(records)
    assert records[3]["response"]["error"]["message"] == "resolver details"
    assert records[-1]["error"]["type"] == "CancelledError"


@pytest.mark.asyncio
async def test_web_export_reads_player_history_after_health_and_rpc_failure(tmp_path):
    from controller.api import create_app
    from controller.config import Settings
    from controller.executor.config import ExecutorConfig

    settings = Settings(
        database=str(tmp_path / "controller.sqlite"),
        executor=ExecutorConfig(prime_socket=str(tmp_path / "player.sock")),
    )
    app = create_app(settings, start_workers=False)
    # Existing socket-directory mount contains evidence left by the dead player.
    recorder = Recorder(tmp_path / "diagnostics")
    recorder.record("runtime", "resolver", result="non-playback")
    recorder.record("device", "logcat", text="native resolver evidence")
    recorder.close()
    calls = []

    async def rpc(method, **kwargs):
        calls.append(method)
        raise ConnectionError("player offline")

    service = app.state.controller
    service.executor = SimpleNamespace(player=SimpleNamespace(rpc=rpc), worker=None)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/devices/living-room/diagnostics")
        assert response.status_code == 200
        bundle = response.json()["prime_player"]
        assert calls == ["health", "diagnostics"]
        assert (
            bundle["persisted_logs"]["sources"]["device"]["records"][0]["data"]["text"]
            == "native resolver evidence"
        )
        assert "service_diagnostics_error" in bundle and "health_error" in bundle
    finally:
        service.executor = None
        await service.stop()


def test_overflow_is_bounded_and_counted_without_blocking(tmp_path, monkeypatch):
    import threading

    ready = threading.Event()
    original = Recorder._run

    def delayed(self):
        ready.wait(2)
        original(self)

    monkeypatch.setattr(Recorder, "_run", delayed)
    recorder = Recorder(tmp_path)
    for index in range(300):
        recorder.record("runtime", "burst", index=index)
    assert recorder.queue.qsize() == 256
    assert recorder.dropped == 45  # Includes the initial capture_started record.
    ready.set()
    recorder.close()
    assert read_logs(tmp_path)["capture_status"]["dropped_records"] == 45
