"""Prime's native focus evidence, ported from the supplied exploration collector.

Snapshot keys retain the archive's spelling for trace comparison. Device event
times and host monotonic receipt times are milliseconds in separate clock domains.
Native focus is never a playback assertion or proof of an element's identity.
"""

import asyncio
import codecs
import copy
import math
import re
import time

PRIME = "com.amazon.firebat"
SUPPORTED_VERSION = "PVFTV-321.0096-L (321009610)"
MAX_LINE = 65536
_FIELD_NAMES = (
    "EventType",
    "EventTime",
    "PackageName",
    "Text",
    "ClassName",
    "ContentDescription",
    "ContentChangeTypes",
    "WindowChangeTypes",
    "Enabled",
    "Checked",
    "Password",
    "Scrollable",
    "FullScreen",
    "ItemCount",
    "CurrentItemIndex",
    "FromIndex",
    "ToIndex",
    "ScrollX",
    "ScrollY",
    "MaxScrollX",
    "MaxScrollY",
    "ScrollDeltaX",
    "ScrollDeltaY",
)
_FIELDS = {
    name: re.compile(r"(?:^|[;\s])" + name + r": (.*?)(?=; [A-Z][A-Za-z]+:|$)") for name in _FIELD_NAMES
}


def parse_event(line):
    # ClassName can start inside the WindowChangeTypes value's nested record.
    # Independent searches intentionally preserve the original parser's grammar.
    fields = {name: match[1].strip() for name, pattern in _FIELDS.items() if (match := pattern.search(line))}
    event_type, package = fields.get("EventType"), fields.get("PackageName")
    try:
        timestamp = float(fields["EventTime"].strip())
    except (KeyError, ValueError):
        return None
    if (
        not event_type
        or (not package and event_type != "TYPE_WINDOWS_CHANGED")
        or not math.isfinite(timestamp)
    ):
        return None

    def clean(value):
        value = value.strip() if value else None
        return None if value in (None, "", "null", "[]") else value

    text = clean(fields.get("Text"))
    if text and text.startswith("[") and text.endswith("]"):
        text = clean(text[1:-1])
    class_name = clean(fields.get("ClassName"))
    properties = {}
    for name in ("Enabled", "Checked", "Password", "Scrollable", "FullScreen"):
        value = fields.get(name, "").strip()
        if value in ("true", "false"):
            properties[name[0].lower() + name[1:]] = value == "true"
    for name in (
        "ItemCount",
        "CurrentItemIndex",
        "FromIndex",
        "ToIndex",
        "ScrollX",
        "ScrollY",
        "MaxScrollX",
        "MaxScrollY",
        "ScrollDeltaX",
        "ScrollDeltaY",
    ):
        value = fields.get(name, "").strip()
        if re.fullmatch(r"-?\d{1,16}", value) and abs(int(value)) <= 2**53 - 1:
            properties[name[0].lower() + name[1:]] = int(value)

    def flags(name):
        match = re.match(r"^\[([^\]]*)\]", fields.get(name, "").strip())
        return [part.strip() for part in match[1].split(",") if part.strip()] if match else None

    return {
        "eventType": event_type.strip(),
        "observedAt": timestamp,
        "package": package.strip() if package else None,
        "text": text,
        "contentChangeTypes": flags("ContentChangeTypes"),
        "windowChangeTypes": flags("WindowChangeTypes"),
        "contentDescription": clean(fields.get("ContentDescription")),
        "className": class_name,
        "properties": properties,
        "role": "button" if "button" in (class_name or "").lower() else class_name or "unknown",
    }


def same_validity(a, b):
    return bool(
        a
        and b
        and type(a.get("generation")) is int
        and type(a.get("revision")) is int
        and a.get("generation") == b.get("generation")
        and a.get("revision") == b.get("revision")
        and a.get("actionId") == b.get("actionId")
    )


