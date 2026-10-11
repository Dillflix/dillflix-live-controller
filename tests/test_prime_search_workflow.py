"""Fallback uses the same execution fences in preflight and direct Play."""

import json
from datetime import UTC, datetime

import pytest
from playback_fixtures import payload
from test_prime_catalogue import saved, setup_selection
from test_prime_matching import results, tile
from test_prime_workflow import cleanup, launch, rig, row

from controller.executor.models import CancelRequest
from controller.prime_player.labels import search_queries


def install_fallback(workflow, on_miss=None):
    original = workflow.player.search
    queries = search_queries(payload()["content_snapshot"])

    async def search(query, *args):
        result = await original(query, *args)
        if query == queries[0]:
            if on_miss:
                on_miss()
            return {**result, **results(tile("Rainbow: In Performance", content_type="MOVIE"))}
        return result

    workflow.player.search = search
    return queries


async def test_direct_launch_uses_fallback_and_keeps_search_audits(tmp_path):
    controller, workflow = rig(tmp_path)
    queries = install_fallback(workflow)
    try:
        token = await launch(workflow)
        report = workflow.store.report(token)
        assert report["operation"]["state"] == "playing_verified"
        assert [c[1] for c in workflow.player.calls if c[0] == "search"] == queries[:2]
        assert report["prime_player"]["query"] == queries[1]
        assert len(report["prime_player"]["selection"]["search_attempts"]) == 2
        assert [c[0] for c in workflow.player.calls].count("play") == 1
    finally:
        await cleanup(controller)


async def test_preflight_fallback_is_reused_without_new_search_or_premature_intent(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    queries = install_fallback(workflow)
    _, _, intent = setup_selection(controller, monkeypatch)
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        device, probe = saved(controller)
        assert device["intent_version"] == intent and device["observed"]["verified"]
        assert probe["query"] == queries[1] and probe["state"] == "ready"
        assert [a["query"] for a in probe["audit"]["search_attempts"]] == queries[:2]
        controller.stage_playback()
        with controller.db.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE state='pending'").fetchone()
            body = json.loads(job["payload"])
            body["deadline_at"] = datetime.fromtimestamp(job["deadline_at"], UTC).isoformat()
        report, _ = workflow.store.submit(body)
        await workflow.navigate(row(workflow, report["token"]))
        report = workflow.store.report(report["token"])
        assert report["operation"]["state"] == "playing_verified"
        assert report["prime_player"]["query"] == queries[1]
        assert len(report["prime_player"]["selection"]["search_attempts"]) == 2
        assert [c[1] for c in workflow.player.calls if c[0] == "search"] == queries[:2]
    finally:
        await cleanup(controller)


@pytest.mark.parametrize("change", ["manual", "intent"])
async def test_plan_change_after_first_query_prevents_fallback_rpc(tmp_path, monkeypatch, change):
    controller, workflow = rig(tmp_path)
    setup_selection(controller, monkeypatch)

    def alter():
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            if change == "manual":
                device["manual_control"] = {"session_id": "manual"}
            else:
                device["intent_version"] += 1
            controller.db.save_device(db, device)

    queries = install_fallback(workflow, alter)
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        device, probe = saved(controller)
        assert probe["state"] == "discarded"
        assert workflow.player.calls == [("search", queries[0])]
        assert not device.get("prime_access")
    finally:
        await cleanup(controller)


async def test_cancel_after_first_query_prevents_direct_fallback_rpc(tmp_path):
    controller, workflow = rig(tmp_path)
    report, _ = workflow.store.submit(payload())
    token = report["token"]
    queries = install_fallback(
        workflow, lambda: workflow.store.request_cancel(CancelRequest(device_id="living-room", token=token))
    )
    try:
        await workflow.navigate(row(workflow, token))
        assert workflow.player.calls == [("search", queries[0])]
        assert workflow.store.report(token)["operation"]["state"] != "playing_verified"
    finally:
        await cleanup(controller)
