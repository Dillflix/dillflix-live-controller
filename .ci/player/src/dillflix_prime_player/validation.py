"""Finite, opt-in API acceptance harness. Never owns the device service."""

import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import platform
import shutil
import tarfile
import time
import uuid

from .client import PrimePlayer


class ValidationFailure(RuntimeError):
    pass


class Recorder:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.journal = (self.directory / "api.jsonl").open("x")
        self.checks = []

    def write(self, name, value):
        (self.directory / name).write_text(
            json.dumps(value, indent=2, allow_nan=False) + "\n"
        )

    async def call(self, name, operation):
        print(f"API {name}: waiting for bounded response...", flush=True)
        started = time.monotonic()
        row = {"operation": name, "started_at": datetime.now(timezone.utc).isoformat()}
        task = None
        try:
            task = asyncio.ensure_future(operation())
            while not task.done():
                await asyncio.wait({task}, timeout=10)
                if not task.done():
                    print(
                        f"API {name}: still waiting ({int(time.monotonic()-started)}s elapsed).",
                        flush=True,
                    )
            value = task.result()
            row["result"] = (
                asdict(value)
                if is_dataclass(value)
                else value.initial.__dict__ if hasattr(value, "initial") else value
            )
            return value
        except Exception as exc:
            row["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
                "attempt_id": getattr(exc, "attempt_id", None),
            }
            raise
        finally:
            if task is not None and not task.done():
                task.cancel()
            row["duration_seconds"] = round(time.monotonic() - started, 3)
            self.journal.write(json.dumps(row, allow_nan=False) + "\n")
            self.journal.flush()

    def check(self, name, passed, detail=None):
        self.checks.append({"name": name, "passed": bool(passed), "detail": detail})
        print(f'{"PASS" if passed else "FAIL"}: {name}', flush=True)
        if not passed:
            raise ValidationFailure(name)


