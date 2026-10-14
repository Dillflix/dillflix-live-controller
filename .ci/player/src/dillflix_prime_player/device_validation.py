"""Finite device lifecycle runner. Every operation has a deadline; no recovery retries."""
import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import threading
import time


class CaptureProcess:
    """Drain output continuously, retaining at most 2 MiB even for noisy logcat."""
    def __init__(self, command, path):
        self.path = Path(path)
        self.parts = deque()
        self.size = 0
        self.dropped = 0
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        while True:
            chunk = self.process.stdout.read1(4096)
            if not chunk:
                break
            self.parts.append(chunk)
            self.size += len(chunk)
            while self.size > 2 * 1024 * 1024:
                removed = self.parts.popleft()
                self.size -= len(removed)
                self.dropped += len(removed)

    def finish(self, grace=5):
        forced = False
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                forced = True
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            os.killpg(self.process.pid, signal.SIGKILL)
            self.thread.join(timeout=3)
        self.path.write_bytes(('dropped_prefix_bytes=%d\n' % self.dropped).encode()
                              + b''.join(self.parts))
        return forced


def command(args, directory, name, timeout):
    captured = CaptureProcess(args, directory / (name + '.log'))
    try:
        code = captured.process.wait(timeout=timeout)
        if code:
            raise RuntimeError('%s exited %s; see its log' % (name, code))
    finally:
        captured.finish()


def wait_ready(process, path, timeout):
    deadline = time.monotonic() + timeout
    next_print = 0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('instrumented service exited before readiness')
        if path.exists():
            return
        remaining = int(deadline - time.monotonic())
        if time.monotonic() >= next_print:
            print('DEVICE: startup pending; %ss remaining' % remaining, flush=True)
            next_print = time.monotonic() + 10
        time.sleep(0.2)
    raise TimeoutError('instrumented startup exceeded total budget')


def compare_searches(before, after):
    """Compare exact tile occurrences, never infer identity from the query alone."""
    def key(tile):
        return json.dumps({k: tile.get(k) for k in (
            'collection', 'position', 'title', 'date_label', 'availability',
            'labels', 'artwork', 'bounds')}, sort_keys=True)
    def unique(result):
        groups = {}
        for tile in result['tiles']:
            groups.setdefault(key(tile), []).append(tile)
        return {k: v[0] for k, v in groups.items() if len(v) == 1}
    old, new = unique(before), unique(after)
    common = old.keys() & new.keys()
    lost = [k for k in common if old[k].get('content_id') and not new[k].get('content_id')]
    changed = [k for k in common if old[k].get('content_id') and new[k].get('content_id')
               and old[k]['content_id'] != new[k]['content_id']]
    comparable = sum(bool(old[k].get('content_id')) for k in common)
    same_session = before['session_id'] == after['session_id']
    return {'same_session': same_session, 'unchanged_tiles': len(common),
            'comparable_resolved_tiles': comparable,
            'before_resolved': sum(bool(t.get('content_id')) for t in before['tiles']),
            'after_resolved': sum(bool(t.get('content_id')) for t in after['tiles']),
            'lost_ids': [dict(tile=old[k], repeat=new[k]) for k in sorted(lost)],
            'changed_ids': [dict(tile=old[k], repeat=new[k]) for k in sorted(changed)],
            'passed': same_session and comparable > 0 and not lost and not changed}


def repeat_searches(cli, cycle, count, interval, process):
    results = []
    comparisons = []
    for index in range(count):
        name = 'repeat-search-%02d' % (index + 1)
        print('DEVICE: same-session search %d/%d' % (index + 1, count), flush=True)
        command(cli, cycle, name, 150)
        result = json.loads((cycle / (name + '.log')).read_text().split('\n', 1)[1])
        (cycle / (name + '.json')).write_text(json.dumps(result, indent=2))
        # Worker captures evidence before another search can replace the watch.
        (cycle / 'snapshot-request').write_text(name)
        deadline = time.monotonic() + 25
        while not (cycle / (name + '-runtime.json')).exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('per-search runtime snapshot unavailable')
            time.sleep(0.1)
        if results:
            comparisons.append(compare_searches(results[0], result))
        results.append(result)
        (cycle / 'repeat-comparison.json').write_text(json.dumps(comparisons, indent=2))
        if comparisons and not comparisons[-1]['passed']:
            raise RuntimeError('repeat search lost/changed IDs or has insufficient comparable evidence; see repeat-comparison.json')
        if index + 1 < count:
            time.sleep(interval)


