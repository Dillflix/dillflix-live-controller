"""Finite acceptance run of the public pages/discover APIs, with no playback selection."""

import asyncio
import io
import json
from pathlib import Path
import tarfile
from datetime import datetime, timezone
from .client import PrimePlayer


async def validate(client, pages, *, limit=20, max_rows=2):
    report = dict(
        schema="prime-page-validation/2",
        started_at=datetime.now(timezone.utc).isoformat(),
        results=[],
        errors=[],
    )
    try:
        report["health_before"] = await client.health()
        report["pages"] = (await client.pages()).to_dict()
        result = await client.discover(
            pages, limit_per_page=limit, max_rows=max_rows, timeout=100 * len(pages)
        )
        report["results"].append(result.to_dict())
        report["health_after"] = await client.health()
    except Exception as exc:
        report["errors"].append(dict(type=type(exc).__name__, message=str(exc)))
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def run(args):
    # Reserve the output before operating the device; do not overwrite old evidence.
    path = Path(args.output).expanduser().resolve()
    with path.open("xb") as output:
        report = asyncio.run(
            validate(
                PrimePlayer(args.socket),
                args.pages,
                limit=args.limit,
                max_rows=args.max_rows,
            )
        )
        wire = json.dumps(report, indent=2, allow_nan=False).encode()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            info = tarfile.TarInfo("page-validation.json")
            info.size = len(wire)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(wire))
    print(
        json.dumps(
            dict(
                archive=str(path),
                pages=sum(len(r["pages"]) for r in report["results"]),
                errors=report["errors"],
            ),
            indent=2,
        )
    )
    return 1 if report["errors"] else 0