async def exercise(args, log, client_factory=PrimePlayer):
    client = client_factory(args.socket)
    health = await log.call("health", client.health)
    log.write("health-start.json", health)
    log.check(
        "service supports current playback status",
        "playback_status" in health.get("capabilities", []),
        "Restart the service from 0.1.0a2 or newer if this fails.",
    )
    log.check(
        "runtime session healthy",
        not health.get("failure")
        and not health.get("closed")
        and health.get("state", {}).get("ready"),
    )
    log.check("service is not manually suspended", not health.get("suspended"))
    session = health["session_id"]
    candidate = None
    if args.query:
        receipt = await log.call("suspend", client.suspend)
        try:
            log.check("suspend API", receipt.get("acknowledged") is True
                      and receipt.get("mode") == "manual"
                      and receipt.get("session_id") == session)
        finally:
            # Release only the receipt obtained by this test, including failed assertions.
            resumed = await log.call("resume", lambda: client.resume(receipt=receipt))
        log.check("resume API", resumed.get("acknowledged") is True
                  and resumed.get("mode") == "automatic"
                  and resumed.get("session_id") == session)
        results = await log.call("search", lambda: client.search(args.query))
        log.write("search.json", results.to_dict())
        log.check(
            "search belongs to this service session", results.session_id == session
        )
        print(
            f"Search returned {len(results.items)} catalogue items; see search.json.",
            flush=True,
        )
        if args.play:
            matches = results.matching(title=args.title, entitlement_status="ENTITLED")
            if args.date_label:
                matches = tuple(t for t in matches if t.metadata.get("liveEventDateBadge") == args.date_label)
            log.write("matches.json", [asdict(t) for t in matches])
            log.check("exactly one requested tile", len(matches) == 1)
            candidate = matches[0]
            log.check(
                "tile has a correlated playable GTI",
                isinstance(candidate.content_id, str)
                and candidate.content_id.startswith("amzn1.dv.gti."),
            )
    if args.play:
        attempt_id = uuid.uuid4().hex
        log.write(
            "attempt.json",
            {
                "attempt_id": attempt_id,
                "session_id": session,
                "content_id": candidate.content_id if candidate else args.content_id,
                "mode": args.mode,
            },
        )
        target = candidate or args.content_id
        attempt = await log.call(
            "play", lambda: client.play(target, mode=args.mode, attempt_id=attempt_id)
        )
        outcome = await log.call(
            "wait_for_playing", lambda: attempt.wait_for_playing(timeout=150)
        )
        log.write("play-outcome.json", asdict(outcome))
        log.check(
            "initial playback verified", outcome.state == "playing", outcome.reason
        )
        duplicate = await log.call(
            "play same attempt ID (idempotency)",
            lambda: client.play(target, mode=args.mode, attempt_id=attempt_id),
        )
        log.check(
            "idempotent request preserves outcome",
            duplicate.id == attempt_id and duplicate.initial == outcome,
        )
    elif args.attempt_id:
        attempt_id = args.attempt_id
        log.write("attempt.json", {"attempt_id": attempt_id, "session_id": session})
    else:
        print("Search-only validation complete. No playback requested.", flush=True)
        return

    await client.close()
    client = client_factory(args.socket)
    after = await log.call("health after client reconnect", client.health)
    log.check(
        "client reconnect preserves service session", after["session_id"] == session
    )
    saved = await log.call(
        "attempt after reconnect", lambda: client.attempt(attempt_id)
    )
    log.check("attempt remains addressable", saved.id == attempt_id)

    async def status():
        value = await log.call(
            "playback_status", lambda: client.playback_status(attempt_id=attempt_id)
        )
        print(
            f"CURRENT: {value.state}; matches={value.matches_attempt}; ID={value.current_content_id}; position={value.position}; error={value.error}",
            flush=True,
        )
        return value

    end = time.monotonic() + args.monitor_seconds
    while True:
        current = await status()
        log.check(
            "fresh status still matches and is playing",
            current.state == "playing" and current.matches_attempt is True,
        )
        left = end - time.monotonic()
        if left <= 0:
            break
        print(f"Monitoring: {int(left)} seconds remaining.", flush=True)
        await asyncio.sleep(min(args.interval, left))

    if args.manual_checks:
        await log.call("suspend for manual checks", client.suspend)
        try:
            for prompt, expected in [
                ("Press PAUSE on the remote now.", "paused"),
                ("Press PLAY on the remote now.", "playing"),
                ("Press BACK to leave playback now.", "not_current"),
            ]:
                print(
                    f"MANUAL: {prompt} Waiting up to {args.manual_timeout}s; no Enter needed.",
                    flush=True,
                )
                deadline = time.monotonic() + args.manual_timeout
                while True:
                    current = await status()
                    if current.state == expected:
                        break
                    if time.monotonic() >= deadline:
                        raise ValidationFailure(
                            f"manual transition to {expected} not observed"
                        )
                    await asyncio.sleep(min(3, max(0, deadline - time.monotonic())))
                log.check(f"manual transition: {expected}", True)
        finally:
            await log.call("resume after manual checks", client.resume)
    log.write("health-end.json", await log.call("final health", client.health))
    await client.close()


def run_validation(args):
    suffix = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:6]
    )
    directory = Path(args.output or ("captures/api-validation-" + suffix)).resolve()
    log = Recorder(directory)
    passed = False
    error = None
    code = 1
    config = {k: v for k, v in vars(args).items() if k not in ("root_prefix",)}
    log.write(
        "run.json",
        {
            "configuration": config,
            "package_version": version("dillflix-prime-player"),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    print(f"ARTIFACT DIRECTORY: {directory}", flush=True)
    print(
        "This harness leaves the service running. Playback is launched only with --play.",
        flush=True,
    )
    try:
        asyncio.run(exercise(args, log))
        passed = True
        code = 0
    except KeyboardInterrupt:
        error = {
            "type": "KeyboardInterrupt",
            "message": "interrupted; partial evidence preserved",
        }
        code = 130
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        print(f"VALIDATION FAILED: {exc}", flush=True)
    finally:
        log.journal.close()
        log.write(
            "summary.json",
            {
                "passed": passed,
                "checks": log.checks,
                "error": error,
                "device_service_stopped": False,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        if args.service_log:
            try:
                shutil.copyfile(args.service_log, directory / "service.log")
            except OSError as exc:
                log.write("service-log-error.json", {"error": str(exc)})
        archive = directory.with_name(directory.name + ".tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(directory, arcname=directory.name)
        print(f"CAPTURE ARCHIVE: {archive}", flush=True)
        print(
            "VALIDATION: " + ("PASS" if passed else "FAILED / INCOMPLETE"), flush=True
        )
    return code