def worker(args):
    from .backend import FridaBackend
    from .controller import Controller
    from .provision import PrivateServer
    from .service import LocalService
    from .startup_diagnostics import capture_failure
    directory = Path(args._worker)
    private = PrivateServer(args.serial, args.server_binary, args.root_prefix)
    backend = None
    service = LocalService(None, args.socket)
    reason = RuntimeError('completed lifecycle snapshot; consult result.json for verdict')
    code = 0
    snapshot_stop = threading.Event()
    snapshot_thread = None
    def capture_requests():
        previous = None
        while not snapshot_stop.wait(0.1):
            request = directory / 'snapshot-request'
            if not request.exists():
                continue
            name = request.read_text()
            if not name or name == previous:
                continue
            previous = name
            extra = {}
            try:
                cursor = backend.cursor
                backend.command('search-results', timeout=15)
                # Command completion may arrive before the emitted observation
                # is drained. Wait for it before committing the archive snapshot.
                backend.wait(lambda: any(r['kind'] == 'search_results_snapshot'
                                         for r in backend.records(cursor)), 5)
            except Exception as exc:
                extra['search_snapshot_error'] = repr(exc)
            value = backend.diagnostic_snapshot()
            value.update(extra)
            temp = directory / (name + '-runtime.tmp')
            temp.write_text(json.dumps(value, indent=2))
            temp.rename(directory / (name + '-runtime.json'))
    def stop(*unused):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        service.start()  # Reserve API before device mutation.
        backend = FridaBackend(args.serial, private.start(), args.state_dir)
        backend.start(restart_prime=True, exclusive_server=True, connection_timeout=30)
        service.server.controller = Controller(backend)
        snapshot_thread = threading.Thread(target=capture_requests, daemon=True)
        snapshot_thread.start()
        (directory / 'ready.json').write_text(json.dumps(backend.health()))
        service.serve_forever(lambda: backend.failure)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        reason, code = exc, 1
    finally:
        snapshot_stop.set()
        if snapshot_thread:
            snapshot_thread.join(timeout=20)
        # Capture before cleanup, including successful server logs for comparison.
        try:
            capture_failure(directory, args.serial, reason, backend, private)
        except Exception as exc:
            (directory / 'capture-error.txt').write_text(repr(exc))
            code = 1
        for resource in (service, backend, private):
            if resource:
                try:
                    resource.close()
                except Exception as exc:
                    (directory / ('cleanup-' + type(resource).__name__ + '.txt')).write_text(repr(exc))
                    code = 1
    return code


