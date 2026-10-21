"""Real chooser transport is archived without changing workflow authority."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from playback_fixtures import payload
from test_prime_catalogue import saved, setup_selection
from test_prime_matching import GTI, results, tile
from test_prime_workflow import cleanup, launch, rig, row

from controller.executor.models import CancelRequest
from controller.llm import ChatClient
from controller.prime_player.matching import EventMatcher


def captures(controller):
    with controller.db.transaction() as db:
        return [
            json.loads(record[0])
            for record in db.execute("SELECT payload FROM chooser_captures ORDER BY rowid")
        ]


async def install_transport(workflow, *, invalid_task=None, entered=None, release=None):
    """Serve scripted teacher answers over the production HTTP/validation path."""
    original_search = workflow.player.search
    exchanged = []

    async def search(*args):
        # Neither the exact title nor a supplied alias: event identity needs the model.
        return {**await original_search(*args), **results(tile("NY Jets @ Detroit"))}

    async def respond(request):
        body = json.loads(request.content)
        task = body["response_format"]["json_schema"]["name"]
        candidate = json.loads(body["messages"][1]["content"])["candidates"][0]
        choice = {
            "match_status": "matched",
            "content_id": GTI + "-invented" if task == invalid_task else candidate["content_id"],
            "reason": "Scripted fixture decision",
            "evidence": [{"field": "title", "quote": candidate["title"]}],
        }
        if task == "event_match":
            choice["viewing_option_id"] = "prime-option"
        else:
            assert task == "broadcast_selection"
            choice["language"] = "unlabeled"
            if entered is not None:
                entered.set()
                await release.wait()
        envelope = {
            "id": f"fixture-completion-{len(exchanged)}",
            "model": "fixture-teacher",
            "choices": [{
                "message": {"role": "assistant", "content": json.dumps(choice)},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }
        exchanged.append({"request": body, "response": envelope})
        return httpx.Response(200, json=envelope)

    config = replace(workflow.config, prime_match_model="fixture-teacher")
    await workflow.matcher.close()
    workflow.matcher = EventMatcher(config, model=ChatClient(config, transport=httpx.MockTransport(respond)))
    workflow.player.search = search
    return exchanged


@pytest.mark.parametrize("source", ["catalogue", "playback"])
async def test_model_calls_are_automatically_captured_in_both_workflows(tmp_path, monkeypatch, source):
    controller, workflow = rig(tmp_path)
    exchanged = await install_transport(workflow)
    try:
        if source == "catalogue":
            setup_selection(controller, monkeypatch)
            controller.stage_playback()
            _, probe = saved(controller)
            await workflow.observe_catalogue(probe)
            assert saved(controller)[1]["state"] == "ready"
            assert not any(call[0] == "play" for call in workflow.player.calls)
            correlation = {"catalogue_probe_id": probe["id"], "intent_version": probe["intent"]}
        else:
            token = await launch(workflow)
            report = workflow.store.report(token)
            assert report["operation"]["state"] == "playing_verified"
            correlation = {
                "token": token,
                "request_id": report["request_id"],
                "intent_version": report["intent_version"],
            }

        records = captures(controller)
        # Pending, HTTP completion, and local validation update the same two calls.
        assert len(records) == len(exchanged) == 2
        assert len({record["call_id"] for record in records}) == 2
        assert [record["task"] for record in records] == ["event_match", "broadcast_selection"]
        for record, wire in zip(records, exchanged, strict=True):
            assert record["state"] == "returned"
            assert record["validation"]["state"] == "passed"
            assert record["request"]["json"] == wire["request"]
            assert record["response"]["json"] == wire["response"]
            assert record["http_status"] == 200
            assert record["controller_version"]
            assert record["finished_at"] >= record["started_at"]
            assert record["context"] == {
                "source": source, "device_id": "living-room", "content_id": "fixture-game", **correlation,
            }
        assert workflow.model_captures.status()["count"] == 2
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("source", ["catalogue", "playback"])
@pytest.mark.parametrize("invalid_task", ["event_match", "broadcast_selection"])
async def test_rejected_teacher_ids_remain_captured_without_playback(
    tmp_path, monkeypatch, source, invalid_task,
):
    controller, workflow = rig(tmp_path)
    exchanged = await install_transport(workflow, invalid_task=invalid_task)
    try:
        if source == "catalogue":
            setup_selection(controller, monkeypatch)
            controller.stage_playback()
            _, probe = saved(controller)
            await workflow.observe_catalogue(probe)
            assert saved(controller)[1]["state"] == "access_unknown"
        else:
            token = await launch(workflow)
            assert workflow.store.report(token)["operation"]["state"] in {"failed", "access_unknown"}

        records = captures(controller)
        assert len(records) == len(exchanged)
        rejected = [record for record in records if record["validation"]["state"] == "rejected"]
        assert len(rejected) == 1
        record = rejected[0]
        assert record["task"] == invalid_task
        assert record["state"] == "returned"  # Transport success is distinct from accepting the answer.
        assert record["validation"]["error_code"] == (
            "prime_selection_invalid" if invalid_task == "event_match" else "prime_broadcast_selection_invalid"
        )
        answer = json.loads(record["response"]["json"]["choices"][0]["message"]["content"])
        assert answer["content_id"] == GTI + "-invented"
        assert record["request"]["json"] == exchanged[-1]["request"]
        assert not any(call[0] == "play" for call in workflow.player.calls)
    finally:
        await cleanup(controller)


async def test_late_catalogue_answer_is_retained_but_cannot_replace_new_intent(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    await install_transport(workflow, entered=entered, release=release)
    setup_selection(controller, monkeypatch)
    task = None
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        task = asyncio.create_task(workflow.observe_catalogue(probe))
        await asyncio.wait_for(entered.wait(), 2)
        assert captures(controller)[-1]["state"] == "pending"
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            device["intent_version"] += 1
            controller.db.save_device(db, device)
        release.set()
        await asyncio.wait_for(task, 2)

        records = captures(controller)
        assert len(records) == 2
        assert records[-1]["state"] == "returned"
        assert records[-1]["validation"]["state"] == "passed"
        assert records[-1]["context"]["intent_version"] == probe["intent"]
        assert saved(controller)[1]["state"] == "discarded"
        assert not any(call[0] in {"play", "stop", "cancel"} for call in workflow.player.calls)
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await cleanup(controller)


async def test_cancelled_playback_keeps_inflight_capture_without_reauthorizing_launch(tmp_path):
    controller, workflow = rig(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    await install_transport(workflow, entered=entered, release=release)
    report, _ = workflow.store.submit(payload())
    token = report["token"]
    task = asyncio.create_task(workflow.navigate(row(workflow, token)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        pending = captures(controller)[-1]
        assert pending["state"] == "pending" and pending["response"] is None
        workflow.store.request_cancel(CancelRequest(device_id="living-room", token=token))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        records = captures(controller)
        assert len(records) == 2
        assert records[-1]["call_id"] == pending["call_id"]
        assert records[-1]["state"] == "cancelled"
        assert records[-1]["validation"]["state"] == "not_checked"
        assert records[-1]["context"]["token"] == token
        assert row(workflow, token)["cancel_requested"]
        assert not any(call[0] == "play" for call in workflow.player.calls)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await cleanup(controller)