def same_subject(a, b):
    return bool(
        a
        and b
        and all(a.get(k) == b.get(k) for k in ("package", "text", "className"))
        and (
            not a.get("contentDescription")
            or not b.get("contentDescription")
            or a["contentDescription"] == b["contentDescription"]
        )
    )


def classify_window(event, state, burst, now, span_ms):
    a, b = (state["channels"][name] for name in ("input", "accessibility"))
    paired = (
        a["usable"]
        and b["usable"]
        and same_subject(a["focus"], b["focus"])
        and all(
            f["properties"].get("enabled") is True and f["properties"].get("fullScreen") is False
            for f in (a["focus"], b["focus"])
        )
    )
    if not paired:
        return "unscoped-boundary", None
    af, bf = a["focus"], b["focus"]
    anchor, received = min(af["observedAt"], bf["observedAt"]), min(af["receivedAt"], bf["receivedAt"])
    bounded = (
        event["observedAt"] >= max(af["observedAt"], bf["observedAt"])
        and event["observedAt"] - anchor <= span_ms
        and 0 <= now - received <= span_ms
    )
    if (
        event["package"] != PRIME
        or event["contentChangeTypes"] != []
        or event["windowChangeTypes"] != []
        or not bounded
        or (burst and event["observedAt"] < burst["lastEventAt"])
    ):
        return "unscoped-boundary", None
    p = event["properties"]
    if (
        p.get("enabled") is True
        and p.get("fullScreen") is False
        and same_subject(event, af)
        and same_subject(event, bf)
    ):
        return "focus-node-echo", {
            "anchor": anchor,
            "received": received,
            "echoAt": event["observedAt"],
            "lastEventAt": event["observedAt"],
            "inputAt": af["observedAt"],
            "accessibilityAt": bf["observedAt"],
            "subject": [af["text"], af["className"], af["contentDescription"]],
            "cleanupCount": 0,
        }
    if (
        burst
        and burst["anchor"] == anchor
        and burst["received"] == received
        and burst["inputAt"] == af["observedAt"]
        and burst["accessibilityAt"] == bf["observedAt"]
        and event["observedAt"] >= burst["echoAt"]
        and event["className"] == "android.view.View"
        and event["text"] is None
        and event["contentDescription"] is None
        and p.get("enabled") is False
        and p.get("fullScreen") is False
    ):
        return "retired-virtual-node", {
            **burst,
            "lastEventAt": event["observedAt"],
            "cleanupCount": burst["cleanupCount"] + 1,
        }
    return "unscoped-boundary", None


