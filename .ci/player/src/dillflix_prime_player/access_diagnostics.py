"""Bounded access research evidence from the existing service-owned runtime.

Neither operation navigates, selects content, or attaches another Frida owner.
These diagnostic dictionaries are evidence, not entitlement classifications.
"""

from datetime import datetime, timezone
import json
import math
import re
import time
import uuid

from .diagnostic_log import scrub
from .errors import StaleResultError
from .scene import build_scene


EXPORT_BYTES = 1024 * 1024
RECORD_LIMIT = 128
SCENE_FIELDS = frozenset((
    "id", "type", "childNodeIds", "boundingBox", "visible", "focusable", "focused",
    "text", "source", "label", "title", "titleText", "subtitle", "contentType",
    "liveliness", "liveEventDateHeader", "metadataBadgeMessage", "providerName",
    "startTime", "endTime",
))
_SENSITIVE = re.compile(
    r"authorization|cookie|password|secret|token|credential|signature|manifest|"
    r"license|stream.?url|playback.?url|sessionId|playbackEnvelope|correlationId|"
    r"directedId|customerId|deviceId|^pin$", re.I,
)
_SIGNED = re.compile(
    r"[?&](?:token|key|signature|policy|credential|auth|key-pair-id|x-amz-[^=&#]*)=",
    re.I,
)
_OPTIONAL_NAVIGATION_FIELDS = {
    "$.refMarker", "$.eventGti", "$.journeyIngressContext", "$.useLimitedPlaybackFeatures",
}


def _native_metadata_complete(metadata, source):
    """Known privacy redactions retain coverage; structural omissions do not.

    The runtime caps its loss list at 32, so a full list cannot establish that
    all omissions were explained. Undefined fields are accepted only for the
    optional navigation-result properties observed in the device captures.
    """
    if not isinstance(metadata, dict) or type(metadata.get("lossless")) is not bool:
        return False
    losses = metadata.get("losses")
    if not isinstance(losses, list) or len(losses) >= 32 or "value" not in metadata:
        return False
    if metadata["lossless"] != (not losses):
        return False

    def allowed(path, reason):
        if reason in ("redacted", "sensitive_field"):
            return bool(_SENSITIVE.search(path.rsplit(".", 1)[-1]))
        if reason in ("sensitive_value", "signed_url"):
            return True
        return (reason == "undefined" and source == "navigation_result_before_policy_check"
                and path in _OPTIONAL_NAVIGATION_FIELDS)

    for loss in losses:
        if not isinstance(loss, str) or ":" not in loss:
            return False
        path, reason = loss.rsplit(":", 1)
        if not allowed(path, reason):
            return False

    def markers(value, path="$", depth=0):
        if depth > 14:
            return False
        if isinstance(value, dict):
            if "omitted" in value:
                return allowed(path, value["omitted"])
            return all(markers(v, path + "." + str(k), depth + 1) for k, v in value.items())
        if isinstance(value, list):
            return all(markers(v, path + "." + str(n), depth + 1) for n, v in enumerate(value))
        return True

    return markers(metadata["value"])


def _safe(value, losses):
    """Defence in depth for runtime redaction and unfiltered native scene fields."""
    budget = [50000]

    def loss(path, why):
        if len(losses) < 128:
            losses.append(path[:224] + ":" + why)
        return "[omitted]"

    def copy(v, path, depth):
        budget[0] -= 1
        if budget[0] < 0 or depth > 14:
            return loss(path, "structure_limit")
        if v is None or type(v) in (bool, int):
            return v
        if isinstance(v, float):
            return v if math.isfinite(v) else loss(path, "nonfinite_number")
        if isinstance(v, str):
            if len(v) > 16384:
                return loss(path, "string_limit")
            if _SIGNED.search(v) or re.match(r"^(?:Atna\||Atnr\||Bearer\s)", v, re.I):
                return loss(path, "sensitive_value")
            # Embedded JSON must receive the same key/value redaction.
            if v.lstrip().startswith(("{", "[")):
                try:
                    return json.dumps(copy(json.loads(v), path + ".json", depth + 1))
                except (ValueError, RecursionError):
                    return loss(path, "unparsed_json_string")
            return v
        if isinstance(v, dict):
            out = {}
            for n, (key, item) in enumerate(v.items()):
                if n >= 256:
                    loss(path, "key_limit")
                    break
                key = str(key)
                if len(key) > 256:
                    loss(path, "key_length_limit")
                    continue
                out[key] = (loss(path + "." + key, "sensitive_field")
                            if _SENSITIVE.search(key)
                            else copy(item, path + "." + key, depth + 1))
            return out
        if isinstance(v, (list, tuple)):
            if len(v) > 10000:
                loss(path, "item_limit")
            return [copy(item, path + "[]", depth + 1) for item in v[:10000]]
        return loss(path, "unsupported_type")

    return scrub(copy(value, "$", 0))


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _bounded_items(items, *, losses, maximum):
    out, size = [], 0
    # Leave room for envelope metadata, limits and loss descriptions.
    for item in items:
        safe = _safe(item, losses)
        count = len(json.dumps(safe, ensure_ascii=True, allow_nan=False).encode())
        if len(out) >= maximum or size + count > EXPORT_BYTES - 65536:
            losses.append("export_limit")
            break
        out.append(safe)
        size += count
    return out


