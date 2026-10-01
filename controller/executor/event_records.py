"""Bounded, channel-local UIAutomator framing from the capture-05 recorder.

Text and ContentDescription can contain literal newlines. Only recordCount ends
an event; partial transport state also participates in screenshot validity.
"""

import codecs
import copy
import re

MAX_RECORD = 1024 * 1024
START = re.compile(r"^(?:\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\s+)?EventType: TYPE_[A-Z_]+;")
END = re.compile(r";\s*recordCount:\s*\d+\s*$")


class TransportEvidence:
    def __init__(self):
        self.pending, self.revision = set(), 0

    def set(self, channel, active):
        if (channel in self.pending) == active:
            return
        if active:
            self.pending.add(channel)
        else:
            self.pending.discard(channel)
        self.revision += 1

    def snapshot(self, focus):
        result = copy.deepcopy(focus)
        result["validity"]["transportRevision"] = self.revision
        result["transport"] = {"pendingRecords": sorted(self.pending)}
        if self.pending:
            result.update(usable=False, reason="incomplete_event_record")
            result["focusProjection"]["usableCurrentEvidence"] = False
            for channel in result["channels"].values():
                channel.update(usable=False, reason="incomplete_event_record")
        return result


class EventRecords:
    def __init__(self, record, *, pending, truncated, diagnostic=lambda _: None, limit=MAX_RECORD):
        self.record, self.pending, self.truncated, self.diagnostic = record, pending, truncated, diagnostic
        self.limit, self.buffer = limit, None

    def push(self, line):
        starts = bool(START.match(line))
        if starts and self.buffer is not None:
            self.discard("next_event_before_terminator")
        if self.buffer is None and not starts:
            self.diagnostic(line)
            return
        self.buffer = line if self.buffer is None else self.buffer + "\n" + line
        if len(self.buffer) > self.limit:
            self.discard("event_size_limit")
        elif END.search(self.buffer):
            raw, self.buffer = self.buffer, None
            self.pending(False)
            self.record(raw)
        else:
            self.pending(True)

    def discard(self, reason):
        if self.buffer is not None:
            self.buffer = None
            self.pending(False)
            self.truncated(reason)

    def end(self):
        self.discard("stream_ended_before_terminator")


class RecordStream:
    """Incremental UTF-8, complete physical lines, then complete event records."""

    def __init__(self, records, transport, channel, *, limit=MAX_RECORD):
        self.records, self.transport, self.channel, self.limit = records, transport, channel, limit
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.line, self.dropping = "", False

    def feed(self, chunk):
        parts = self.decoder.decode(chunk).split("\n")
        for index, part in enumerate(parts):
            if not self.dropping:
                if len(self.line) + len(part) > self.limit:
                    self.line, self.dropping = "", True
                    if self.records.buffer is not None:
                        self.records.discard("physical_line_size_limit")
                    else:
                        self.records.truncated("physical_line_size_limit")
                else:
                    self.line += part
            if index < len(parts) - 1:
                if not self.dropping:
                    self.records.push(self.line.rstrip("\r"))
                self.line, self.dropping = "", False
            self.transport.set(self.channel + ":fragment", bool(self.line) or self.dropping)

    def end(self):
        # A complete final record need not have a trailing newline. A partial
        # record is rejected by EventRecords.end, never parsed as a short label.
        self.line += self.decoder.decode(b"", final=True)
        if self.line and not self.dropping:
            self.records.push(self.line.rstrip("\r"))
        self.records.end()
        self.transport.set(self.channel + ":fragment", False)
