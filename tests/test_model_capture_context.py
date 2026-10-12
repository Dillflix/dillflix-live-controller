import asyncio
import copy
import json

import httpx
import pytest

from controller.executor.config import ExecutorConfig
from controller.executor.models import ExecutorError
from controller.llm import ChatClient
from controller.model_diagnostics import body_evidence, capture_model_calls, model_decision


def client(handler=None):
    def respond(request):
        return httpx.Response(
            200,
            json={
                "model": "teacher-resolved-revision",
                "usage": {"prompt_tokens": 50, "completion_tokens": 12},
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": '{"ok": true}'}}
                ],
            },
        )

    return ChatClient(
        ExecutorConfig(base_url="https://model.invalid/v1", api_key="private-api-secret"),
        transport=httpx.MockTransport(handler or respond),
    )


@pytest.mark.parametrize("invalid", [False, True])
async def test_transport_and_validation_are_distinct_preserved_phases(invalid):
    archive, diagnostics = [], []
    model = client()
    try:
        with capture_model_calls(
            lambda r: diagnostics.append(copy.deepcopy(r)),
            recorder=lambda r: archive.append(copy.deepcopy(r)),
            context={"source": "catalogue", "content_id": "event-1"},
        ):
            try:
                with model_decision():
                    await model.completion(
                        "teacher", [{"role": "user", "content": "Choose a feed"}], name="broadcast_selection"
                    )
                    if invalid:
                        raise ExecutorError("prime_selection_invalid", "Unsupported ID")
            except ExecutorError:
                assert invalid
        assert len({r["call_id"] for r in archive}) == 1
        assert [r["state"] for r in archive] == ["pending", "returned", "returned"]
        assert archive[1]["validation"]["state"] == "not_checked"
        final = archive[-1]
        assert final["validation"]["state"] == ("rejected" if invalid else "passed")
        assert final["context"] == {"source": "catalogue", "content_id": "event-1"}
        assert final["task"] == "broadcast_selection"
        assert final["request"]["json"]["model"] == "teacher"
        assert final["response"]["json"]["model"] == "teacher-resolved-revision"
        assert final["response"]["json"]["usage"]["prompt_tokens"] == 50
        assert len(final["request_sha256"]) == 64 and final["controller_version"]
        assert diagnostics == archive
        assert "private-api-secret" not in json.dumps(archive)
    finally:
        await model.close()


async def test_capture_failure_does_not_change_answer_or_repeat_call(caplog):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        )

    def broken(record):
        raise RuntimeError("sensitive-provider-detail-must-not-appear-in-log")

    model = client(respond)
    try:
        with capture_model_calls(broken, recorder=broken):
            with model_decision():
                assert await model.completion("teacher", []) == "{}"
        assert len(calls) == 1
        assert "Model capture sink failed" in caplog.text
        assert "sensitive-provider-detail" not in caplog.text
    finally:
        await model.close()


async def test_cancelled_call_is_archived_without_passing_validation():
    entered = asyncio.Event()
    records = []

    async def respond(request):
        entered.set()
        await asyncio.Future()

    model = client(respond)

    async def invoke():
        with capture_model_calls(None, recorder=lambda r: records.append(copy.deepcopy(r))):
            with model_decision():
                await model.completion("teacher", [])

    task = asyncio.create_task(invoke())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [r["state"] for r in records] == ["pending", "cancelled"]
        assert records[-1]["validation"]["state"] == "not_checked"
    finally:
        await model.close()


def test_embedded_prompt_json_credentials_are_redacted_before_storage():
    prompt = json.dumps(
        {"target": {"title": "Game", "api_key": "prompt-secret", "password": "password-secret"}}
    )
    value = {"messages": [{"role": "user", "content": prompt}], "model": "teacher"}
    record = body_evidence(json.dumps(value).encode(), "wire-secret", 10000)
    assert record["redacted"] and not record["truncated"]
    assert all(secret not in json.dumps(record) for secret in ("prompt-secret", "password-secret"))
    assert json.loads(record["json"]["messages"][0]["content"])["target"]["title"] == "Game"
    unchanged = body_evidence(b'{"messages": [], "model": "teacher"}', "wire-secret", 10000)
    assert unchanged["redacted"] is False


async def test_concurrent_decisions_keep_context_and_validation_separate():
    records = []
    model = client()

    async def invoke(content_id, invalid):
        with capture_model_calls(
            None, recorder=lambda r: records.append(copy.deepcopy(r)), context={"content_id": content_id}
        ):
            with model_decision():
                await model.completion("teacher", [{"role": "user", "content": content_id}])
                if invalid:
                    raise ExecutorError("prime_selection_invalid", "Unsupported answer")

    try:
        await asyncio.gather(invoke("first", False), invoke("second", True), return_exceptions=True)
        final = {r["context"]["content_id"]: r for r in records}
        assert final["first"]["validation"]["state"] == "passed"
        assert final["second"]["validation"]["state"] == "rejected"
        assert final["first"]["call_id"] != final["second"]["call_id"]
    finally:
        await model.close()


@pytest.mark.parametrize(
    "raw,truncated",
    [
        (b'{"password":"private","padding":"' + b"x" * 100 + b'"}', False),
        (b'{"password":"private","padding":"incomplete', True),
        (b"<html>password=private</html>", False),
        (json.dumps({"content": '{"password":"private",broken'}).encode(), False),
    ],
)
def test_truncated_or_malformed_bodies_never_bypass_structured_redaction(raw, truncated):
    record = body_evidence(raw, "", 40, truncated=truncated)
    assert "private" not in json.dumps(record)
    assert record["redacted"] is True
