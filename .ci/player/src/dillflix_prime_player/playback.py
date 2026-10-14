"""Fresh, bounded playback observations; never mutates navigation or playback."""

from datetime import datetime, timezone
import math
import time

from .errors import PrimeError


def observe_playback(backend, attempt, timeout=50):
    deadline = time.monotonic() + timeout
    result = dict(
        attempt_id=attempt["attempt_id"],
        session_id=backend.session_id,
        requested_id=attempt["requested_id"],
        resolved_id=attempt["resolved_id"],
        state="unknown",
        is_playing=None,
        matches_attempt=None,
        current_content_id=None,
        position=None,
        position_unit="native",
        observed_at=None,
        observation_age_seconds=None,
        state_event_age_seconds=None,
        error=None,
        evidence={},
    )

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("playback status deadline exceeded")
        return left

    def checkpoint():
        value = backend.command("playback-status", timeout=min(15, remaining()))[
            "result"
        ]
        if not isinstance(value.get("epoch"), int) or not isinstance(
            value.get("timeMs"), (int, float)
        ):
            raise PrimeError("unsupported playback checkpoint shape")
        result["observed_at"] = datetime.now(timezone.utc).isoformat()
        result["observation_age_seconds"] = 0.0
        result["evidence"]["checkpoint"] = value
        return value

    def same_session(value, original):
        if value["epoch"] != original["epoch"] or value.get("page") != original.get(
            "page"
        ):
            raise PrimeError("playback changed during inspection")

    def event_state(event, epoch, content_id):
        if event is None:
            return "unobserved"
        if event.get("epoch") != epoch or event.get("pageId") != content_id:
            raise PrimeError("state event belongs to another playback session")
        if (
            event.get("name", "").startswith("PLAYBACK_FATAL_")
            or event.get("name") == "PLAYBACK_EXPECTED_FATAL"
        ):
            return "error"
        return {
            "Playing": "playing", "Paused": "paused", "Buffering": "buffering",
            "Stopped": "stopped", "Ended": "ended",
        }.get(event.get("rawState"), "unknown")

    def sample():
        start = backend.cursor
        receipt = backend.command("inspect-player", timeout=min(15, remaining()))

        def received():
            rows = [
                r
                for r in backend.records(start)
                if r["kind"] == "native_query"
                and r["data"].get("plugin") == "app-player-plugin"
                and r["data"].get("inspectionId") == receipt["id"]
            ]
            methods = {r["data"].get("method"): r["data"] for r in rows}
            return (
                methods
                if all(k in methods for k in ("getTitleId", "getCurrentPosition"))
                else None
            )

        methods = backend.wait(received, remaining())
        title, pos = methods["getTitleId"], methods["getCurrentPosition"]
        for value in (title, pos):
            if (
                not value.get("success")
                or value.get("pageChanged")
                or value.get("pageAtRequest") != value.get("pageAtReceipt")
            ):
                raise PrimeError("native player query failed or page changed")
            if value.get("pageAtReceipt", {}).get("pageType") != "RUST_PLAYBACK":
                raise PrimeError("native player sample is not on playback page")
        ident, position = title.get("result"), pos.get("result")
        if not isinstance(ident, str) or not ident:
            raise PrimeError("native player identity unavailable")
        if type(position) not in (int, float) or not math.isfinite(position):
            raise PrimeError("native player position unavailable")
        stamp = pos.get("receivedTimeMs")
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            raise PrimeError("native player timestamp unavailable")
        return dict(
            content_id=ident,
            position=position,
            received_at_ms=stamp,
            page=pos["pageAtReceipt"],
            inspection_id=receipt["id"],
        )

    try:
        initial = checkpoint()
        epoch = attempt.get("playback_epoch")
        if not attempt["resolved_id"] or epoch is None:
            raise PrimeError(
                "attempt has no bound playback session; verify the play attempt first"
            )
        if initial.get("page", {}).get("pageType") != "RUST_PLAYBACK":
            result.update(state="not_current", is_playing=False, matches_attempt=False)
            return result
        first = sample()
        final = checkpoint()
        same_session(final, initial)
        result.update(
            current_content_id=first["content_id"], position=first["position"]
        )
        result["evidence"]["samples"] = [first]
        if first["content_id"] != attempt["resolved_id"] or final["epoch"] != epoch:
            result.update(state="not_current", is_playing=False, matches_attempt=False)
            return result
        if (
            first["page"] != final["page"]
            or first["page"].get("pageId") != first["content_id"]
        ):
            raise PrimeError("native identity and active playback page disagree")
        result["matches_attempt"] = True
        event = final.get("event")
        # Explicit state events are retained in the app, not reconstructed from
        # a host journal that may have rolled over during hours of playback.
        state = event_state(event, epoch, first["content_id"])
        if state in ("playing", "unobserved"):
            # Playing may precede navigation and never repeat. Prove progress
            # with fresh native samples in the bound session in either case.
            started = time.monotonic()
            backend.wait(lambda: time.monotonic() - started >= 1, remaining())
            second = sample()
            final = checkpoint()
            same_session(final, initial)
            if (
                second["content_id"] != first["content_id"]
                or second["page"] != first["page"]
            ):
                raise PrimeError("native playback identity changed between samples")
            result["evidence"]["samples"].append(second)
            result.update(position=second["position"])
            event = final.get("event")
            state = event_state(event, epoch, first["content_id"])
            if state in ("playing", "unobserved") and (
                second["received_at_ms"] <= first["received_at_ms"]
                or second["position"] <= first["position"]
            ):
                raise PrimeError(
                    "position did not advance; pause, buffering or completion cannot be inferred"
                )
            if state in ("playing", "unobserved"):
                result["evidence"]["verification_basis"] = (
                    "native_position_progress" if event is None
                    else "playing_event_and_native_position_progress"
                )
                state = "playing"
        last = result["evidence"]["samples"][-1]
        result["observation_age_seconds"] = max(
            0, (final["timeMs"] - last["received_at_ms"]) / 1000
        )
        if event is not None:
            result["state_event_age_seconds"] = max(
                0, (final["timeMs"] - event["timeMs"]) / 1000
            )
        if result["observation_age_seconds"] > 10:
            raise PrimeError("native sample is stale")
        result.update(
            state=state,
            is_playing=(
                True if state == "playing" else None if state == "unknown" else False
            ),
        )
        if state == "unknown":
            result["error"] = "unrecognized playback state event"
        elif state == "error":
            result["error"] = event["name"]
    except (PrimeError, TimeoutError, KeyError, TypeError, ValueError) as exc:
        result.update(
            state="unknown", is_playing=None, matches_attempt=None, error=str(exc)
        )
    return result
