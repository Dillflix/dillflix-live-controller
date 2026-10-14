"""Bounded host reassembly. Incomplete/conflicting scenes never become evidence."""

import copy, json, time


def utf16_bytes(s):
    return s.encode("utf-16-le", "surrogatepass")


def checksum(s):
    b = utf16_bytes(s)
    h = 2166136261
    for i in range(0, len(b), 2):
        h = ((h ^ (b[i] + 256 * b[i + 1])) * 16777619) & 0xFFFFFFFF
    return h


class SceneTransfer:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.pending = {}
        self.errors = []

    def error(self, tid, reason):
        self.pending.pop(tid, None)
        self.errors.append({"transfer_id": tid, "reason": reason})

    def expire(self):
        for tid, p in list(self.pending.items()):
            if self.clock() - p["started"] > 30:
                self.error(tid, "incomplete_transfer_deadline")

    def finish(self):
        for tid in list(self.pending):
            self.error(tid, "capture_ended_before_transfer_complete")

    def feed(self, rows):
        self.expire()
        out = []
        for r in rows:
            d = r.get("data", {})
            meta = d.get("result", {})
            meta = meta.get("chunkedScene") if isinstance(meta, dict) else None
            if r.get("kind") == "native_query" and meta is not None:
                tid = meta.get("id")
                if (
                    not isinstance(tid, str)
                    or type(meta.get("parts")) is not int
                    or type(meta.get("chars")) is not int
                    or not 1 <= meta["parts"] <= 32
                    or not 1 <= meta["chars"] <= 1048576
                    or meta.get("encoding") != "json-utf16"
                    or meta["parts"] != (meta["chars"] + 32767) // 32768
                ):
                    self.error(str(tid), "invalid_transfer_header")
                    continue
                if tid in self.pending:
                    self.error(tid, "duplicate_header")
                    continue
                if len(self.pending) >= 2:
                    self.error(tid, "concurrent_transfer_limit")
                    continue
                self.pending[tid] = {
                    "header": r,
                    "meta": meta,
                    "parts": {},
                    "started": self.clock(),
                }
                continue
            if r.get("kind") != "native_scene_chunk":
                out.append(r)
                continue
            tid = d.get("transferId")
            p = self.pending.get(tid)
            if not p:
                continue
            i = d.get("index")
            t = d.get("text")
            m = p["meta"]
            if (
                type(i) is not int
                or not 0 <= i < m["parts"]
                or not isinstance(t, str)
                or len(utf16_bytes(t)) // 2 > 32768
            ):
                self.error(tid, "invalid_chunk")
                continue
            if i in p["parts"]:
                if p["parts"][i] != t:
                    self.error(tid, "conflicting_duplicate_chunk")
                continue
            p["parts"][i] = t
            if len(p["parts"]) != m["parts"]:
                continue
            wire = "".join(p["parts"][j] for j in range(m["parts"]))
            if len(utf16_bytes(wire)) // 2 != m["chars"] or checksum(wire) != m.get(
                "checksum"
            ):
                self.error(tid, "length_or_checksum_mismatch")
                continue
            try:
                wire = utf16_bytes(wire).decode("utf-16-le")
                value = json.loads(wire)
                if not isinstance(value, list) or not value:
                    raise ValueError("nonempty_scene_array_required")
            except (ValueError, UnicodeError) as e:
                self.error(tid, "invalid_scene_json: " + str(e))
                continue
            result = copy.deepcopy(p["header"])
            result["data"]["result"] = value
            result["sequence"] = r["sequence"]
            result["data"]["transferEvidence"] = {
                **m,
                "verified": True,
                "lastChunkSequence": r.get("sequence"),
            }
            self.pending.pop(tid)
            out.append(result)
        return out
