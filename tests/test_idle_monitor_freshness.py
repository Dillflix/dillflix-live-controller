"""Catalogue latency must not manufacture playback failure or hide native completion."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json

import pytest

from test_idle_sports_integration import candidate, seed, EventPlayer
from test_prime_workflow import cleanup, rig, row
from test_idle_sports_renewal import expire_source, player


@pytest.mark.parametrize('kind', ['event', 'linear_program'])
async def test_monitor_never_ages_playback_status_during_catalogue_work(tmp_path, kind):
    controller, workflow = rig(tmp_path)
    try:
        workflow.config = replace(workflow.config, monitor_interval=30)
        value = candidate(kind)
        request = seed(controller, value)
        player(workflow, request, value)
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
            saved = workflow.store.report(token)['prime_player']
            catalogue = saved['catalogue']
            catalogue['observed_at'] = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
            if kind == 'linear_program':
                saved['selected']['linear_airing']['observed_at'] = catalogue['observed_at']
            workflow.save_workflow(token, catalogue=catalogue, selected=saved['selected'])
            await workflow.monitor(row(workflow, token))
            checked = workflow.store.report(token)
            assert checked['observation']['verified']
            assert checked['observation_status']['state'] == 'fresh'
            assert checked['operation']['state'] == 'playing_verified'
        assert calls == (['catalogue', 'status'] if kind == 'linear_program' else ['status']) * 3
        assert len([call for call in workflow.player.calls if call[0] == 'play']) == 1
    finally:
        await cleanup(controller)


@pytest.mark.parametrize('state', ['ended', 'playing'])
async def test_native_playback_and_completion_do_not_depend_on_catalogue_after_launch(tmp_path, state):
    controller, workflow = rig(tmp_path)
    try:
        value = candidate('event')
        request = seed(controller, value)
        workflow.player = EventPlayer(value)
        report, _ = workflow.store.submit(request)
        token = report['token']
        await workflow.navigate(row(workflow, token))
        async def missing(*args, **params):
            pytest.fail('Native playback monitoring must not request the catalogue')

        workflow.player.rpc = missing
        workflow.player.state = state
        await workflow.monitor(row(workflow, token))
        checked = workflow.store.report(token)
        assert (checked['content_status']['effective_state'] == 'ended') == (state == 'ended')
        assert checked['observation']['verified'] is (state == 'playing')
        assert len([call for call in workflow.player.calls if call[0] == 'play']) == 1
    finally:
        await cleanup(controller)


async def test_native_startup_accepts_bound_live_proof_after_catalogue_admission_expires(tmp_path):
    controller, workflow = rig(tmp_path)
    try:
        value = candidate('event')
        request = seed(controller, value)
        workflow.player = EventPlayer(value)
        report, _ = workflow.store.submit(request)
        token = report['token']
        play = workflow.player.play

        async def long_startup(*args):
            outcome = await play(*args)
            catalogue = workflow.store.report(token)['prime_player']['catalogue']
            catalogue['observed_at'] = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
            workflow.save_workflow(token, catalogue=catalogue)
            async def no_catalogue(*args, **kwargs):
                pytest.fail('Confirmed native startup must not trigger another catalogue check')
            workflow.player.rpc = no_catalogue
            return outcome

        workflow.player.play = long_startup
        await workflow.navigate(row(workflow, token))
        assert workflow.store.report(token)['observation']['verified']
        assert workflow.store.report(token)['operation']['state'] == 'playing_verified'
        assert len([call for call in workflow.player.calls if call[0] == 'play']) == 1
    finally:
        await cleanup(controller)


async def test_native_catalogue_expiry_before_dispatch_still_prevents_play(tmp_path, monkeypatch):
    import controller.prime_player.workflow as module
    controller, workflow = rig(tmp_path)
    try:
        value = candidate('event')
        request = seed(controller, value)
        workflow.player = EventPlayer(value)
        report, _ = workflow.store.submit(request)
        mutation = workflow.mutation
        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(seconds=61)
        async def delayed_dispatch(token, action, function):
            if action == 'PRIME_PLAY':
                monkeypatch.setattr(module, 'datetime', Later)
            return await mutation(token, action, function)
        workflow.mutation = delayed_dispatch
        await workflow.navigate(row(workflow, report['token']))
        checked = workflow.store.report(report['token'])
        assert checked['operation']['error']['code'] == 'prime_stale_result'
        assert not any(call[0] == 'play' for call in workflow.player.calls)
    finally:
        await cleanup(controller)


@pytest.mark.parametrize('fault', ['stale', 'wrong_identity', 'wrong_attempt', 'stopped'])
async def test_native_monitor_still_rejects_unbound_or_unhealthy_status(tmp_path, fault):
    controller, workflow = rig(tmp_path)
    try:
        value = candidate('event')
        request = seed(controller, value)
        workflow.player = EventPlayer(value)
        report, _ = workflow.store.submit(request)
        token = report['token']
        await workflow.navigate(row(workflow, token))
        status = workflow.player.status
        async def bad_status(*args):
            result = await status(*args)
            if fault == 'stale': result['observation_age_seconds'] = 60
            if fault == 'wrong_identity': result['current_content_id'] = 'different-event'
            if fault == 'wrong_attempt': result['attempt_id'] = 'different-attempt'
            if fault == 'stopped': result.update(state='stopped', is_playing=False)
            return result
        workflow.player.status = bad_status
        await workflow.monitor(row(workflow, token))
        checked = workflow.store.report(token)
        assert not checked['observation']['verified']
        assert checked['content_status']['effective_state'] != 'ended'
        assert len([call for call in workflow.player.calls if call[0] == 'play']) == 1
    finally:
        await cleanup(controller)


@pytest.mark.parametrize('assessment', [None, 'non_sports'])
async def test_discovery_absence_keeps_owned_native_playback_without_renewing_admission(tmp_path, assessment):
    from controller.current_playback import current_playback
    from controller.prime_player.catalogue import key
    controller, workflow = rig(tmp_path)
    try:
        value = candidate('event')
        seed(controller, value)
        workflow.player = EventPlayer(value)
        controller.stage_playback()
        with controller.db.snapshot() as db:
            probe = controller.db.meta(db, key(controller.device_id))
        await workflow.observe_catalogue(probe)
        controller.stage_playback()
        with controller.db.snapshot() as db:
            job = dict(db.execute("SELECT * FROM jobs WHERE state='pending'").fetchone())
            body = json.loads(job['payload'])
            body['deadline_at'] = datetime.fromtimestamp(job['deadline_at'], UTC).isoformat()
        report, _ = workflow.store.submit(body)
        token = report['token']
        await workflow.navigate(row(workflow, token))
        assert controller.receive_playback_report(job['id'], controller.playback.project(workflow.store.report(token)))
        expire_source(controller, value['candidate_id'])
        controller.expire_idle_rows()
        with controller.db.snapshot() as db:
            expiry = controller.db.device(db)['idle_sports']['expires_at']
            assert not db.execute('SELECT active FROM contents WHERE id=?', (value['candidate_id'],)).fetchone()[0]
        for _ in range(3):
            context = controller.idle_context(read_device=False)
            assert context
            assert controller.idle_publish(context, None,
                {'match_status': 'no_match', 'current_classification': assessment}, {})
            with controller.db.snapshot() as db:
                nomination = controller.db.device(db)['idle_sports']
                if assessment:
                    assert not nomination.get('content_id')
                    return  # An explicit reassessment can withdraw the nomination.
                assert nomination['content_id'] == value['candidate_id']
                assert nomination['expires_at'] == expiry
                assert current_playback(controller, db, controller.device_id).state == 'playing'
            controller.stage_playback()
            await workflow.monitor(row(workflow, token))
            with controller.db.transaction() as db:
                device = controller.db.device(db)
                assert device['desired'] == value['candidate_id']
                assert device['observed']['verified']
                assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
                device['idle_sports']['next_check_at'] = 0
                controller.db.save_device(db, device)
        workflow.player.state = 'ended'
        await workflow.monitor(row(workflow, token))
        assert workflow.store.report(token)['content_status']['effective_state'] == 'ended'
        controller.stage_playback()
        with controller.db.snapshot() as db:
            assert controller.db.device(db)['desired'] is None
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