def inspect_page(c, timeout=30, ownership=None):
    """Inspect the current native page, including during a manual handoff."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 5 <= timeout <= 60:
        raise ValueError("inspect_page timeout must be 5..60 seconds")
    deadline = time.monotonic() + timeout

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("page inspection deadline exceeded")
        return left

    with c.operation(ownership, readonly=True):
        c._require("native_inspection")
        generation = c._generation()
        # _ready() deliberately rejects manual ownership. Inspection is read-only
        # and must support a user who has opened details with the normal remote.
        c.backend.wait(lambda: c.backend.health()["state"].get("ready"), remaining())
        start, cid = c.backend.cursor, uuid.uuid4().hex
        c.backend.command("inspect-native", command_id=cid, timeout=min(20, remaining()))

        def received():
            return next((r for r in c.backend.records(start)
                         if r.get("kind") == "native_query"
                         and r.get("data", {}).get("inspectionId") == cid
                         and r["data"].get("method") == "inspectPage"), None)

        record = c.backend.wait(received, remaining())
        d = record["data"]
        rows = c.backend.records(start)
        if (generation != c._generation() or not d.get("success")
                or d.get("pageChanged")
                or not isinstance(d.get("pageAtRequest"), dict)
                or d["pageAtRequest"] != d.get("pageAtReceipt")
                or d["pageAtReceipt"] != c.backend.health()["state"].get("page")
                or any(r.get("kind") == "control_app_event"
                       and r.get("data", {}).get("name") == "NAVIGATE_TO_PAGE"
                       for r in rows)):
            raise StaleResultError("page or runtime changed, or native inspection failed")
        # Backend reassembles and verifies chunked transfers before exposing them.
        # Validate the complete tree before returning any allowlisted nodes.
        try:
            nodes = build_scene(record)["nodes"]
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            raise StaleResultError("native scene is incomplete or invalid") from exc
        losses = []
        scene = _bounded_items(
            ({key: value for key, value in n["raw"].items() if key in SCENE_FIELDS}
             for n in nodes), losses=losses, maximum=10000,
        )
        page = _safe(d["pageAtReceipt"], losses)
        structural_loss = any(not x.endswith((":sensitive_field", ":sensitive_value"))
                              for x in losses)
        return dict(
            schema_version=1, session_id=c.backend.session_id, captured_at=_stamp(),
            inspection_id=cid, page=page,
            runtime_generation=generation, complete=not structural_loss,
            scene=scene, scene_node_count=len(nodes), losses=losses,
            limits=dict(export_bytes=EXPORT_BYTES, scene_nodes=10000,
                        scene_fields=sorted(SCENE_FIELDS)),
            coverage="Current native scene only; no navigation or access classification.",
        )


def resolution_evidence(c, request_id, session_id=None):
    """Read retained evidence for exactly one resolution; never calls the device."""
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("request_id must be a resolution request ID")
    if session_id is not None and session_id != c.backend.session_id:
        raise StaleResultError("resolution belongs to another service session")
    snapshot = c.backend.diagnostic_snapshot()
    rows = snapshot.get("runtime_records", [])
    kinds = {"content_resolution", "content_metadata", "resolver_diagnostic", "control_completion"}
    matching = [r for r in rows if r.get("kind") in kinds
                and r.get("data", {}).get("id") == request_id]
    requested = next((r for r in matching if r["kind"] == "content_resolution"
                      and r["data"].get("phase") == "requested"), None)
    completion = next((r for r in reversed(matching) if r["kind"] == "control_completion"), None)
    start = requested.get("sequence") if requested else None
    end = completion.get("sequence") if completion else (rows[-1].get("sequence") if rows else None)
    scoped_events = [r for r in rows if r.get("kind") == "control_app_event"
                     and start is not None and end is not None
                     and start < r.get("sequence", -1) <= end]
    selected = sorted(matching + scoped_events, key=lambda r: r.get("sequence", 0))
    raw = [r for r in matching if r["kind"] == "content_metadata"
           and r["data"].get("source") == "matching_raw_resolver_response"]
    requested_id = requested["data"].get("titleId") if requested else None
    if requested_id is None and raw:
        requested_id = raw[-1]["data"].get("requestedTitleId")
    matching_raw = [r for r in raw if r["data"].get("requestedTitleId") == requested_id
                    and isinstance(r["data"].get("metadata", {}).get("value"), dict)
                    and "omitted" not in r["data"]["metadata"]["value"]]
    coverage = dict(
        requested=requested is not None,
        raw_response=bool(matching_raw),
        metadata_complete=bool(raw) and all(
            _native_metadata_complete(r["data"].get("metadata"), r["data"].get("source"))
            for r in matching if r["kind"] == "content_metadata"
        ),
        terminal=any(r["kind"] == "content_resolution"
                     and r["data"].get("phase") in ("resolved", "refused") for r in matching),
        completion=completion is not None,
    )
    losses = []
    if not coverage["metadata_complete"]:
        losses.append("runtime_metadata_incomplete")
    records = _bounded_items(selected, losses=losses, maximum=RECORD_LIMIT)
    structural_loss = any(not x.endswith((":sensitive_field", ":sensitive_value"))
                          for x in losses)
    complete = (all(coverage.values()) and len(records) == len(selected)
                and not structural_loss and not snapshot.get("failure"))
    return dict(
        schema_version=1, session_id=c.backend.session_id, request_id=request_id,
        requested_id=requested_id, captured_at=_stamp(), records=records,
        complete=complete, coverage=coverage, losses=losses,
        retained_range=dict(first_sequence=rows[0].get("sequence") if rows else None,
                            last_sequence=rows[-1].get("sequence") if rows else None),
        limits=dict(export_bytes=EXPORT_BYTES, records=RECORD_LIMIT),
        runtime_failure=_safe(snapshot.get("failure"), losses),
        retention="Current service bounded memory only; missing evidence is not an access denial.",
    )