class FocusState:
    """Synchronous, collector-local evidence state; no device or model I/O."""

    def __init__(self, *, app_version=None, now=None, max_age_ms=60000, coalesce_ms=120):
        self.now = now or (lambda: time.monotonic() * 1000)
        self.app_version, self.max_age_ms, self.coalesce_ms = app_version, max_age_ms, coalesce_ms
        self.generation = self.sequence = self.revision = 0
        self.last = None
        self._reset()

    def _reset(self):
        self.boundary, self.action = math.inf, None
        self.channels = {"input": {}, "accessibility": {}}
        self.window_boundary = self.focus_burst = self.window_evidence = self.hard_window_boundary = None
        self.window_epoch = 0
        self.unlabeled_focus_evidence = None

    def invalidate(self):
        self.generation += 1
        self.revision += 1
        self._reset()

    def begin_action(self, action, device_time=None):
        self.invalidate()
        self.sequence += 1
        self.action = {"id": self.sequence, "action": action, "dispatchedAt": self.now()}
        if device_time is not None and math.isfinite(device_time):
            self.action["deviceTime"] = device_time
            self.boundary = device_time
        return dict(self.action)

    def ingest(self, line):
        e = parse_event(line)
        if e is None:
            return
        if e["eventType"] == "TYPE_WINDOWS_CHANGED":
            if e["observedAt"] <= self.boundary or e["observedAt"] < self._window_time():
                return
            self.channels = {"input": {}, "accessibility": {}}
            self.focus_burst = None
            self.window_boundary = e["observedAt"]
            self.window_epoch += 1
            self.window_evidence = {
                "kind": "system-window-boundary",
                "event": e,
                "requiresVisualConfirmation": True,
            }
            self.hard_window_boundary = copy.deepcopy(self.window_evidence)
            self.revision += 1
            return
        if e["package"] != PRIME:
            if re.search(r"FOCUSED|WINDOW_STATE_CHANGED", e["eventType"]):
                self.invalidate()
            return
        if e["observedAt"] <= self.boundary:
            return
        if e["eventType"] == "TYPE_WINDOW_STATE_CHANGED":
            kind, burst = (
                classify_window(e, self.snapshot(), self.focus_burst, self.now(), self.coalesce_ms)
                if (self.app_version == SUPPORTED_VERSION)
                else ("unscoped-boundary", None)
            )
            self.focus_burst = burst
            self.window_evidence = {
                "kind": kind,
                "event": e,
                "scope": "event-burst-only",
                "instanceIdentityEstablished": False,
                "requiresVisualConfirmation": bool(burst),
                "appVersion": self.app_version,
                **({"burst": copy.deepcopy(burst)} if burst else {}),
            }
            if kind == "unscoped-boundary":
                self.window_boundary = max(self._window_time(), e["observedAt"])
                self.window_epoch += 1
                empty_disabled = (
                    e["className"] == "android.view.View"
                    and e["text"] is None
                    and e["contentDescription"] is None
                    and e["properties"].get("enabled") is False
                    and e["properties"].get("fullScreen") is False
                    and e["contentChangeTypes"] == []
                    and e["windowChangeTypes"] == []
                )
                if not empty_disabled or not self.hard_window_boundary:
                    self.hard_window_boundary = {
                        "kind": kind,
                        "event": copy.deepcopy(e),
                        "requiresVisualConfirmation": True,
                    }
            self.revision += 1
            return
        if self.focus_burst:
            burst = self.focus_burst
            closure = (
                e["eventType"] == "TYPE_WINDOW_CONTENT_CHANGED"
                and e["className"] == "android.view.View"
                and e["text"] is None
                and e["contentDescription"] is None
                and e["properties"].get("enabled") is True
                and e["properties"].get("fullScreen") is False
                and e["contentChangeTypes"] == ["CONTENT_CHANGE_TYPE_SUBTREE"]
                and e["windowChangeTypes"] == []
                and e["observedAt"] >= burst["lastEventAt"]
                and e["observedAt"] - burst["anchor"] <= self.coalesce_ms
                and self.now() - burst["received"] <= self.coalesce_ms
            )
            if closure:
                self.window_evidence = {
                    **self.window_evidence,
                    "closedBy": e,
                    "kind": "virtual-node-focus-burst",
                }
            else:
                self.window_boundary = max(self._window_time(), e["observedAt"])
                self.window_epoch += 1
                self.window_evidence = {
                    "kind": "incomplete-focus-burst",
                    "event": e,
                    "requiresVisualConfirmation": True,
                }
                self.hard_window_boundary = copy.deepcopy(self.window_evidence)
            self.focus_burst = None
            self.revision += 1
        channel = {
            "TYPE_VIEW_FOCUSED": "input",
            "TYPE_VIEW_ACCESSIBILITY_FOCUSED": "accessibility",
            "TYPE_VIEW_ACCESSIBILITY_FOCUS_CLEARED": "accessibility",
        }.get(e["eventType"])
        if channel is None or re.search(r"Ignite|IgnitionActivity|FrameLayout", e["className"] or ""):
            return
        if e["observedAt"] < self._window_time():
            return
        slot = self.channels[channel]
        if e["observedAt"] < slot.get("eventAt", -math.inf):
            return
        at = self.now()
        transport_age = (
            max(0, self.action["deviceTime"] + at - self.action["dispatchedAt"] - e["observedAt"])
            if self.action
            else 0
        )
        if transport_age > self.max_age_ms or slot.get("lastEvent") == e:
            return
        slot["lastEvent"] = e
        if e["eventType"] == "TYPE_VIEW_ACCESSIBILITY_FOCUS_CLEARED":
            if e["text"] and slot.get("focus") and e["text"] != slot["focus"]["text"]:
                return
            slot.update(eventAt=e["observedAt"], clearedAt=e["observedAt"])
            self.revision += 1
            return
        if not e["text"]:
            if not e["className"] or e["className"] == "button":
                slot.update(eventAt=e["observedAt"], unlabeledAt=e["observedAt"])
                self.unlabeled_focus_evidence = {
                    "event": copy.deepcopy(e),
                    "channel": channel,
                    "actionId": self.action_id,
                    "generation": self.generation,
                    "receivedAt": at,
                    "scope": "action-local-diagnostic",
                    "establishesCurrentIdentity": False,
                }
                self.revision += 1
            return
        if slot.get("focus", {}).get("observedAt") == e["observedAt"] and slot["focus"]["text"] != e["text"]:
            slot["ambiguousAt"] = e["observedAt"]
        elif e["observedAt"] > (
            slot.get("ambiguousAt") if slot.get("ambiguousAt") is not None else -math.inf
        ):
            slot["ambiguousAt"] = None
        duplicate = bool(
            self.last
            and self.last["generation"] == self.generation
            and same_subject(self.last, e)
            and at - self.last["receivedAt"] <= self.coalesce_ms
        )
        if not duplicate:
            self.sequence += 1
        self.last = {
            **e,
            "eventTypes": list(
                dict.fromkeys([*(self.last["eventTypes"] if duplicate else []), e["eventType"]])
            ),
            "properties": {**(self.last["properties"] if duplicate else {}), **e["properties"]},
            "contentDescription": e["contentDescription"]
            or (self.last["contentDescription"] if duplicate else None),
            "source": "prime-accessibility",
            "confidence": "high",
            "transportAgeMs": transport_age,
            "receivedAt": at,
            "generation": self.generation,
            "actionId": self.action_id,
            "windowEpoch": self.window_epoch,
            "transitionId": self.last["transitionId"] if duplicate else self.sequence,
        }
        slot["focus"] = {**self.last, **e, "eventTypes": [e["eventType"]]}
        slot["eventAt"] = e["observedAt"]
        self.revision += 1

    def _window_time(self):
        return self.window_boundary if self.window_boundary is not None else -math.inf

    @property
    def action_id(self):
        return self.action["id"] if self.action else None

    def _read_channel(self, slot):
        focus = slot.get("focus")
        age = self.now() - focus["receivedAt"] + focus["transportAgeMs"] if focus else None
        if "clearedAt" in slot and (not focus or slot["clearedAt"] >= focus["observedAt"]):
            reason = "focus_cleared"
        elif "unlabeledAt" in slot and (not focus or slot["unlabeledAt"] >= focus["observedAt"]):
            reason = "unlabeled_control_focused"
        elif slot.get("ambiguousAt") is not None:
            reason = "conflicting_focus_events"
        elif not focus:
            reason = "no_event"
        elif self.window_boundary is not None and (
            self.window_boundary > focus["observedAt"] or focus["windowEpoch"] != self.window_epoch
        ):
            reason = "window_boundary"
        elif not 0 <= age <= self.max_age_ms:
            reason = "expired"
        else:
            reason = "fresh"
        return {
            "focus": copy.deepcopy(focus),
            "ageMs": age,
            "usable": reason == "fresh",
            "reason": reason,
            **({"clearedAt": slot["clearedAt"]} if "clearedAt" in slot else {}),
        }

    def snapshot(self):
        channels = {name: self._read_channel(slot) for name, slot in self.channels.items()}
        a, b = channels["input"], channels["accessibility"]
        equivalent = a["usable"] and b["usable"] and same_subject(a["focus"], b["focus"])
        disagreement = a["usable"] and b["usable"] and not equivalent
        selected = a if a["reason"] != "no_event" else b
        usable = selected["usable"] and not disagreement and not self.focus_burst
        focus = selected["focus"] or self.last
        if equivalent:
            af, bf = a["focus"], b["focus"]
            focus = {
                **(af if af["observedAt"] >= bf["observedAt"] else bf),
                "contentDescription": af["contentDescription"] or bf["contentDescription"],
                "eventTypes": list(dict.fromkeys([*af["eventTypes"], *bf["eventTypes"]])),
            }
        window_evidence = copy.deepcopy(self.window_evidence)
        if (
            window_evidence
            and window_evidence.get("burst")
            and (
                window_evidence["burst"]["inputAt"] != (a["focus"] or {}).get("observedAt")
                or window_evidence["burst"]["accessibilityAt"] != (b["focus"] or {}).get("observedAt")
            )
        ):
            window_evidence = {
                "kind": "prior-burst-window-uncertainty",
                "scope": "window-epoch",
                "requiresVisualConfirmation": True,
                "instanceIdentityEstablished": False,
                "origin": window_evidence,
            }
        reason = selected["reason"]
        if reason == "no_event" and self.last and self.last["generation"] != self.generation:
            reason = "no_labeled_focus_after_action" if self.action else "focus_invalidated"
        return {
            "lastInput": copy.deepcopy(self.action),
            "focus": copy.deepcopy(focus),
            "windowEvidence": window_evidence,
            "hardWindowBoundary": copy.deepcopy(self.hard_window_boundary),
            "focusProjection": {
                "source": "none"
                if not focus
                else "matching-channel-labels"
                if equivalent
                else "channel-record"
                if selected["focus"]
                else "retained-history",
                "historicalFallback": not selected["focus"] and bool(self.last),
                "usableCurrentEvidence": bool(usable),
            },
            "unlabeledFocusEvidence": copy.deepcopy(self.unlabeled_focus_evidence),
            "ageMs": selected["ageMs"],
            "usable": bool(usable),
            "channels": channels,
            "association": "unresolved-different-subjects"
            if disagreement
            else "matching-labels-not-instance-proof"
            if equivalent
            else "single-channel"
            if a["usable"] or b["usable"]
            else "no-usable-channels",
            "validity": {
                "generation": self.generation,
                "revision": self.revision,
                "actionId": self.action_id,
            },
            "reason": "focus_burst_pending"
            if self.focus_burst
            else "focus_channels_disagree"
            if disagreement
            else reason,
        }


