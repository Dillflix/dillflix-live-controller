"""Reuse committed catalogue projections without caching playback decisions."""
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from .planner import parse_time


def next_boundary(items, now):
    # Expiry changes freshness, including the stale flag of terminal evidence.
    # A scheduled start can enable a Prime catalogue check without a DB write.
    return min((at for item in items
                for value in (item["start_time"], item["lifecycle"].get("effective_valid_until"))
                if (at := parse_time(value)) is not None and at > now), default=None)


@dataclass
class Entry:
    value: bytes
    created_at: datetime
    expires_at: datetime | None
    size: int


class CatalogueCache:
    def __init__(self, *, max_bytes=64 * 1024 * 1024, max_entries=16):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.entries = OrderedDict()
        self.bytes = 0
        # Coalesce concurrent worker/overview/display builds. Builders only read
        # their WAL snapshot; a writer never waits for this lock. RLock permits
        # the enriched projection to reuse the separately cached source view.
        self.lock = threading.RLock()

    def read(self, key, clock, build):
        with self.lock:
            # Sample after waiting for another builder. A request's earlier
            # arrival time is not a backwards system-clock movement.
            now = clock()
            entry = self.entries.pop(key, None)
            if entry:
                self.bytes -= entry.size
            if not entry or now < entry.created_at or (entry.expires_at and now >= entry.expires_at):
                value = build(now)
                wire = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
                entry = Entry(wire, now, next_boundary(value, now), len(wire) + len(repr(key).encode()) + 512)
            if entry.size <= self.max_bytes and self.max_entries > 0:
                self.entries[key] = entry
                self.bytes += entry.size
                while self.bytes > self.max_bytes or len(self.entries) > self.max_entries:
                    _, expired = self.entries.popitem(last=False)
                    self.bytes -= expired.size
        # Immutable compact storage avoids retaining many duplicated Python
        # object graphs. Each reader gets independent data without recomputing
        # lifecycle, route eligibility, identities or entitlement enrichment.
        return json.loads(entry.value)
