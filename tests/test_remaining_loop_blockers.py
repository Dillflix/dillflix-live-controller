import asyncio
import sqlite3
import threading
import time

import pytest
from test_prime_workflow import cleanup, rig
from test_entitlements import seed


@pytest.mark.parametrize('operation', ['status', 'health'])
async def test_contended_status_and_health_persistence_leave_loop_responsive(tmp_path, operation):
    controller, workflow = rig(tmp_path)
    seed(controller)
    blocker = sqlite3.connect(controller.db.path, isolation_level=None)
    blocker.execute('BEGIN IMMEDIATE')
    task = asyncio.create_task(controller.refresh_status() if operation == 'status' else workflow.check_session())
    try:
        started = time.monotonic()
        await asyncio.sleep(.1)
        assert time.monotonic() - started < 1
        assert not task.done()
    finally:
        blocker.rollback()
        blocker.close()
        await asyncio.wait_for(task, 15)
        await cleanup(controller)


async def test_entitlement_progress_and_completion_are_off_loop(tmp_path, monkeypatch):
    controller, workflow = rig(tmp_path)
    seed(controller)
    loop_thread = threading.get_ident()
    called = set()
    for name in ('entitlement_progress', 'finish_entitlement'):
        original = getattr(controller, name)
        def checked(*args, _name=name, _original=original, **kwargs):
            assert threading.get_ident() != loop_thread
            called.add(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(controller, name, checked)
    try:
        await controller.check_entitlement_once()
        assert called == {'entitlement_progress', 'finish_entitlement'}
    finally:
        await cleanup(controller)
