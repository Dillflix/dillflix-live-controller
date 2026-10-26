"""Exercise deployed recovery once; never launch an isolated replacement player.

Run on the controller/player host while a live event is verified. The only fault
injection is one force-stop of Prime. No Play, VPN, restart or remote-control
commands are issued afterwards. Existing automation must restore playback.
"""
import argparse
import json
import re
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path


SNAPSHOT = '''import json,os,sqlite3,sys
from pathlib import Path
from controller.config import Settings
device_id=sys.argv[1]
binding=next((d for d in Settings.from_env().configured_devices if d.id==device_id),None)
if binding is None:
    raise RuntimeError('Device is not configured')
c=sqlite3.connect(Path(os.environ['CONTROLLER_DATABASE']).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
c.execute('BEGIN')
d=json.loads(c.execute("SELECT payload FROM devices WHERE id=?",(device_id,)).fetchone()[0])
rows=c.execute("SELECT report FROM executor_jobs WHERE device_id=? ORDER BY rowid DESC LIMIT 5",(device_id,))
reports=[json.loads(r[0]) for r in rows]
probe=c.execute("SELECT value FROM metadata WHERE key=?",('prime_catalogue_probe:'+device_id,)).fetchone()
activity=c.execute("SELECT at,message,detail FROM activity WHERE device_id=? AND kind='prime_catalogue' ORDER BY sequence DESC LIMIT 20",(device_id,)).fetchall()
print(json.dumps({'device':d,'configured_serial':binding.adb_serial,'reports':reports,'catalogue_probe':json.loads(probe[0]) if probe else None,'catalogue_history':activity}))
'''


def run(args, timeout=15):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} exited {result.returncode}: {result.stderr[-500:]}")
    return result.stdout


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError, TypeError):
        return 0


def verified(snapshot, content_id, since, session_id):
    device = snapshot['device']
    if device.get('automation') != 'active' or device.get('manual_control') or device.get('input_handoff'):
        return False
    observed = device.get('observed') or {}
    if (not device.get('id') or observed.get('device_id') != device['id']
            or not observed.get('verified') or observed.get('content_id') != content_id):
        return False
    return any(
        r.get('device_id') == device['id']
        and (r.get('observation') or {}).get('device_id') == device['id']
        and (r.get('observation') or {}).get('content_id') == content_id
        and (r.get('observation') or {}).get('verified') is True
        and (r.get('observation') or {}).get('simulated') is False
        and (r.get('observation') or {}).get('presentation') == 'live'
        and (r.get('observation') or {}).get('request_id') == observed.get('request_id')
        and str(((r.get('observation') or {}).get('evidence') or {}).get('evidence_id', '')).startswith('prime:' + session_id + ':')
        and timestamp((r.get('observation') or {}).get('observed_at')) > since
        and timestamp((r.get('observation') or {}).get('valid_until')) > time.time()
        for r in snapshot['reports']
    )


def ready(health, old_session=None):
    network = health.get('network') or {}
    return bool(
        health.get('session_id') and health.get('session_id') != old_session
        and not health.get('closed') and not health.get('failure')
        and network.get('enabled') and network.get('state') == 'ready' and network.get('context_id')
        and (health.get('state') or {}).get('ready') is True
    )


def instance_paths(device_id, state=None, service=None):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', device_id):
        raise ValueError('Device ID must match a configured lowercase stream ID')
    default = Path.home() / '.local/state/dillflix-prime-player'
    if device_id != 'living-room':
        default = default / 'instances' / device_id
    unit = service or ('dillflix-prime-player' + ('' if device_id == 'living-room' else '@' + device_id) + '.service')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.@-]*\.service', unit):
        raise ValueError('Player service must be a literal systemd .service unit name')
    return Path(state).resolve() if state is not None else default, unit


