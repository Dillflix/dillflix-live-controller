"""Bounded per-event projections of committed catalogue data."""
import json
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime

from .planner import parse_time

log = logging.getLogger(__name__)


@dataclass
class Entry:
    value: bytes
    snapshot: bytes | None
    created_at: datetime
    expires_at: datetime | None
    size: int

    def valid(self, now):
        return self.created_at <= now and (self.expires_at is None or now < self.expires_at)


def entry_for(key, item, now):
    boundaries = (item["start_time"], item["lifecycle"].get("effective_valid_until"))
    expiry = min((at for value in boundaries if (at := parse_time(value)) and at > now), default=None)
    wire = json.dumps({k: v for k, v in item.items() if k != "snapshot"},
                      ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    snapshot = json.dumps(item["snapshot"], ensure_ascii=True, separators=(",", ":"),
                          allow_nan=False).encode("ascii") if "snapshot" in item else None
    return Entry(wire, snapshot, now, expiry, len(wire) + len(snapshot or b"") + len(repr(key).encode()) + 512)


class CatalogueCache:
    def __init__(self, *, max_bytes=64 * 1024 * 1024, max_entries=16384):
        if max_bytes < 0 or max_entries < 0:
            raise ValueError("Cache limits must be nonnegative")
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.entries = OrderedDict()
        self.families = {}
        self.entry_families = {}
        self.bytes = 0
        self.building = {}
        self.lock = threading.Lock()

    def discard(self, key):
        entry = self.entries.pop(key, None)
        if entry:
            self.bytes -= entry.size
        family = self.entry_families.pop(key, None)
        if family is not None and self.families.get(family, (None, None))[1] == key:
            self.families.pop(family)

    def read(self, keys, clock, build, *, device_id=None, families=None, include_snapshot=True):
        """Return independent items in input order; batch only missing IDs.

        keys maps content IDs to immutable snapshot/intent identities. No SQL,
        projection, encoding, decoding or waiting happens under the cache lock.
        A pending build coalesces only readers of those exact event identities.
        """
        started = time.monotonic()
        ready, owned, waiting = {}, {}, {}
        with self.lock:
            now = clock()
            for cid, key in keys.items():
                entry = self.entries.get(key)
                if entry and entry.valid(now):
                    self.entries.move_to_end(key)
                    ready[cid] = entry
                    continue
                if entry:
                    self.discard(key)
                future = self.building.get(key)
                if future is None:
                    future = owned[cid] = self.building[key] = Future()
                else:
                    waiting[cid] = future
        indexed = time.monotonic()
        hits = len(ready)
        # Publish our own work before waiting on peers. Overlapping batches
        # cannot form a wait cycle. Builders only read their committed snapshot.
        if owned:
            try:
                values = {item["content_id"]: item for item in build(set(owned), now)}
                built = {cid: entry_for(keys[cid], values[cid], now) for cid in owned}
                with self.lock:
                    for cid, entry in built.items():
                        key = keys[cid]
                        family, revision = families[cid] if families else (None, None)
                        previous = self.families.get(family)
                        retain = previous is None or previous[0] <= revision
                        if retain and previous and previous[1] != key:
                            self.discard(previous[1])
                        if retain and entry.size <= self.max_bytes and self.max_entries > 0:
                            self.entries[key] = entry
                            self.bytes += entry.size
                            if family is not None:
                                self.families[family] = (revision, key)
                                self.entry_families[key] = family
                        self.building.pop(key)
                    while self.bytes > self.max_bytes or len(self.entries) > self.max_entries:
                        self.discard(next(iter(self.entries)))
                ready.update(built)
                for cid, future in owned.items():
                    future.set_result(built[cid])
            except BaseException as error:
                with self.lock:
                    for cid in owned:
                        self.building.pop(keys[cid], None)
                for future in owned.values():
                    future.set_exception(error)
                raise
        constructed = time.monotonic()
        for cid, future in waiting.items():
            entry = future.result()
            # A peer started before this request. If its view had already
            # expired when our read began, refresh only that event locally.
            if not entry.valid(now):
                item, = build({cid}, now)
                entry = entry_for(keys[cid], item, now)
            ready[cid] = entry
        waited = time.monotonic()
        # Share immutable event bytes across streams, but return independent
        # caller objects without retaining entire catalogue variants.
        def wire(entry):
            if include_snapshot and entry.snapshot is not None:
                return entry.value[:-1] + b',"snapshot":' + entry.snapshot + b"}"
            return entry.value
        result = json.loads(b"[" + b",".join(wire(ready[cid]) for cid in keys) + b"]")
        finished = time.monotonic()
        if finished - started >= 1:
            log.warning("Slow catalogue cache: device=%s seconds=%.3f hits=%d built=%d waited=%d "
                        "retained_bytes=%d stages=%s", device_id, finished - started, hits, len(owned),
                        len(waiting), self.bytes, {"lookup": round(indexed - started, 3),
                        "build": round(constructed - indexed, 3), "wait": round(waited - constructed, 3),
                        "decode": round(finished - waited, 3)})
        return result
