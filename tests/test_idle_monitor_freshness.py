"""Catalogue latency must not manufacture playback failure or hide native completion."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from test_idle_sports_integration import candidate, seed, EventPlayer
from test_prime_workflow import cleanup, rig, row
from test_idle_sports_renewal import expire_source


async def test_native_idle_monitor_reads_current_status_after_slow_catalogue_across_cycles(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        workflow.config = replace(workflow.config, monitor_interval=30)
        value = candidate('event')
        request = seed(controller, value)
        workflow.player = EventPlayer(value)
        report, _ = workflow.store.submit(request)
        token = report['token']
        await workflow.navigate(row(workflow, token))
        rpc, status = workflow.player.rpc, workflow.player.status
        samples, calls = [], []

        async def slow_catalogue(method, **params):
            calls.append('catalogue')
            # Reproduce the captured 17-second scan exceeding the 15-second
            # sample acceptance window, without sleeping in a regression test.
            for sample in samples:
                sample['observed_at'] = (datetime.now(UTC) - timedelta(
                    seconds=controller.settings.observation_ttl + 2)).isoformat()
            return await rpc(method, **params)

        async def current_status(*args):
            calls.append('status')
            sample = await status(*args)
            samples.append(sample)
            return sample

        workflow.player.rpc, workflow.player.status = slow_catalogue, current_status
        for _ in range(3):
            catalogue = workflow.store.report(token)['prime_player']['catalogue']
            catalogue['observed_at'] = (datetime.now(UTC) - timedelta(seconds=40)).isoformat()
            workflow.save_workflow(token, catalogue=catalogue)
            await workflow.monitor(row(workflow, token))
            checked = workflow.store.report(token)
            assert checked['observation']['verified']
            assert checked['observation_status']['state'] == 'fresh'
            assert checked['operation']['state'] == 'playing_verified'
        assert calls == ['catalogue', 'status'] * 3
        assert len([call for call in workflow.player.calls if call[0] == 'play']) == 1
    finally:
        await cleanup(controller)


@pytest.mark.parametrize('state', ['ended', 'playing'])
async def test_missing_native_guide_can_complete_only_from_bound_ended_status(tmp_path, state):
    controller, workflow = rig(tmp_path)
    try:
        value = candidate('event')
        request = seed(controller, value)
        workflow.player = EventPlayer(value)
        report, _ = workflow.store.submit(request)
        token = report['token']
        await workflow.navigate(row(workflow, token))
        original = workflow.player.rpc

        async def missing(method, **params):
            result = await original(method, **params)
            return {**result, 'events': [], 'catalogue_coverage': {'exhausted': True}}

        workflow.player.rpc = missing
        workflow.player.state = state
        await workflow.monitor(row(workflow, token))
        checked = workflow.store.report(token)
        assert (checked['content_status']['effective_state'] == 'ended') == (state == 'ended')
        assert not checked['observation']['verified']
        assert len([call for call in workflow.player.calls if call[0] == 'play']) == 1
    finally:
        await cleanup(controller)


async def test_failed_idle_attempt_releases_recovery_and_allows_discovery_to_resume(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        value = candidate('event')
        seed(controller, value)
        expire_source(controller, value['candidate_id'])
        with controller.db.transaction() as db:
            device = controller.db.device(db)
            device.update(desired=value['candidate_id'], playback_state='failed',
                observed={'request_id': 'older-verified-request', 'content_id': value['candidate_id'],
                          'viewing_option_id': 'prime_idle_live', 'verified': False},
                recovery={'content_id': value['candidate_id'], 'since': datetime.now(UTC).isoformat()})
            controller.db.save_device(db, device)
        assert controller.idle_context() is None  # Retained recovery blocks discovery.
        controller.stage_playback()
        with controller.db.snapshot() as db:
            device = controller.db.device(db)
            assert device['desired'] is None and device['observed'] is None
            assert device['recovery'] is None and device['playback_state'] == 'waiting'
            assert not db.execute("SELECT 1 FROM jobs WHERE state='pending'").fetchone()
        assert controller.idle_context() is not None
    finally:
        await cleanup(controller)
