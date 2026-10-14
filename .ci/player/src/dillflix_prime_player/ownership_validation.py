"""Finite on-device handoff validation. Never selects content for playback."""

import asyncio
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tarfile
import time
from .client import PrimePlayer
from .errors import BusyError, CancelledError, StaleResultError


def run(args):
    events = []

    def record(phase, value):
        events.append(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "phase": phase,
                "value": value,
            }
        )
        print("HANDOFF: " + phase, flush=True)

    async def validate():
        client = PrimePlayer(args.socket)
        task = None
        receipt = None
        try:
            health = await client.health()
            if (
                health.get("api_version", 0) < 4
                or health["ownership"]["mode"] != "automatic"
                or health.get("busy")
            ):
                raise RuntimeError(
                    "requires an idle automatic service with API version 4"
                )
            record("before", health)
            task = asyncio.create_task(client.search(args.query))
            deadline = time.monotonic() + 10
            while not (await client.health()).get("busy"):
                if task.done():
                    await task
                    raise RuntimeError(
                        "search completed before cancellation could be tested"
                    )
                if time.monotonic() >= deadline:
                    raise TimeoutError("search did not enter service operation")
                await asyncio.sleep(0.1)
            start = time.monotonic()
            receipt = await client.suspend()
            record(
                "manual_handoff_acknowledged",
                dict(receipt, elapsed_seconds=time.monotonic() - start),
            )
            if not receipt.get("acknowledged") or receipt.get("mode") != "manual":
                raise RuntimeError("manual handoff not acknowledged")
            try:
                await task
                raise RuntimeError(
                    "search unexpectedly completed instead of cancellation"
                )
            except CancelledError:
                record("search_cancelled", True)
            try:
                await client.search(args.query)
                raise RuntimeError("automatic search accepted during manual control")
            except BusyError:
                record("automatic_mutation_refused", True)
            if args.send_wake:
                record(
                    "manual_wake_delivered",
                    await client.manual_input(
                        receipt=receipt, seq=receipt["last_seq"] + 1, key="wake"
                    ),
                )
            record(
                "automatic_handoff_acknowledged", await client.resume(receipt=receipt)
            )
            old = receipt
            receipt = None
            try:
                await client.manual_input(
                    receipt=old, seq=old["last_seq"] + 2, key="wake"
                )
                raise RuntimeError("stale manual receipt accepted")
            except StaleResultError:
                record("stale_manual_input_refused", True)
            record("after", await client.health())
            return True
        except Exception as error:
            record("failed", {"type": type(error).__name__, "message": str(error)})
            return False
        finally:
            if receipt:
                try:
                    record("cleanup_resume", await client.resume(receipt=receipt))
                except Exception as error:
                    record("cleanup_unconfirmed", str(error))
            if task and not task.done():
                try:
                    await client.cancel()
                    await asyncio.wait_for(asyncio.shield(task), 110)
                except Exception as error:
                    record("search_cleanup", str(error))

    passed = asyncio.run(validate())
    output = Path(
        args.output
        or (
            "handoff-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + ".tar.gz"
        )
    ).resolve()
    data = json.dumps(
        {"schema": "prime-handoff-validation/1", "passed": passed, "events": events},
        indent=2,
    ).encode()
    with output.open("xb") as file, tarfile.open(fileobj=file, mode="w:gz") as archive:
        info = tarfile.TarInfo("handoff.json")
        info.mode = 0o600
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    print(("PASS" if passed else "FAIL") + ": " + str(output), flush=True)
    return 0 if passed else 1
