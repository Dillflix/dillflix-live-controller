"""Device operations; only this layer owns navigation and playback mutations."""

import re
import math
import threading
import time
from .errors import (
    BusyError,
    PrimeError,
    PlaybackRefusedError,
    StaleResultError,
    OutcomeUnknownError,
    CancelledError,
)
from .search import search_results
from .playback import observe_playback
from .ownership import Ownership


class Controller:
    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.Lock()
        self.handles = {}
        self.attempts = {}
        self.suspended = False
        self.handle_runtime_generation = None
        self.current_attempt_id = None
        self.ownership = Ownership(self)

    def operation(self, expected=None, readonly=False):
        return self.ownership.operation(expected, readonly)

    def _ready(self, timeout=20):
        if self.suspended:
            raise BusyError("automatic control is suspended")
        self.backend.wait(lambda: self.backend.health()["state"].get("ready"), timeout)

    def compatibility(self):
        return self.backend.compatibility()

    def _generation(self):
        snapshot = getattr(self.backend, "compatibility", None)
        return snapshot().get("generation") if snapshot else None

    def _require(self, capability):
        check = getattr(self.backend, "require_capability", None)
        if check:
            check(capability)

    def health(self):
        return {
            **self.backend.health(),
            "api_version": 11,
            "ownership": self.ownership.receipt(),
            "capability_list_scope": "implemented_api; check compatibility.capabilities for availability",
            "capabilities": [
                "search",
                "resolve",
                "broadcasts",
                "inspect_page",
                "resolution_evidence",
                "pages",
                "discover",
                "play",
                "attempt",
                "playback_status",
                "suspend",
                "resume",
                "cancel",
                "stop",
                "manual_input",
            ],
            "suspended": self.suspended,
            "busy": self.lock.locked(),
        }

    def _active_attempts(self):
        for a in self.attempts.values():
            if (
                a["state"] in ("resolving", "launch_requested", "observing")
                and time.monotonic() > a["deadline"]
            ):
                a.update(state="unknown", reason="verification deadline exceeded")
        return any(
            a["state"] in ("resolving", "launch_requested", "observing")
            for a in self.attempts.values()
        )

    def inspect_page(self, *, timeout=30, ownership=None):
        from .access_diagnostics import inspect_page

        return inspect_page(self, timeout, ownership)

    def resolution_evidence(self, request_id, *, session_id=None):
        from .access_diagnostics import resolution_evidence

        return resolution_evidence(self, request_id, session_id)

    def suspend(self, value, owner_id=None, previous=None):
        if type(value) is not bool:
            raise ValueError("suspended must be boolean")
        return self.ownership.transition(
            "manual" if value else "automatic",
            owner_id=owner_id or "local-client",
            previous=previous,
        )

    def cancel(self, session_id, attempt_id=None, ownership=None):
        return self.ownership.transition(
            "automatic",
            session_id=session_id,
            attempt_id=attempt_id,
            previous=ownership,
            cancel_only=True,
        )

    def manual_input(self, receipt, seq, key=None, text=None):
        return self.ownership.manual_input(receipt, seq, key=key, text=text)

    def stop(self, attempt_id, session_id, ownership=None):
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("stop requires an attempt ID")
        if session_id != self.backend.session_id:
            raise StaleResultError("service session changed")
        barrier = self.cancel(session_id, attempt_id, ownership)
        if barrier.get("cancelled") is False:
            return {"stopped": False, "reason": "attempt_not_current"}
        with self.operation(barrier):
            if attempt_id != self.current_attempt_id:
                return {"stopped": False, "reason": "attempt_not_current"}
            a = self.attempts[attempt_id]
            self._observe_attempt(a)
            status = observe_playback(self.backend, a, timeout=30)
            if status.get("matches_attempt") is not True:
                return {
                    "stopped": False,
                    "reason": "current_playback_identity_unproven",
                    "status": status,
                }
            stop_start = self.backend.cursor
            receipt = self.backend.command(
                "stop-playback",
                expectedTitleId=a["resolved_id"],
                expectedPlaybackEpoch=status["evidence"]["checkpoint"]["epoch"],
                attemptId=attempt_id,
            )
            # wait() holds the backend condition lock while evaluating its
            # predicate. A synchronous Frida RPC here would block the message
            # callback needed to complete that same RPC.
            deadline = time.monotonic() + 15
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("playback page exit not observed after stop")
                checkpoint = self.backend.command(
                    "playback-status", timeout=min(5, remaining)
                )["result"]
                page_type = checkpoint.get("page", {}).get("pageType")
                if page_type and page_type != "RUST_PLAYBACK":
                    break
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
            expected_epoch = status["evidence"]["checkpoint"]["epoch"]
            confirmed = any(
                r.get("kind") == "control_app_event"
                and r["data"].get("name") == "PLAYBACK_STATE_CHANGE"
                and r["data"].get("playbackEpoch") == expected_epoch
                and r["data"].get("state", {}).get("page", {}).get("pageId")
                == a["resolved_id"]
                and r["data"].get("params", {}).get("value", {}).get("state")
                in ("Stopped", "Ended")
                for r in self.backend.records(stop_start)
            )
            a.update(
                state="stopped" if confirmed else "unknown",
                reason=(
                    "matching stopped event observed"
                    if confirmed
                    else "playback page exited; native stop not confirmed"
                ),
            )
            return {
                "stopped": True if confirmed else None,
                "page_exited": True,
                "stop_requested": True,
                "attempt_id": attempt_id,
                "session_id": session_id,
                "receipt": receipt["id"],
            }

    def pages(self, timeout=60, ownership=None):
        from .browsing import inventory

        return inventory(self, timeout, ownership)

    def discover(
        self,
        pages,
        limit_per_page=20,
        max_rows=2,
        max_steps=16,
        timeout=400,
        ownership=None,
        resolve_access=True,
    ):
        from .browsing import discover

        return discover(
            self, pages, limit_per_page, max_rows, max_steps, timeout, ownership, resolve_access
        )

    def broadcasts(self, content_id, timeout=45, ownership=None):
        from .broadcasts import fetch

        return fetch(self, content_id, timeout, ownership)

    def search(self, query, timeout=45, ownership=None):
        from .catalog import search

        return search(self, query, timeout, ownership)

    def _search_renderer(self, query, timeout=100, ownership=None, resolve_access=True):
        if not isinstance(query, str) or not query.strip() or len(query) > 512:
            raise ValueError("query must contain 1..512 characters")
        if not 60 <= timeout <= 120:
            raise ValueError("search timeout must be 60..120 seconds")
        if type(resolve_access) is not bool:
            raise ValueError("resolve_access must be boolean")
        with self.operation(ownership):
            if self._active_attempts():
                raise BusyError("finish playback verification before another search")
            deadline = time.monotonic() + timeout

            def remaining():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("search deadline exceeded")
                return left

            if any(a["state"] == "unknown" for a in self.attempts.values()):
                self.ownership.reconcile_unknown()
            self.current_attempt_id = None
            self._require("renderer_search")
            self._ready(min(20, remaining()))
            self.handles.clear()
            self.handle_runtime_generation = self._generation()
            # Re-enter search from a confirmed Prime Home page so an already
            # displayed result set is not mistaken for a fresh search lifecycle.
            from .browsing import _navigate

            _navigate(self, dict(pageType="home", pageId="home"), remaining)
            if self.handle_runtime_generation != self._generation():
                raise StaleResultError("runtime changed while returning Home")
            start = self.backend.cursor
            receipt = self.backend.command(
                "search", query=query, timeout=min(30, remaining())
            )
            sid = receipt["id"]

            def loaded():
                events = [
                    r["data"]
                    for r in self.backend.records(start)
                    if r["kind"] == "search_query_evidence"
                    and r["data"].get("searchId") == sid
                ]
                return events[-1] if events and events[-1].get("loaded") else None

            evidence = self.backend.wait(loaded, remaining())
            generation = evidence["generation"]
            # Impressions are batched/delayed in the observed build. This is a
            # bounded collection window, not a claim that the catalogue is complete.
            collected = time.monotonic()
            self.backend.wait(lambda: time.monotonic() - collected >= 35, remaining())
            if not loaded():
                raise StaleResultError("search generation changed while collecting")
            self._require("renderer_search")
            self._ready(min(20, remaining()))
            scene_start = self.backend.cursor
            self.backend.command("inspect-native", timeout=min(30, remaining()))

            def scene():
                values = [
                    r
                    for r in self.backend.records(scene_start)
                    if r["kind"] == "native_query"
                    and r["data"].get("method") == "inspectPage"
                ]
                return values[-1] if values else None

            snapshot = self.backend.wait(scene, remaining())
            d = snapshot["data"]
            current = loaded()
            if (
                not d.get("success")
                or d.get("pageChanged")
                or not current
                or current["generation"] != generation
            ):
                raise StaleResultError(
                    "search page or generation changed during inspection"
                )
            if self.handle_runtime_generation != self._generation():
                raise StaleResultError(
                    "runtime changed during search; results discarded"
                )
            result = search_results(
                self.backend.records(start),
                query,
                self.backend.session_id,
                sid,
                generation,
            )
            if resolve_access:
                from .resolution import enrich

                result = enrich(self, result, remaining)
                if self.handle_runtime_generation != self._generation():
                    raise StaleResultError("runtime changed during resolution")
            now = time.monotonic()
            self.handles = {tile.handle: (tile, now) for tile in result.tiles}
            return result.to_dict()

    def resolve(self, content_id=None, *, handle=None, timeout=45, ownership=None):
        """Resolve a fresh handle or GTI without navigating or starting playback."""
        from .resolution import resolve

        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 5 <= timeout <= 60:
            raise ValueError("resolve timeout must be 5..60 seconds")
        with self.operation(ownership):
            self._require("resolve")
            deadline = time.monotonic() + timeout
            if self._active_attempts():
                raise BusyError("finish playback verification before resolving another title")
            if (content_id is None) == (handle is None):
                raise ValueError("provide content_id or handle, not both")
            if handle is not None:
                if not isinstance(handle, str):
                    raise ValueError("handle must be a string")
                entry = self.handles.get(handle)
                if (self.handle_runtime_generation != self._generation()
                        or not entry or time.monotonic() - entry[1] > 120):
                    raise StaleResultError("result handle expired; search again")
                content_id = entry[0].content_id
            if not isinstance(content_id, str) or not re.fullmatch(r"amzn1\.dv\.gti\.[A-Za-z0-9-]+", content_id):
                raise ValueError("resolve requires a correlated GTI")
            self._ready(min(20, timeout))
            if handle is not None and (
                self.handle_runtime_generation != self._generation()
                or time.monotonic() - entry[1] > 120
            ):
                raise StaleResultError("result handle expired while waiting for readiness")
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("resolve deadline exceeded before request")
            return resolve(self, content_id, left).to_dict()

    def play(
        self, content_id=None, *, handle=None, mode="live", attempt_id, ownership=None
    ):
        if not isinstance(attempt_id, str) or not re.fullmatch(
            r"[a-f0-9]{32}", attempt_id
        ):
            raise ValueError("invalid attempt ID")
        if mode not in ("live", "replay"):
            raise ValueError("mode must be live or replay")
        with self.operation(ownership):
            if attempt_id in self.attempts:
                old = self.attempts[attempt_id]
                if old["request"] != {
                    "content_id": content_id,
                    "handle": handle,
                    "mode": mode,
                }:
                    raise ValueError("attempt ID reused with different request")
                return self._public(old)
            if len(self.attempts) >= 256:
                raise PrimeError(
                    "session attempt limit reached; explicitly restart service when idle"
                )
            if self._active_attempts():
                raise BusyError("a playback attempt is still active")
            self._require("play")
            self._ready()
            if (content_id is None) == (handle is None):
                raise ValueError("provide content_id or handle, not both")
            requested = content_id
            if handle is not None:
                if self.handle_runtime_generation != self._generation():
                    raise StaleResultError(
                        "result handle belongs to an earlier runtime generation"
                    )
                entry = self.handles.get(handle)
                if not entry or time.monotonic() - entry[1] > 120:
                    raise StaleResultError("result handle expired; search again")
                requested = entry[0].content_id
                if requested is None:
                    raise ValueError("result has no sufficiently correlated content ID")
            if not isinstance(requested, str) or not re.fullmatch(
                r"amzn1\.dv\.gti\.[A-Za-z0-9-]+", requested
            ):
                raise ValueError("play requires a GTI, not a collection ID")
            if any(a["state"] == "unknown" for a in self.attempts.values()):
                self.ownership.reconcile_unknown()
            a = {
                "attempt_id": attempt_id,
                "request": {"content_id": content_id, "handle": handle, "mode": mode},
                "requested_id": requested,
                "resolved_id": None,
                "state": "resolving",
                "reason": None,
                "start": self.backend.cursor,
                "deadline": time.monotonic() + 180,
                "samples": [],
                "playing_at": None,
                "arrival": None,
                "command_id": attempt_id,
                "last_sample": 0,
                "evidence": {},
            }
            a["session_id"] = self.backend.session_id
            self.attempts[attempt_id] = a
            try:
                receipt = self.backend.command(
                    "play-live" if mode == "live" else "play-content",
                    titleId=requested,
                    attemptId=attempt_id,
                    command_id=attempt_id,
                    expectedPlaybackClass=(
                        "live_watch_now" if mode == "live" else "on_demand"
                    ),
                    timeout=30,
                )
                summary = receipt.get("result", {}).get("summary", {})
                a["resolved_id"] = summary.get("resolvedTitleId")
                a["command_id"] = receipt["id"]
                if not a["resolved_id"] or not receipt.get("result", {}).get(
                    "launchInvoked"
                ):
                    raise OutcomeUnknownError("launch acknowledgement incomplete")
                self.current_attempt_id = attempt_id
                self.handles.clear()
                a["state"] = "launch_requested"
                a["evidence"]["resolution"] = {
                    k: summary.get(k)
                    for k in (
                        "requestedTitleId",
                        "resolvedTitleId",
                        "playbackClass",
                        "freshRequestStartedMs",
                    )
                }
                if summary.get("playbackTransition"):
                    a["evidence"]["playback_transition"] = summary["playbackTransition"]
            except CancelledError:
                # A handoff can race the launch acknowledgement; do not claim
                # that the previous playback is still current.
                self.current_attempt_id = attempt_id
                self.handles.clear()
                a.update(state="cancelled", reason="ownership revoked")
                raise
            except PlaybackRefusedError as exc:
                a.update(state="failed", reason=str(exc))
                a["evidence"]["launch"] = {"disposition": "not_invoked", "reason_code": "launch_refused"}
            except PrimeError as exc:
                # Delivery or navigation may have occurred. Do not pretend the
                # previous attempt remains current or automatically retry.
                self.current_attempt_id = attempt_id
                self.handles.clear()
                a["evidence"]["launch"] = {"disposition": "unknown"}
                a["state"] = "unknown"
                a["reason"] = str(exc)
            return self._public(a)

    def _public(self, a):
        return {
            k: (a.get(k, self.backend.session_id) if k == "session_id" else a[k])
            for k in (
                "attempt_id",
                "session_id",
                "state",
                "requested_id",
                "resolved_id",
                "reason",
                "evidence",
            )
        }

    def _observe_attempt(self, a):
        rows = self.backend.records(a["start"])
        arrival = None
        playing_at = None
        launches = [
            r
            for r in rows
            if r["kind"] in ("content_resolution", "live_resolution")
            and r["data"].get("id") == a["command_id"]
            and r["data"].get("phase") == "launch_invoked"
        ]
        if not launches:
            return
        if a.get("resolved_id") is None:
            a["resolved_id"] = (
                launches[-1]["data"].get("summary", {}).get("resolvedTitleId")
            )
        launch = launches[-1]
        invoking = [
            r for r in rows
            if r["kind"] in ("content_resolution", "live_resolution")
            and r["data"].get("id") == a["command_id"]
            and r["data"].get("phase") == "launch_invoking"
            and r["sequence"] < launch["sequence"]
        ]
        boundary = invoking[-1] if invoking else launch
        rows = [r for r in rows if r["sequence"] > boundary["sequence"]]
        for r in rows:
            d = r.get("data", {})
            if r["kind"] != "control_app_event":
                continue
            name = d.get("name", "")
            p = d.get("params", {}).get("value", {})
            if name == "NAVIGATE_TO_PAGE":
                pp = p.get("pageParams", {})
                if arrival is None and (
                    p.get("pageType") == "RUST_PLAYBACK"
                    and pp.get("titleId") == a["resolved_id"]
                ):
                    arrival = r["sequence"]
                    a["playback_epoch"] = d.get("playbackEpoch")
                elif arrival is not None or p.get("pageType") == "RUST_PLAYBACK":
                    a.update(
                        state="failed", reason="navigation left the requested playback"
                    )
                    return
            if arrival is not None:
                if name.startswith("PLAYBACK_FATAL_"):
                    a.update(state="failed", reason=name)
                    return
                if name == "PLAYBACK_STATE_CHANGE":
                    if p.get("state") == "Playing":
                        if playing_at is None:
                            a["playback_epoch"] = d.get("playbackEpoch")
                        playing_at = playing_at or r["timeMs"]
                    elif playing_at is not None:
                        a.update(
                            state="failed",
                            reason="playback left Playing during verification",
                        )
                        return
        a["arrival"] = arrival
        a["playing_at"] = playing_at

    def playback_status(self, attempt_id, timeout=50):
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 5 <= timeout <= 60
        ):
            raise ValueError("status timeout must be 5..60 seconds")
        with self.operation(readonly=True):
            if attempt_id not in self.attempts:
                raise ValueError("unknown attempt ID for this service session")
            a = self.attempts[attempt_id]
            if a.get("playback_epoch") is None:
                try:
                    observed = dict(a)
                    self._observe_attempt(observed)
                    if observed.get("playback_epoch") is not None:
                        a["playback_epoch"] = observed["playback_epoch"]
                except PrimeError:
                    pass
            return observe_playback(self.backend, a, timeout)

    def attempt(self, attempt_id):
        with self.operation(readonly=True):
            if attempt_id not in self.attempts:
                raise ValueError("unknown attempt ID for this service session")
            a = self.attempts[attempt_id]
            if a["state"] in ("playing", "failed", "unknown", "cancelled", "stopped"):
                return self._public(a)
            try:
                inspection = self.backend.health().get("playback_inspection", {})
                if inspection.get("state") == "degraded" and inspection.get("recovery") == "retry_after_cooldown":
                    a["evidence"]["playback_inspection"] = inspection
                    return self._public(a)
                if inspection.get("state") == "degraded":
                    a.update(state="unknown", reason="playback inspection degraded; restart player session")
                    a["evidence"]["playback_inspection"] = inspection
                    return self._public(a)
                self._observe_attempt(a)
                if a["state"] == "failed":
                    return self._public(a)
                if time.monotonic() > a["deadline"]:
                    a.update(state="unknown", reason="verification deadline exceeded")
                    return self._public(a)
                if self.suspended:
                    a.update(
                        state="unknown", reason="manual control suspended verification"
                    )
                    return self._public(a)
                state = self.backend.health()["state"]
                page = state.get("page", {})
                if (
                    a["arrival"] is None
                    or not state.get("ready")
                    or page.get("pageType") != "RUST_PLAYBACK"
                    or page.get("pageId") != a["resolved_id"]
                ):
                    return self._public(a)
                if time.monotonic() - a["last_sample"] < 20:
                    return self._public(a)
                if a["playing_at"] is None:
                    # Some launches keep an already Playing native stream and
                    # emit only navigation. Never borrow its old Playing event.
                    # Use session-fenced native progress, retaining the normal
                    # sustained verification window before accepting startup.
                    status = observe_playback(self.backend, a, timeout=50)
                    a["evidence"]["startup_observation"] = status
                    a["last_sample"] = time.monotonic()
                    if status["state"] == "playing" and status["matches_attempt"]:
                        samples = status["evidence"]["samples"]
                        first = a.setdefault("progress_started", samples[0])
                        last = samples[-1]
                        a["state"] = "observing"
                        if (
                            last["received_at_ms"] - first["received_at_ms"] >= 20000
                            and last["position"] > first["position"]
                        ):
                            a["state"] = "playing"
                            a["evidence"]["verification_basis"] = "native_position_progress"
                            a["evidence"]["player_samples"] = [
                                {
                                    "title_id": sample["content_id"],
                                    "position_raw": sample["position"],
                                    "received_at_ms": sample["received_at_ms"],
                                }
                                for sample in (first, last)
                            ]
                    else:
                        a.pop("progress_started", None)
                        a["state"] = "launch_requested"
                    return self._public(a)
                start = self.backend.cursor
                self.backend.command("inspect-player", timeout=20)

                def sample():
                    values = [
                        r
                        for r in self.backend.records(start)
                        if r["kind"] == "native_query"
                        and r["data"].get("plugin") == "app-player-plugin"
                    ]
                    bymethod = {r["data"].get("method"): r for r in values}
                    return (
                        bymethod
                        if all(
                            k in bymethod for k in ("getTitleId", "getCurrentPosition")
                        )
                        else None
                    )

                got = self.backend.wait(sample, 30)
                title = got["getTitleId"]
                pos = got["getCurrentPosition"]
                if any(
                    not r["data"].get("success")
                    or r["data"].get("pageChanged")
                    or r["data"].get("pageAtReceipt", {}).get("pageId")
                    != a["resolved_id"]
                    for r in (title, pos)
                ):
                    raise PrimeError(
                        "native player sample failed or belongs to another page"
                    )
                if title["data"]["result"] != a["resolved_id"]:
                    raise PrimeError(
                        "native player title differs from requested playback"
                    )
                position = pos["data"]["result"]
                if type(position) not in (int, float) or not math.isfinite(position):
                    raise PrimeError("unknown native position response shape")
                sample_time = pos["data"]["receivedTimeMs"]
                a["last_sample"] = time.monotonic()
                a["samples"].append(
                    {
                        "title_id": title["data"]["result"],
                        "position_raw": position,
                        "received_at_ms": sample_time,
                    }
                )
                a["state"] = "observing"
                self._observe_attempt(a)
                if a["state"] == "failed":
                    return self._public(a)
                if len(a["samples"]) >= 2:
                    first, last = a["samples"][0], a["samples"][-1]
                    if last["position_raw"] <= first["position_raw"]:
                        raise PrimeError("native playback position did not advance")
                    if (
                        last["received_at_ms"] - first["received_at_ms"] >= 15000
                        and sample_time - a["playing_at"] >= 20000
                    ):
                        a["state"] = "playing"
                        a["evidence"]["player_samples"] = a["samples"]
                        a["evidence"]["playing_duration_ms"] = (
                            sample_time - a["playing_at"]
                        )
            except PrimeError as exc:
                a.update(state="unknown", reason=str(exc))
            except TimeoutError as exc:
                a.update(state="unknown", reason=str(exc))
            return self._public(a)