def associate_capture(before, after, finalized):
    """Bind native evidence to the whole capture/preparation interval."""
    associated = same_validity(before.get("validity"), after.get("validity")) and same_validity(
        after.get("validity"), finalized.get("validity")
    )
    result = copy.deepcopy(after)
    result["usable"] = after["usable"] and finalized["usable"] and associated
    result["reason"] = "focus_changed_during_capture" if not associated else finalized["reason"]
    result["focusProjection"]["usableCurrentEvidence"] = result["usable"]
    result["captureAssociation"] = {
        "status": "unchanged-during-capture" if associated else "changed-during-capture",
        "before": before.get("validity"),
        "after": after.get("validity"),
        "finalized": finalized.get("validity"),
    }
    return result


def observation_validity(captured, current):
    if not captured or not captured.get("validity"):
        return {"status": "unknown", "reason": "event_validity_unavailable", "screenContinuity": "unproven"}
    same = same_validity(captured["validity"], current.get("validity")) and not (
        captured["usable"] and not current["usable"]
    )
    associated = captured.get("captureAssociation", {}).get("status") == "unchanged-during-capture"
    return {
        "status": "unchanged" if same and associated else "changed",
        "reason": "focus_or_input_evidence_changed"
        if not same
        else "capture_association_unproven"
        if not associated
        else "event_evidence_unchanged",
        "captured": captured["validity"],
        "current": current.get("validity"),
        "scope": "input-and-focus-events-only",
        "screenContinuity": "unproven",
    }