def require_target(snapshot, health, device_id, serial):
    if (snapshot.get('device', {}).get('id') != device_id
            or snapshot.get('configured_serial') != serial or health.get('serial') != serial):
        raise RuntimeError('Device registry, player socket and requested serial disagree; no fault authorized')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--device-id', default='living-room', help='Configured controller stream ID')
    parser.add_argument('--player-state', type=Path, help='Host state directory if it differs from the standard instance path')
    parser.add_argument('--player-service', help='Journal unit if it differs from the standard instance service')
    parser.add_argument('--interrupt-playback', action='store_true', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--stable-seconds', type=int, default=60)
    args = parser.parse_args()
    if not 60 <= args.timeout <= 1800 or not 30 <= args.stable_seconds <= 300:
        parser.error('timeout must be 60–1800 seconds; stable-seconds must be 30–300')
    try:
        state, player_service = instance_paths(args.device_id, args.player_state, args.player_service)
    except ValueError as exc:
        parser.error(str(exc))
    out = args.output.resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    player = Path.home() / 'dillflix-prime-player/.venv/bin/prime-player'
    cli = [str(player), '--socket', str(state / 'player.sock')]
    started = datetime.now(timezone.utc).isoformat()
    verdict = {'passed': False, 'started_at': started, 'fault_injected': False,
               'device_id': args.device_id, 'serial': args.serial, 'player_service': player_service,
               'scope': 'deployed supervisor and persisted adaptation; one Prime process loss, not a future runtime update'}
    journal = journal_output = None

    def save(name, value):
        (out / name).write_text(json.dumps(value, indent=2), encoding='utf-8')

    def snapshot():
        return json.loads(run(['docker', 'compose', 'exec', '-T', 'controller', 'python', '-c', SNAPSHOT, args.device_id]))

    try:
        baseline = snapshot()
        health = json.loads(run(cli + ['health']))
        save('baseline-controller.json', baseline)
        save('baseline-health.json', health)
        require_target(baseline, health, args.device_id, args.serial)
        content_id = (baseline['device'].get('observed') or {}).get('content_id')
        if health.get('serial') != args.serial or not ready(health) or not verified(baseline, content_id, 0, health['session_id']):
            raise RuntimeError('Preflight failed: require ready network and fresh verified live playback on the requested device; no fault injected')
        original_session = health['session_id']
        (out / 'controller-commit.txt').write_text(run(['git', 'rev-parse', 'HEAD']))
        (out / 'player-commit.txt').write_text(run(['git', '-C', str(player.parents[2]), 'rev-parse', 'HEAD']))
        (out / 'baseline-pid.txt').write_text(run(['adb', '-s', args.serial, 'shell', 'pidof', 'com.amazon.firebat']))
        # Open the privileged reader before fault injection, while sudo's cached
        # authorization is valid. Do not wait 15 minutes to request it again.
        run(['sudo', '-n', 'true'])
        journal_output = (out / 'player-journal.log').open('w', encoding='utf-8')
        journal = subprocess.Popen(
            ['sudo', '-n', 'timeout', str(args.timeout + 120), 'journalctl', '-u',
             player_service, '--since', started, '--no-pager', '-f', '-o', 'short-iso'],
            stdout=journal_output, stderr=subprocess.STDOUT,
        )
        fault_at = time.time()
        verdict.update(fault_dispatch='started', fault_at=fault_at, content_id=content_id)
        save('result.json', verdict)
        run(['adb', '-s', args.serial, 'shell', 'am', 'force-stop', 'com.amazon.firebat'])
        verdict.update(fault_injected=True, fault_dispatch='acknowledged')
        deadline, stable_since = time.monotonic() + args.timeout, None
        print('Prime stopped once. Observing deployed automatic recovery; no further device inputs.', flush=True)
        index = 0
        while time.monotonic() < deadline:
            index += 1
            try:
                health = json.loads(run(cli + ['health']))
                current = snapshot()
                save(f'{index:03d}-health.json', health)
                save(f'{index:03d}-controller.json', current)
                try:
                    require_target(current, health, args.device_id, args.serial)
                except RuntimeError as exc:
                    raise InterruptedError('Device binding changed during acceptance') from exc
                device = current['device']
                if device.get('manual_control') or device.get('input_handoff') or device.get('automation') != 'active':
                    raise InterruptedError('Manual takeover or pause detected; acceptance invalidated')
                passed = ready(health, original_session) and verified(current, content_id, fault_at, health['session_id'])
                stable_since = (stable_since or time.monotonic()) if passed else None
                print(f"network={(health.get('network') or {}).get('state')} new_session={health.get('session_id') != original_session} verified_live={passed}", flush=True)
                if stable_since and time.monotonic() - stable_since >= args.stable_seconds:
                    verdict.update(passed=True, recovered_session=health['session_id'], stable_seconds=args.stable_seconds)
                    break
            except InterruptedError:
                raise
            except Exception as exc:
                stable_since = None
                save(f'{index:03d}-error.json', {'type': type(exc).__name__, 'message': str(exc)})
                print(f'Observation failed: {type(exc).__name__}; continuing within deadline', flush=True)
            time.sleep(10)
        if not verdict['passed']:
            raise RuntimeError('Recovery deadline expired without stable fresh verified live playback')
    except (Exception, KeyboardInterrupt) as exc:
        verdict['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if journal is not None:
            if journal.poll() is not None:
                verdict['journal_error'] = f'Journal capture exited early: {journal.returncode}'
            else:
                journal.terminate()
                try:
                    journal.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    journal.kill()
                    journal.wait(timeout=5)
        if journal_output is not None:
            journal_output.close()
        verdict['finished_at'] = datetime.now(timezone.utc).isoformat()
        save('result.json', verdict)
        for name, command in (
            ('controller.log', ['docker', 'compose', 'logs', '--since', started, '--tail', '3000', 'controller']),
        ):
            try:
                (out / name).write_text(run(command, timeout=30), encoding='utf-8')
            except Exception as exc:
                (out / (name + '.error')).write_text(str(exc), encoding='utf-8')
        with tarfile.open(str(out) + '.tar.gz', 'w:gz') as archive:
            archive.add(out, arcname=out.name)
            if (state / 'adaptation').is_dir():
                archive.add(state / 'adaptation', arcname='adaptation')
        print(('PASS' if verdict['passed'] else 'FAIL') + ': ' + str(out) + '.tar.gz', flush=True)
        if verdict.get('error'):
            print(verdict['error'], flush=True)
    return 0 if verdict['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
