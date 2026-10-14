from .device_input import manual_input as manual_device_input
import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
from .client import PrimePlayer


def main():
    root = Path(os.environ.get(
        "PRIME_PLAYER_STATE_DIR", str(Path.home() / ".local/state/dillflix-prime-player")
    ))
    parser = argparse.ArgumentParser(
        description="Persistent Prime Video runtime controller"
    )
    parser.add_argument("--socket", default=os.environ.get("PRIME_PLAYER_SOCKET", str(root / "player.sock")))
    sub = parser.add_subparsers(dest="op", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--serial", required=True)
    servers = serve.add_mutually_exclusive_group(required=True)
    servers.add_argument("--endpoint")
    servers.add_argument("--server-binary")
    serve.add_argument("--root-prefix")
    serve.add_argument(
        "--connection-timeout",
        type=int,
        default=120,
        help="Read-only Frida handshake budget in seconds (5..120); separate from runtime startup",
    )
    serve.add_argument("--state-dir", default=str(root))
    serve.add_argument("--restart-prime", action="store_true", required=True)
    serve.add_argument("--supervised", action="store_true", help="Keep the API available while adapting to runtime updates and recovering failed sessions")
    serve.add_argument("--network-config", default=os.environ.get("PRIME_PLAYER_NETWORK_CONFIG"),
                       help="Opt-in managed VPN inventory; requires --supervised")
    serve.add_argument("--shared-socket", action="store_true", help="Create mode-0660 socket for the process primary group")
    serve.add_argument(
        "--exclusive-server",
        action="store_true",
        help="Required when reusing --endpoint; never share a spawn-gating server",
    )
    sub.add_parser("health")
    sub.add_parser("vpn-locations")
    vpn_status = sub.add_parser("vpn-status", help="Read cached network/operation evidence; no device mutation")
    vpn_status.add_argument("--operation-id")
    vpn_status.add_argument("--output", help="Write a read-only network evidence archive")
    for name in ("vpn-connect", "vpn-disconnect"):
        vpn = sub.add_parser(name, help="Managed network transition; interrupts playback and restarts Prime")
        if name == "vpn-connect":
            vpn.add_argument("target_id")
            vpn.add_argument("--qualify", action="store_true", help="Explicit setup trial; return to direct to commit qualification")
        vpn.add_argument("--operation-id", required=True, help="Retain this ID to inspect uncertain delivery")
        vpn.add_argument("--timeout", type=int, default=600)
        vpn.add_argument("--interrupt-playback", action="store_true", required=True)
    vpn_cancel = sub.add_parser("vpn-cancel", help="Cancel one operation; does not restore/disconnect the VPN")
    vpn_cancel.add_argument("operation_id")
    compatibility = sub.add_parser(
        "compatibility", help="Read-only compatibility evidence; never restarts Prime"
    )
    compatibility.add_argument("--output", help="Write an evidence tar.gz archive")
    analyze = sub.add_parser(
        "analyze-adapter",
        help="Offline static candidate inventory; never enables capabilities",
    )
    analyze.add_argument("resources", nargs="+")
    sub.add_parser("suspend")
    resume = sub.add_parser("resume")
    resume.add_argument(
        "--receipt", required=True, help="JSON receipt saved by suspend"
    )
    cancel = sub.add_parser("cancel")
    cancel.add_argument("--attempt-id")
    stop = sub.add_parser("stop")
    stop.add_argument("attempt_id")
    stop.add_argument("--session-id", required=True)
    handoff = sub.add_parser(
        "validate-handoff",
        help="Cancel an in-progress search and verify manual handoff; no playback selection",
    )
    handoff.add_argument("--query", required=True)
    handoff.add_argument("--send-wake", action="store_true")
    handoff.add_argument("--output")
    page_validation = sub.add_parser(
        "validate-pages", help="Bounded API device acceptance; no playback selection"
    )
    page_validation.add_argument("--page", dest="pages", action="append", required=True)
    page_validation.add_argument("--limit", type=int, default=20)
    page_validation.add_argument("--max-rows", type=int, default=2)
    page_validation.add_argument("--output", required=True)
    sub.add_parser("pages")
    page = sub.add_parser("discover")
    page.add_argument("pages", nargs="+")
    page.add_argument("--limit-per-page", type=int, default=20)
    page.add_argument("--max-rows", type=int, default=2)
    page.add_argument("--max-steps", type=int, default=16)
    page.add_argument("--timeout", type=int, default=400)
    page.add_argument("--no-resolve", action="store_true", help="Skip per-tile access resolution")
    search = sub.add_parser("search")
    search.add_argument("query")
    resolve = sub.add_parser("resolve", help="Resolve action and entitlement without playback")
    resolve.add_argument("content_id")
    resolve.add_argument("--timeout", type=int, default=45)
    play = sub.add_parser("play")
    play.add_argument("content_id")
    play.add_argument("--mode", choices=["live", "replay"], default="live")
    play.add_argument("--wait", action="store_true")
    play.add_argument("--attempt-id")
    attempt = sub.add_parser("attempt")
    attempt.add_argument("attempt_id")
    status = sub.add_parser(
        "playback-status", help="Fresh current playback inspection for a prior attempt"
    )
    status.add_argument("attempt_id")
    validate = sub.add_parser(
        "validate", help="Finite API acceptance run with an artifact archive"
    )
    source = validate.add_mutually_exclusive_group(required=True)
    source.add_argument("--query")
    source.add_argument("--content-id")
    source.add_argument("--attempt-id")
    validate.add_argument("--title")
    validate.add_argument("--date-label")
    validate.add_argument("--mode", choices=["live", "replay"], default="live")
    validate.add_argument(
        "--play",
        action="store_true",
        help="Explicitly launch the unique selected result or supplied GTI",
    )
    validate.add_argument("--monitor-seconds", type=int, default=90)
    validate.add_argument("--interval", type=int, default=10)
    validate.add_argument(
        "--manual-checks",
        action="store_true",
        help="Bounded remote pause/play/back checks after automatic monitoring",
    )
    validate.add_argument("--manual-timeout", type=int, default=90)
    validate.add_argument("--output")
    validate.add_argument("--service-log")
    args = parser.parse_args()
    if args.op == "validate-pages":
        from .page_validation import run

        raise SystemExit(run(args))
    if args.op == "validate-handoff":
        from .ownership_validation import run

        raise SystemExit(run(args))
    if args.op == "analyze-adapter":
        from .compatibility import analyze_resources

        print(json.dumps(analyze_resources(args.resources), indent=2))
        return
    if args.op == "validate":
        if args.play and args.attempt_id:
            parser.error(
                "--attempt-id monitors an existing attempt; do not combine with --play"
            )
        if args.content_id and not args.play:
            parser.error("--content-id requires --play")
        if args.play and args.query and not args.title:
            parser.error("search playback requires an exact --title")
        if args.manual_checks and not (args.play or args.attempt_id):
            parser.error("--manual-checks requires playback or an existing attempt")
        if (
            not 0 <= args.monitor_seconds <= 3600
            or not 1 <= args.interval <= 60
            or not 10 <= args.manual_timeout <= 300
        ):
            parser.error(
                "monitor seconds: 0..3600; interval: 1..60; manual timeout: 10..300"
            )
        from .validation import run_validation

        raise SystemExit(run_validation(args))
    if args.op == "serve":
        from .backend import FridaBackend
        from .controller import Controller
        from .service import LocalService

        if not 5 <= args.connection_timeout <= 120:
            parser.error("--connection-timeout must be 5..120")

        if args.endpoint and not args.exclusive_server:
            parser.error("--endpoint requires --exclusive-server")
        if args.network_config and not args.supervised:
            parser.error("--network-config requires --supervised")
        from .network import NetworkConfig, AdbVpnBridge
        network_config = NetworkConfig.read(args.network_config)
        from .provision import PrivateServer

        private = (
            PrivateServer(args.serial, args.server_binary, args.root_prefix)
            if args.server_binary
            else None
        )
        service = LocalService(None, args.socket, shared=args.shared_socket, recover_stale=args.supervised)
        backend = None
        supervisor = None
        device_service = None
        # Reserve local API endpoint before changing the device lifecycle.
        service.start()
        from .diagnostic_log import Recorder
        from .runtime_capture import service_output

        recorder = Recorder(Path(args.socket).parent / "diagnostics")
        service.server.recorder = recorder
        output_capture = service_output(recorder)
        output_capture.__enter__()

        def stop_signal(*args):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop_signal)
        try:
            print(
                "Starting dedicated controller; Prime will restart. Waiting for runtime readiness...",
                flush=True,
            )
            if args.supervised:
                from .backend import RUNTIME
                from .recovery import RuntimeSupervisor

                def create_backend(profile):
                    if private:
                        private.close()
                    endpoint = private.start() if private else args.endpoint
                    return FridaBackend(args.serial, endpoint, args.state_dir, profile=profile)

                supervisor = RuntimeSupervisor(
                    json.loads((RUNTIME / "profile.json").read_text()), args.state_dir,
                    create_backend, start_options={"connection_timeout": args.connection_timeout},
                    serial=args.serial,
                )
                from .network_device import NetworkDevice
                device_service = NetworkDevice(
                    supervisor, args.state_dir, config=network_config,
                    bridge=AdbVpnBridge(args.serial, network_config.token_file) if network_config.enabled else None,
                    manual_input=lambda **params: manual_device_input(["adb", "-s", args.serial], **params),
                )
                service.server.controller = device_service
                supervisor.start()
                device_service.start()
                print("Player API ready on " + args.socket + "; runtime recovery is automatic", flush=True)
                service.serve_forever()
            else:
                endpoint = private.start() if private else args.endpoint
                backend = FridaBackend(args.serial, endpoint, args.state_dir)
                backend.start(
                    restart_prime=args.restart_prime,
                    exclusive_server=True,
                    connection_timeout=args.connection_timeout,
                )
                service.server.controller = Controller(backend)
                print("Controller ready on " + args.socket, flush=True)
                service.serve_forever()
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            import traceback
            recorder.record("runtime", "service_failure", traceback=traceback.format_exc())
            from .startup_diagnostics import capture_failure

            try:
                capture_failure(args.state_dir, args.serial, exc, backend, private)
            except Exception as diagnostic_error:
                print(
                    f"Startup diagnostics could not be archived: {diagnostic_error}",
                    flush=True,
                )
            raise
        finally:
            try:
                service.close()
                if device_service:
                    device_service.close()
                elif supervisor:
                    supervisor.close()
                if backend:
                    backend.close()
                if private:
                    private.close()
            finally:
                output_capture.__exit__(None, None, None)
                recorder.close()
        return

    async def run():
        client = PrimePlayer(args.socket)
        if args.op == "vpn-locations":
            return await client.vpn_locations()
        if args.op == "vpn-status":
            value = await client.vpn_status(operation_id=args.operation_id)
            if args.output:
                from .network import archive_network
                evidence = {"status": value, "locations": await client.vpn_locations()}
                return {"archive": str(archive_network(evidence, args.output))}
            return value
        if args.op == "vpn-cancel":
            return await client.vpn_cancel(args.operation_id)
        if args.op in {"vpn-connect", "vpn-disconnect"}:
            import time
            existing = await client.vpn_status(operation_id=args.operation_id)
            if existing.get("state") != "not_found":
                return existing
            return await client.vpn_connect(
                args.target_id if args.op == "vpn-connect" else "direct",
                operation_id=args.operation_id, deadline_at=time.time() + args.timeout,
                interrupt_playback=args.interrupt_playback, qualify=getattr(args, "qualify", False),
            )
        if args.op == "resume":
            return await client.resume(
                receipt=json.loads(Path(args.receipt).read_text())
            )
        if args.op == "cancel":
            return await client.cancel(attempt_id=args.attempt_id)
        if args.op == "stop":
            return await client.stop(
                attempt_id=args.attempt_id, session_id=args.session_id
            )
        if args.op == "compatibility":
            value = await client.compatibility()
            if args.output:
                from .compatibility import archive_evidence

                return {
                    "archive": str(archive_evidence(value, args.output)),
                    "generation": value.get("generation"),
                }
            return value
        if args.op == "pages":
            return (await client.pages()).to_dict()
        if args.op == "discover":
            return (
                await client.discover(
                    args.pages,
                    args.limit_per_page,
                    max_rows=args.max_rows,
                    max_steps=args.max_steps,
                    timeout=args.timeout,
                    resolve_access=not args.no_resolve,
                )
            ).to_dict()
        if args.op == "search":
            return (await client.search(args.query)).to_dict()
        if args.op == "play":
            attempt = await client.play(
                args.content_id, mode=args.mode, attempt_id=args.attempt_id
            )
            return asdict(
                await attempt.wait_for_playing() if args.wait else attempt.initial
            )
        if args.op == "resolve":
            return (await client.resolve(args.content_id, timeout=args.timeout)).to_dict()
        if args.op == "attempt":
            return asdict((await client.attempt(args.attempt_id)).initial)
        if args.op == "playback-status":
            return asdict(await client.playback_status(attempt_id=args.attempt_id))
        return await getattr(client, args.op)()

    print(json.dumps(asyncio.run(run()), indent=2))
