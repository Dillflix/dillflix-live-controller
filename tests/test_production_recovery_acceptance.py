import copy
import importlib.util
import io
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('recovery_acceptance', Path(__file__).parents[1] / 'tools/production_recovery_acceptance.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_acceptance_requires_fresh_live_evidence_from_new_session():
    now = datetime.now(timezone.utc)
    observation = dict(device_id='second-tv', content_id='event', request_id='request', verified=True, simulated=False,
                       presentation='live', observed_at=now.isoformat(),
                       valid_until=(now + timedelta(seconds=60)).isoformat(),
                       evidence={'evidence_id': 'prime:new-session:attempt'})
    snapshot = {'device': {'id': 'second-tv', 'automation': 'active', 'observed': observation},
                'reports': [{'device_id': 'second-tv', 'observation': observation}]}
    assert module.verified(snapshot, 'event', now.timestamp() - 1, 'new-session')
    assert not module.verified(snapshot, 'event', now.timestamp() + 1, 'new-session')
    assert not module.verified(snapshot, 'event', 0, 'old-session')
    for key, value in [('simulated', True), ('verified', False), ('presentation', 'replay'),
                       ('device_id', 'living-room'), ('content_id', 'other'),
                       ('valid_until', (now - timedelta(seconds=1)).isoformat())]:
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


def test_instance_paths_keep_legacy_default_and_scope_second_player(tmp_path, monkeypatch):
    monkeypatch.setattr(module.Path, 'home', lambda: tmp_path)
    root = tmp_path / '.local/state/dillflix-prime-player'
    assert module.instance_paths('living-room') == (root, 'dillflix-prime-player.service')
    assert module.instance_paths('second-tv') == (root / 'instances/second-tv', 'dillflix-prime-player@second-tv.service')
    assert module.instance_paths('second-tv', tmp_path / 'custom', 'custom-player.service') == (
        tmp_path / 'custom', 'custom-player.service')
    with pytest.raises(ValueError):
        module.instance_paths('../living-room')


def test_snapshot_reads_only_selected_configured_stream(tmp_path, monkeypatch):
    path = tmp_path / 'controller.sqlite'
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE devices(id,payload); CREATE TABLE executor_jobs(device_id,report); '
                     'CREATE TABLE metadata(key,value); CREATE TABLE activity(device_id,kind,sequence,at,message,detail);')
    for device_id in ('living-room', 'second-tv'):
        db.execute('INSERT INTO devices VALUES (?,?)', (device_id, json.dumps({'id': device_id})))
        db.execute('INSERT INTO executor_jobs VALUES (?,?)', (device_id, json.dumps({'device_id': device_id})))
        db.execute('INSERT INTO metadata VALUES (?,?)', ('prime_catalogue_probe:' + device_id, json.dumps({'id': device_id})))
        db.execute('INSERT INTO activity VALUES (?,?,?,?,?,?)', (device_id, 'prime_catalogue', 1, 'now', device_id, 'detail'))
    db.commit()
    db.close()
    monkeypatch.setenv('CONTROLLER_DATABASE', str(path))
    monkeypatch.setenv('CONTROLLER_MODE', 'demo')
    monkeypatch.delenv('CONTROLLER_DEVICES_FILE', raising=False)
    monkeypatch.setenv('CONTROLLER_DEVICES_JSON', json.dumps([
        {'id': key, 'name': key, 'adb_serial': key + ':5555'} for key in ('living-room', 'second-tv')]))
    monkeypatch.setattr(sys, 'argv', ['-c', 'second-tv'])
    output = io.StringIO()
    with redirect_stdout(output):
        exec(module.SNAPSHOT, {})
    snapshot = json.loads(output.getvalue())
    assert snapshot['device']['id'] == 'second-tv'
    assert snapshot['configured_serial'] == 'second-tv:5555'
    assert snapshot['reports'] == [{'device_id': 'second-tv'}]
    assert snapshot['catalogue_probe'] == {'id': 'second-tv'}
    assert snapshot['catalogue_history'][0][1] == 'second-tv'


def test_mismatched_player_socket_never_injects_fault(tmp_path, monkeypatch):
    calls = []

    def run(args, timeout=15):
        calls.append(args)
        if args[:4] == ['docker', 'compose', 'exec', '-T']:
            assert args[-1] == 'second-tv'
            return json.dumps({'device': {'id': 'second-tv'}, 'configured_serial': 'second:5555', 'reports': []})
        if args[-1] == 'health':
            assert str(Path('instances') / 'second-tv' / 'player.sock') in args[-2]
            return json.dumps({'serial': 'first:5555'})
        assert args[:3] == ['docker', 'compose', 'logs']
        return ''

    monkeypatch.setattr(module, 'run', run)
    monkeypatch.setattr(sys, 'argv', ['acceptance', '--device-id', 'second-tv', '--serial', 'second:5555',
                                    '--interrupt-playback', '--output', str(tmp_path / 'run')])
    assert module.main() == 1
    result = json.loads((tmp_path / 'run/result.json').read_text())
    assert result['fault_injected'] is False
    assert 'disagree' in result['error']
    assert not any(call[0] in {'adb', 'sudo'} for call in calls)


def test_legacy_stack_installer_rejects_registry_before_reinstall(monkeypatch):
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace())
    source = Path(__file__).parents[1] / 'tools/install-prime-stack.py'
    spec = importlib.util.spec_from_file_location('legacy_stack_install', source)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    installer.require_legacy_deployment('SCREEN_ADB_SERIAL=first:5555\nCONTROLLER_DEVICES_FILE=""\n')
    for line in ['CONTROLLER_DEVICES_FILE="/run/prime-player/devices.json"',
                 "  export CONTROLLER_DEVICES_JSON='[]'"]:
        with pytest.raises(ValueError, match='add-stream.py'):
            installer.require_legacy_deployment(line)