class EventLines:
    """Incremental UTF-8/CRLF decoder; oversized records are discarded whole."""

    def __init__(self, ingest):
        self.ingest, self.buffer, self.discarding = ingest, "", False
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, chunk):
        pieces = self.decoder.decode(chunk).split("\n")
        for index, piece in enumerate(pieces):
            terminated = index < len(pieces) - 1
            if not self.discarding:
                if len(self.buffer) + len(piece) > MAX_LINE:
                    self.discarding, self.buffer = True, ""
                else:
                    self.buffer += piece
            if terminated:
                if not self.discarding:
                    self.ingest(self.buffer.rstrip("\r"))
                self.buffer, self.discarding = "", False


async def drain(task):
    """Finish owned subprocess cleanup even if the caller is cancelled repeatedly."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


class FocusCollector:
    """One asynchronous, reconnecting uiautomator event stream for one device."""

    def __init__(self, command, version_lookup, *, timeout=10, reconnect_seconds=1, state=None, spawn=None):
        self.command, self.version_lookup = tuple(command), version_lookup
        self.timeout, self.reconnect_seconds = timeout, reconnect_seconds
        self.state = state or FocusState()
        self.spawn = spawn or asyncio.create_subprocess_exec
        self.process = self.runner = None
        self.status, self.version_status = "stopped", "unavailable"
        self._lifecycle = asyncio.Lock()
        self._first_attempt = asyncio.Event()

    async def start(self):
        async with self._lifecycle:
            if self.runner is None or self.runner.done():
                self._first_attempt = asyncio.Event()
                self.runner = asyncio.create_task(self._run(), name="prime-accessibility")
            initialized = self._first_attempt
        # A missing reader/version must not hang the navigation worker. The
        # stream can recover later; until then native evidence remains unknown.
        try:
            await asyncio.wait_for(initialized.wait(), self.timeout + 0.5)
        except TimeoutError:
            pass

    def invalidate(self):
        self.state.invalidate()

    def begin_action(self, action, device_time=None):
        return self.state.begin_action(action, device_time)

    def snapshot(self):
        return {
            **self.state.snapshot(),
            "streamStatus": self.status,
            "appVersion": self.state.app_version,
            "appVersionStatus": self.version_status,
        }

    async def _version(self, process, initialized):
        version = None
        try:
            async with asyncio.timeout(self.timeout):
                version = await self.version_lookup()
        except Exception:
            pass
        finally:
            if self.process is process:
                self.state.app_version = version
                self.version_status = "observed" if version else "unavailable"
            initialized.set()

    async def _discard_stderr(self, process):
        while await process.stderr.read(65536):
            pass

    async def _cleanup(self, process, tasks):
        for task in tasks:
            task.cancel()
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await asyncio.gather(
            *(tasks + ([asyncio.create_task(process.wait())] if process else [])), return_exceptions=True
        )

    async def _run(self):
        initialized = self._first_attempt
        try:
            while True:
                self.state.invalidate()
                self.state.app_version, self.version_status = None, "unavailable"
                self.status = "connecting"
                process, tasks = None, []
                try:
                    # Drain spawn on cancellation too: losing a process handle
                    # between creation and assignment must not orphan a reader.
                    spawning = asyncio.create_task(
                        self.spawn(
                            *self.command,
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                    )
                    try:
                        process = await asyncio.shield(spawning)
                    except asyncio.CancelledError:
                        await drain(asyncio.gather(spawning, return_exceptions=True))
                        if not spawning.cancelled() and spawning.exception() is None:
                            process = spawning.result()
                        raise
                    self.process, self.status = process, "connected"
                    tasks = [
                        asyncio.create_task(self._version(process, initialized)),
                        asyncio.create_task(self._discard_stderr(process)),
                    ]
                    lines = EventLines(self.state.ingest)
                    while chunk := await process.stdout.read(65536):
                        lines.feed(chunk)
                except (OSError, ValueError):
                    pass
                finally:
                    self.process = None
                    self.state.invalidate()
                    self.state.app_version, self.version_status = None, "unavailable"
                    self.status = "disconnected"
                    initialized.set()
                    cleanup = asyncio.create_task(self._cleanup(process, tasks))
                    if await drain(cleanup):
                        raise asyncio.CancelledError
                await asyncio.sleep(self.reconnect_seconds)
        finally:
            self.status = "stopped"
            self.state.invalidate()
            initialized.set()

    async def wait_for_focus(self, action, timeout_ms=900):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            state = self.snapshot()
            if not action or self.state.action_id != action["id"]:
                return state
            if (
                state["usable"]
                and state["focus"]["actionId"] == action["id"]
                and self.state.now() - state["focus"]["receivedAt"] >= self.state.coalesce_ms
            ):
                return state
            await asyncio.sleep(min(0.02, max(0, deadline - time.monotonic())))
        return self.snapshot()

    async def stop(self):
        async with self._lifecycle:
            self.state.invalidate()
            if self.runner is not None:
                self.runner.cancel()
                cancelled = await drain(asyncio.gather(self.runner, return_exceptions=True))
                self.runner = None
                self.status = "stopped"
                if cancelled:
                    raise asyncio.CancelledError


def focus_metadata(state):
    """Compact actor data, retaining unknown/historical and per-channel scope."""
    if not state:
        return None
    channels = {}
    for name, channel in state.get("channels", {}).items():
        focus = channel.get("focus") or {}
        channels[name] = {
            "status": channel["reason"],
            "usable": channel["usable"],
            "text": focus.get("text"),
            "role": focus.get("role"),
            "contentDescription": focus.get("contentDescription"),
            "ageMs": channel["ageMs"],
            "observedAt": focus.get("observedAt"),
            **({"clearedAt": channel["clearedAt"]} if "clearedAt" in channel else {}),
        }
    focus = state.get("focus") or {}
    metadata = {
        "source": "prime-accessibility",
        "currentFocusEstablished": bool(state.get("usable")),
        "reason": state.get("reason"),
        "association": state.get("association"),
        "channels": channels,
        "labelScope": "unresolved: element or containing group",
        "identityGrounding": "not established by event freshness",
        "validity": state.get("validity"),
        "captureAssociation": state.get("captureAssociation"),
    }
    if not state.get("usable") or focus.get("confidence") != "high":
        metadata["currentFocusEstablished"] = False
        metadata["meaning"] = (
            "Cleared/expired/unassociated labels are historical. Channels may describe different subjects or semantic levels."
        )
        return metadata
    metadata.update(
        text=focus.get("text"),
        role=focus.get("role"),
        contentDescription=focus.get("contentDescription"),
        ageMs=state.get("ageMs"),
    )
    window = state.get("windowEvidence")
    if window:
        metadata["windowContext"] = {
            "classification": window["kind"],
            "requiresVisualConfirmation": bool(window.get("requiresVisualConfirmation")),
            "meaning": "Window-event provenance does not establish current visual instance identity or window continuity.",
        }
    context = re.match(r"^\[([^\]]+)\]\s*", focus.get("contentDescription") or "")
    if context and len(context[1]) <= 120:
        metadata["descriptionContext"] = context[1]
    properties, facts = focus.get("properties", {}), {}
    for key, value in (("enabled", False), ("checked", True), ("scrollable", True)):
        if properties.get(key) is value:
            facts[key] = value
    count, index = properties.get("itemCount"), properties.get("currentItemIndex")
    if type(count) is int and count > 1 and type(index) is int and 0 <= index < count:
        facts["collection"] = {"itemCount": count, "currentItemIndex": index}
    for axis in ("X", "Y"):
        maximum, offset = properties.get("maxScroll" + axis), properties.get("scroll" + axis)
        if type(maximum) is int and maximum > 0 and type(offset) is int and 0 <= offset <= maximum:
            facts.setdefault("scroll", {})[axis.lower()] = {"offset": offset, "max": maximum}
    if facts:
        metadata["eventProperties"] = facts
    return metadata