def run(args):
    directory = Path(args.output).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    adb = ['adb', '-s', args.serial]
    cli = [sys.executable, '-m', 'dillflix_prime_player', '--socket', args.socket]
    report = {'schema': 'prime-device-validation/1', 'started_at': datetime.now(timezone.utc).isoformat(),
              'mode': args.mode, 'cycles': [], 'passed': False,
              'scope': 'startup/search/handoff plus explicitly requested playback; not a stability guarantee'}
    logcat = None
    child = None
    try:
        command(adb + ['get-state'], directory, 'adb-state', 10)
        command(adb + ['shell', 'df', '-k', '/data'], directory, 'storage', 10)
        command(adb + ['shell', 'getprop'], directory, 'device-properties', 10)
        logcat = CaptureProcess(adb + ['logcat', '-b', 'main', '-b', 'system', '-b', 'crash', '-v', 'threadtime', '-T', '1'], directory / 'device-live.log')
        for index in range(args.cycles):
            cycle = directory / ('cycle-%02d' % (index + 1))
            cycle.mkdir()
            item = {'index': index + 1, 'passed': False}
            report['cycles'].append(item)
            print('DEVICE: cycle %d/%d (%s)' % (index + 1, args.cycles, args.mode), flush=True)
            if args.mode == 'baseline':
                command(adb + ['shell', 'am', 'force-stop', 'com.amazon.firebat'], cycle, 'stop-before', 10)
                command(adb + ['shell', 'am', 'start', '-W', '-n', 'com.amazon.firebat/com.amazon.pyrocore.IgnitionActivity'], cycle, 'launch', 100)
                if 'Status: ok' not in (cycle / 'launch.log').read_text(errors='replace'):
                    raise RuntimeError('baseline activity launch did not report success')
                def pid():
                    result = subprocess.run(adb + ['shell', 'pidof', 'com.amazon.firebat'], capture_output=True, text=True, timeout=8, check=True)
                    return result.stdout.strip()
                original = pid()
                if not original:
                    raise RuntimeError('baseline Prime PID absent')
                until = time.monotonic() + args.monitor_seconds
                while time.monotonic() < until:
                    if pid() != original:
                        raise RuntimeError('baseline Prime process disappeared or restarted')
                    time.sleep(min(5, max(0, until - time.monotonic())))
                item['evidence'] = 'activity startup acknowledged and PID retained; UI/playback not verified'
            else:
                child = CaptureProcess([sys.executable, '-m', 'dillflix_prime_player.device_validation',
                    '--_worker', str(cycle), '--serial', args.serial, '--server-binary', args.server_binary,
                    '--socket', args.socket, '--state-dir', args.state_dir,
                    *(['--root-prefix', args.root_prefix] if args.root_prefix is not None else [])], cycle / 'service.log')
                wait_ready(child.process, cycle / 'ready.json', args.startup_timeout)
                if args.search_repeats:
                    repeat_searches(cli + ['search', args.query], cycle, args.search_repeats,
                                    args.search_repeat_interval, child.process)
                command(cli + ['validate', '--query', args.query, '--output', str(cycle / 'search')], cycle, 'search', 240)
                command(cli + ['validate-handoff', '--query', args.query, '--send-wake', '--output', str(cycle / 'handoff.tar.gz')], cycle, 'handoff', 240)
                if args.content_id:
                    command(cli + ['validate', '--content-id', args.content_id, '--mode', args.content_mode,
                                   '--play', '--monitor-seconds', str(args.monitor_seconds),
                                   '--output', str(cycle / 'playback')], cycle, 'playback', args.monitor_seconds + 300)
                    if args.check_stop:
                        with tarfile.open(cycle / 'playback.tar.gz') as archive:
                            member = next(m for m in archive.getmembers() if m.name.endswith('/attempt.json'))
                            attempt = json.load(archive.extractfile(member))
                        command(cli + ['stop', attempt['attempt_id'], '--session-id', attempt['session_id']], cycle, 'stop', 120)
                        stop_result = json.loads((cycle / 'stop.log').read_text().split('\n', 1)[1])
                        if stop_result.get('stopped') is not True:
                            raise RuntimeError('native stopping not confirmed; see stop.log (no retry)')
                else:
                    until = time.monotonic() + args.monitor_seconds
                    while time.monotonic() < until:
                        command(cli + ['health'], cycle, 'health', 15)
                        if child.process.poll() is not None:
                            raise RuntimeError('service exited during observation')
                        time.sleep(min(5, max(0, until - time.monotonic())))
                if child.process.poll() is not None:
                    raise RuntimeError('service exited before cycle completed')
                forced = child.finish(grace=120)
                exit_code = child.process.returncode
                child = None
                if forced or exit_code != 0:
                    raise RuntimeError('cleanup failed or exceeded deadline; no additional cycle allowed')
            item['passed'] = True
        report['passed'] = True
    except (Exception, KeyboardInterrupt) as exc:
        report['error'] = {'type': type(exc).__name__, 'message': str(exc)}
    finally:
        if child:
            report['cleanup_forced'] = child.finish(grace=120)
        if args.mode == 'baseline':
            try:
                command(adb + ['shell', 'am', 'force-stop', 'com.amazon.firebat'], directory, 'baseline-cleanup', 10)
            except Exception as exc:
                report['cleanup_error'] = repr(exc)
                report['passed'] = False
        if logcat:
            report['logcat_exited_early'] = logcat.process.poll() is not None
            if report['logcat_exited_early']:
                report['passed'] = False
            logcat.finish()
        for name, tail in [('activities', ['shell', 'dumpsys', 'activity', 'activities']),
                           ('crash', ['logcat', '-b', 'crash', '-d', '-v', 'threadtime', '-t', '500'])]:
            try:
                command(adb + tail, directory, name, 10)
            except Exception as exc:
                report[name + '_capture_error'] = repr(exc)
        report['cleanup_errors'] = {str(p.relative_to(directory)): p.read_text(errors='replace')
                                    for p in directory.rglob('cleanup-*.txt')}
        if report['cleanup_errors']:
            report['passed'] = False
        # Keep the Frida error separate from capability outcomes. It is not a crash verdict.
        report['frida_invalid_instruction_observed'] = any('Error: invalid instruction' in p.read_text(errors='replace') for p in directory.rglob('frida-server.log'))
        (directory / 'result.json').write_text(json.dumps(report, indent=2))
        archive = directory.with_name(directory.name + '.tar.gz')
        with tarfile.open(archive, 'x:gz') as output:
            output.add(directory, arcname=directory.name)
        print(('PASS' if report['passed'] else 'FAIL / INCOMPLETE') + ': ' + str(archive), flush=True)
    return 0 if report['passed'] else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--serial', required=True)
    p.add_argument('--socket', default=str(Path.home() / '.local/state/dillflix-prime-player/player.sock'))
    p.add_argument('--state-dir', default=str(Path.home() / '.local/state/dillflix-prime-player'))
    p.add_argument('--server-binary')
    p.add_argument('--root-prefix')
    p.add_argument('--mode', choices=['baseline', 'instrumented'], default='instrumented')
    p.add_argument('--query', default='browns steelers')
    p.add_argument('--search-repeats', type=int, default=0, help='Run 2..5 identical searches in one session, archiving runtime evidence after each')
    p.add_argument('--search-repeat-interval', type=int, default=0, help='Seconds between completed searches, 0..60')
    p.add_argument('--content-id')
    p.add_argument('--check-stop', action='store_true', help='After verified playback, require matching native stop evidence')
    p.add_argument('--content-mode', choices=['live', 'replay'], default='live')
    p.add_argument('--cycles', type=int, default=1)
    p.add_argument('--monitor-seconds', type=int, default=60)
    p.add_argument('--startup-timeout', type=int, default=180)
    p.add_argument('--output')
    p.add_argument('--exclusive-device', action='store_true', help='Confirm other controllers, service supervisors and instrumentation are stopped; this test restarts Prime')
    p.add_argument('--_worker', help=argparse.SUPPRESS)
    args = p.parse_args()
    if args._worker:
        return worker(args)
    if not args.exclusive_device or not args.output:
        p.error('--exclusive-device and --output are required')
    if args.search_repeats not in (0, 2, 3, 4, 5) or not 0 <= args.search_repeat_interval <= 60:
        p.error('search-repeats must be 0 or 2..5; search-repeat-interval 0..60')
    if args.search_repeats and args.mode != 'instrumented':
        p.error('search-repeats requires instrumented mode')
    if args.check_stop and (not args.content_id or args.mode != 'instrumented'):
        p.error('--check-stop requires instrumented mode and --content-id')
    if args.mode == 'instrumented' and not args.server_binary:
        p.error('--server-binary is required for instrumented testing')
    if not 1 <= args.cycles <= 5 or not 0 <= args.monitor_seconds <= 3600 or not 30 <= args.startup_timeout <= 300:
        p.error('cycles 1..5; monitor-seconds 0..3600; startup-timeout 30..300')
    if Path(args.socket).exists():
        p.error('service socket exists; stop the service before testing (do not delete a live socket)')
    if Path(args.output + '.tar.gz').exists():
        p.error('output archive already exists')
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
