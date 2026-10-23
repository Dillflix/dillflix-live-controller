import asyncio
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime

import pytest
from playback_fixtures import payload
from test_prime_workflow import cleanup, rig, row
from test_prime_catalogue import setup_selection, saved
from controller.launch_progress import launch_progress
from controller.model_diagnostics import capture_model_calls_async, publish


async def test_matching_time_does_not_consume_prepared_handoff(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    setup_selection(controller, monkeypatch)
    original = workflow.finish_catalogue
    def slow_matching(probe, evidence):
        evidence['received_at'] -= 95  # Model/selection work outlived the old 60/90s timers.
        original(probe, evidence)
    monkeypatch.setattr(workflow, 'finish_catalogue', slow_matching)
    try:
        controller.stage_playback()
        _, probe = saved(controller)
        await workflow.observe_catalogue(probe)
        controller.stage_playback()
        _, prepared = saved(controller)
        assert prepared['expires'] - prepared['selected_at'] == 60
        with controller.db.snapshot() as db:
            job = db.execute("SELECT * FROM jobs WHERE state='pending'").fetchone()
            body = json.loads(job['payload'])
            body['deadline_at'] = datetime.fromtimestamp(job['deadline_at'], UTC).isoformat()
        report, _ = workflow.store.submit(body)
        await workflow.navigate(row(workflow, report['token']))
        assert workflow.store.report(report['token'])['operation']['state'] == 'playing_verified'
        assert [c[0] for c in workflow.player.calls].count('search') == 1
        assert workflow.prepared_catalogue(body, workflow.player.session) is not None
        monkeypatch.setattr('controller.prime_player.catalogue.time.time', lambda: prepared['expires'] + 1)
        assert workflow.prepared_catalogue(body, workflow.player.session) is None
    finally:
        await cleanup(controller)


async def test_launch_database_contention_leaves_event_loop_responsive(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    report, _ = workflow.store.submit(payload())
    queued = row(workflow, report['token'])
    loop_thread = threading.get_ident()
    original = workflow.db.transaction
    def checked():
        assert threading.get_ident() != loop_thread, 'write transaction on event loop'
        return original()
    blocker = sqlite3.connect(workflow.db.path, isolation_level=None)
    blocker.execute('BEGIN IMMEDIATE')
    monkeypatch.setattr(workflow.db, 'transaction', checked)
    task = asyncio.create_task(workflow.navigate(queued))
    try:
        start = time.monotonic()
        await asyncio.sleep(.1)
        assert time.monotonic() - start < 1
        assert not task.done()
        blocker.rollback()
        await asyncio.wait_for(task, 10)
        assert workflow.store.report(report['token'])['operation']['state'] == 'playing_verified'
    finally:
        blocker.close()
        monkeypatch.setattr(workflow.db, 'transaction', original)
        await cleanup(controller)


def test_startup_progress_is_distinct_from_playback_verification():
    op = {'phase': 'network_starting_prime', 'phase_started_at': '2026-10-22T23:03:40Z'}
    progress = launch_progress({'operation': op}, {'requested_target': 'toronto', 'startup': {'phase': 'frida_handshake'}})
    assert progress['title'] == 'Starting Prime on the connected network · toronto'
    assert progress['detail'] == 'Connecting to device control'
    assert progress['started_at'] == op['phase_started_at']
    assert 'playback' not in progress['title']
    assert launch_progress({'operation': {'phase': 'verifying'}}, {})['title'] == 'Verifying live playback'


async def test_model_captures_drain_in_order_off_loop_even_on_cancellation():
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    records = []
    def persist(record):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5)
        records.append(record['state'])
    async def capture():
        async with capture_model_calls_async(persist):
            record = {'state': 'returned'}
            publish(record)
            record['state'] = 'validated'
            publish(record)
            await asyncio.sleep(60)
    task = asyncio.create_task(capture())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(.05)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert records == ['returned', 'validated']
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
