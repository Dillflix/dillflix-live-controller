"""Passive Linux-host capture. Never attaches Frida or requests native inspection."""

import argparse
import json
import math
import re
import shlex
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .client import PrimePlayer

FIELDS = ("Size", "Rss", "Pss", "Private_Dirty", "Private_Clean", "Swap", "SwapPss")
HEADER = re.compile(r"^[0-9a-f]+-[0-9a-f]+\s+[rwxps-]{4}\s+", re.IGNORECASE)


def summarize_smaps(text):
    """Group by mapped path, preserving anonymous and named allocator mappings."""
    groups, total, current = {}, dict.fromkeys(FIELDS, 0), None
    mappings = 0
    for line in text.splitlines():
        if HEADER.match(line):
            parts = line.split(maxsplit=5)
            name = parts[5] if len(parts) == 6 else "[anonymous]"
            current = groups.setdefault(name, dict.fromkeys(FIELDS, 0))
            mappings += 1
        elif current is not None:
            match = re.fullmatch(r"(\w+):\s+(\d+) kB", line.strip())
            if match and match[1] in FIELDS:
                key, value = match[1], int(match[2])
                current[key] += value
                total[key] += value
    return {"units": "KiB", "mapping_count": mappings, "total": total, "groups": groups}


def adb_read(serial, command, root_prefix=""):
    if root_prefix:
        command = shlex.join(shlex.split(root_prefix)) + " " + shlex.quote(command)
    value = subprocess.run(
        ["adb", "-s", serial, "shell", command],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return value.stdout


def collect(client, serial, root_prefix=""):
    sample = {"time": datetime.now(timezone.utc).isoformat()}
    try:
        # health reads cached host state; playback_status would add native queries.
        health = client._request_sync("health", {}, 10)
        sample.update(
            session_id=health.get("session_id"),
            app_pid=health.get("app_pid"),
            failure=health.get("failure"),
            runtime_memory=health.get("runtime_memory"),
        )
        metric = sample["runtime_memory"]
        stamp = metric.get("timeMs") if isinstance(metric, dict) else None
        sample["runtime_memory_age_seconds"] = (
            max(0, time.time() - stamp / 1000) if stamp else None
        )
        pid = health.get("app_pid")
        if type(pid) is not int or pid < 1:
            raise RuntimeError(
                "service does not expose app_pid; update the player first"
            )
        # Bracket reads with the process start time, detecting exits/PID reuse.
        stat_before = adb_read(serial, f"cat /proc/{pid}/stat", root_prefix)
        sample["process_status"] = adb_read(
            serial, f"cat /proc/{pid}/status", root_prefix
        )
        smaps = adb_read(serial, f"cat /proc/{pid}/smaps", root_prefix)
        stat_after = adb_read(serial, f"cat /proc/{pid}/stat", root_prefix)
        start_before = stat_before.rsplit(") ", 1)[1].split()[19]
        start_after = stat_after.rsplit(") ", 1)[1].split()[19]
        if start_before != start_after:
            raise RuntimeError("process changed during memory reads; sample discarded")
        sample["process_start_ticks"] = start_before
        sample["smaps"] = summarize_smaps(smaps)
        if not sample["smaps"]["mapping_count"]:
            raise RuntimeError(
                "smaps was empty or unreadable; try --root-prefix 'su -c'"
            )
    except Exception as error:  # noqa: BLE001 -- preserve failed samples during an incident
        sample["error"] = str(error)
        sample.pop("smaps", None)
    try:
        sample["device_meminfo"] = adb_read(serial, "cat /proc/meminfo")
    except Exception as error:  # noqa: BLE001 -- device loss must not discard earlier samples
        sample["device_error"] = str(error)
    return sample


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument(
        "--socket",
        default=str(Path.home() / ".local/state/dillflix-prime-player/player.sock"),
    )
    parser.add_argument(
        "--root-prefix",
        default="",
        help="Use existing root for /proc reads, e.g. 'su -c'",
    )
    parser.add_argument("--duration", type=float, default=1800)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--output", type=Path, default=Path.home())
    args = parser.parse_args()
    if not math.isfinite(args.duration) or not 0 <= args.duration <= 43200:
        parser.error("duration must be 0..43200 seconds")
    if not math.isfinite(args.interval) or not 30 <= args.interval <= 3600:
        parser.error("interval must be 30..3600 seconds")
    directory = args.output / (
        "prime-memory-profile-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    directory.mkdir(parents=True)
    client = PrimePlayer(args.socket)
    deadline = time.monotonic() + args.duration
    print(
        "Passive capture: no navigation, playback queries, restart, or forced GC. Ctrl-C finishes the archive.",
        flush=True,
    )
    try:
        with (directory / "samples.jsonl").open("w", encoding="utf-8") as output:
            while True:
                sample = collect(client, args.serial, args.root_prefix)
                output.write(json.dumps(sample) + "\n")
                output.flush()
                print(
                    sample["time"]
                    + " "
                    + (sample.get("error") or f"PID {sample['app_pid']} captured"),
                    flush=True,
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(args.interval, remaining))
    except KeyboardInterrupt:
        pass
    finally:
        archive = directory.with_suffix(".tar.gz")
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(directory, arcname=directory.name)
        print(f"Capture: {archive}", flush=True)


if __name__ == "__main__":
    main()
