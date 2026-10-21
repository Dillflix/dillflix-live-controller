import copy
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location('recovery_acceptance', Path(__file__).parents[1] / 'tools/production_recovery_acceptance.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_acceptance_requires_fresh_live_evidence_from_new_session():
    now = datetime.now(timezone.utc)
    observation = dict(content_id='event', request_id='request', verified=True, simulated=False,
                       presentation='live', observed_at=now.isoformat(),
                       valid_until=(now + timedelta(seconds=60)).isoformat(),
                       evidence={'evidence_id': 'prime:new-session:attempt'})
    snapshot = {'device': {'automation': 'active', 'observed': observation},
                'reports': [{'observation': observation}]}
    assert module.verified(snapshot, 'event', now.timestamp() - 1, 'new-session')
    assert not module.verified(snapshot, 'event', now.timestamp() + 1, 'new-session')
    assert not module.verified(snapshot, 'event', 0, 'old-session')
    for key, value in [('simulated', True), ('verified', False), ('presentation', 'replay'),
                       ('content_id', 'other'), ('valid_until', (now - timedelta(seconds=1)).isoformat())]:
        changed = copy.deepcopy(snapshot)
        changed['reports'][0]['observation'][key] = value
        assert not module.verified(changed, 'event', 0, 'new-session')


def test_readiness_requires_new_runtime_and_verified_network_context():
    health = {'session_id': 'new', 'state': {'ready': True},
              'network': {'enabled': True, 'state': 'ready', 'context_id': 'context'}}
    assert module.ready(health, 'old')
    assert not module.ready(health, 'new')
    health['network']['state'] = 'transitioning'
    assert not module.ready(health, 'old')
