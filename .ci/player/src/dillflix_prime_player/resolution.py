"""Account-specific resolution, without invoking the returned navigation action."""

from dataclasses import replace
from datetime import datetime, timezone
import time
import uuid

from .errors import CompatibilityError, OutcomeUnknownError, StaleResultError
from .models import ContentResolution


def from_records(records, *, request_id, session_id, requested_id):
    """Only the matching raw response establishes action and entitlement."""
    matching = [r for r in records if r.get("data", {}).get("id") == request_id]
    raw = [r["data"].get("metadata", {}).get("value") for r in matching
           if r.get("kind") == "content_metadata"
           and r["data"].get("source") == "matching_raw_resolver_response"
           and r["data"].get("requestedTitleId") == requested_id]
    value = raw[-1] if raw and isinstance(raw[-1], dict) else {}
    action = value.get("action")
    valid = (value.get("success") is True and isinstance(action, str)
             and 0 < len(action) <= 128
             and (value.get("eventGti", requested_id) == requested_id
                  or (value.get("titleType") == "LIVE_EVENT_ITEM"
                      and value.get("gti") == requested_id))
             and any(r.get("kind") == "content_resolution"
                     and r.get("data", {}).get("phase") in ("resolved", "refused")
                     for r in matching))
    action = action if valid else None
    meta = value.get("playbackMetadata")
    meta = meta if valid and isinstance(meta, dict) else {}
    entitlement = meta.get("userEntitlementMetadata")
    public_entitlement = {}
    if isinstance(entitlement, dict):
        if isinstance(entitlement.get("entitlementType"), str):
            public_entitlement["entitlementType"] = entitlement["entitlementType"]
        benefits = entitlement.get("benefitType")
        if isinstance(benefits, list) and all(isinstance(b, str) for b in benefits):
            public_entitlement["benefitType"] = benefits
    resolved = value.get("gti")
    playback_class = None
    if meta.get("videoMaterialType") == "LiveStreaming" and meta.get("position") == "LIVE_STREAM_WATCH_NOW":
        playback_class = "live_watch_now"
    elif meta.get("videoMaterialType") == "Feature":
        playback_class = "on_demand"
    return ContentResolution(
        request_id=request_id, session_id=session_id, requested_id=requested_id,
        resolved_id=resolved if valid and isinstance(resolved, str) else None,
        action=action, entitlement=public_entitlement or None,
        playback_class=playback_class,
        observed_at=datetime.now(timezone.utc).isoformat(),
        status="resolved" if valid else "unknown",
        reason=None if valid else "matching action metadata unavailable",
    )


def resolve(c, requested_id, timeout):
    """Caller holds the operation lease; completion is required even on refusal."""
    deadline = time.monotonic() + timeout
    cid, cursor, generation = uuid.uuid4().hex, c.backend.cursor, c._generation()
    error = None
    try:
        c.backend.command("resolve-content", titleId=requested_id, command_id=cid,
                          timeout=max(0.1, timeout - 1))
    except OutcomeUnknownError as exc:
        # A details result is refused by the internal playback policy, but is
        # still a successful resolution. Do not infer why it opens details.
        error = exc

    def completion():
        return next((r for r in c.backend.records(cursor)
                     if r.get("kind") == "control_completion"
                     and r.get("data", {}).get("id") == cid), None)

    try:
        c.backend.wait(completion, max(0.1, deadline - time.monotonic()))
    except TimeoutError as exc:
        raise OutcomeUnknownError("resolution completion unavailable; no automatic retry") from (error or exc)
    rows = c.backend.records(cursor)
    if c._generation() != generation or any(
        r.get("kind") == "control_app_event"
        and r.get("data", {}).get("name") == "NAVIGATE_TO_PAGE" for r in rows
    ):
        raise StaleResultError("page or runtime changed during resolution")
    return from_records(rows, request_id=cid, session_id=c.backend.session_id,
                        requested_id=requested_id)


def enrich(c, result, remaining):
    """Resolve distinct, corroborated GTIs inside the existing operation budget."""
    cache, tiles, stopped = {}, [], None
    for tile in result.tiles:
        cid = tile.content_id
        if not cid or not cid.startswith("amzn1.dv.gti."):
            tiles.append(replace(tile, resolution_status="unresolved_identity",
                                 resolution_reason="a correlated GTI is required"))
            continue
        if cid not in cache:
            if stopped is None and remaining() < 25:
                stopped = "resolution time budget exhausted"
            if stopped is None:
                try:
                    c._require("resolve")
                    cache[cid] = resolve(c, cid, min(40, remaining() - 15))
                except OutcomeUnknownError:
                    # Drain/fence an uncertain request before returning handles.
                    c.ownership.reconcile_unknown()
                    stopped = "resolution outcome uncertain; remaining tiles not resolved"
                except CompatibilityError:
                    stopped = "resolution capability unavailable"
            if cid not in cache:
                tiles.append(replace(tile, resolution_status="unknown", resolution_reason=stopped))
                continue
        resolved = cache[cid]
        tiles.append(replace(tile, action=resolved.action, entitlement=resolved.entitlement,
                             resolution_status=resolved.status, resolution_reason=resolved.reason,
                             resolved_at=resolved.observed_at))
    warnings = result.warnings + ((stopped,) if stopped else ())
    return replace(result, tiles=tuple(tiles), warnings=warnings)
