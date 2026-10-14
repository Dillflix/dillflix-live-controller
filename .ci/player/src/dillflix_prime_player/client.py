import asyncio
import http.client
import json
import socket
import time
import uuid
from . import errors
from .catalog import SearchResults, SearchItem
from .models import (
    BrowseResults,
    SearchTile,
    PlaybackOutcome,
    PlaybackStatus,
    PageInfo,
    PagesResults,
    ContentResolution,
)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class PlaybackAttempt:
    def __init__(self, client, value):
        self.client = client
        self.id = value["attempt_id"]
        self.initial = PlaybackOutcome(**value)

    async def status(self, *, timeout=60):
        """Historical launch verification; use playback_status for current playback."""
        return PlaybackOutcome(
            **await self.client._request(
                "attempt", attempt_id=self.id, _timeout=timeout
            )
        )

    async def playback_status(self, *, timeout=50):
        return await self.client.playback_status(attempt_id=self.id, timeout=timeout)

    async def cancel(self):
        return await self.client.cancel(
            attempt_id=self.id, session_id=self.initial.session_id
        )

    async def stop(self):
        return await self.client.stop(
            attempt_id=self.id, session_id=self.initial.session_id
        )

    async def wait_for_playing(self, timeout=150):
        deadline = time.monotonic() + timeout
        outcome = self.initial
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return PlaybackOutcome(
                    self.id,
                    "unknown",
                    outcome.requested_id,
                    outcome.resolved_id,
                    "client wait deadline exceeded; service attempt remains inspectable",
                )
            try:
                outcome = await self.status(timeout=min(60, remaining))
            except errors.OutcomeUnknownError:
                return PlaybackOutcome(
                    self.id,
                    "unknown",
                    outcome.requested_id,
                    outcome.resolved_id,
                    "service status unavailable; query this attempt ID before making another play request",
                )
            if outcome.state in (
                "playing",
                "failed",
                "unknown",
                "cancelled",
                "stopped",
            ):
                return outcome
            if time.monotonic() >= deadline:
                return PlaybackOutcome(
                    self.id,
                    "unknown",
                    outcome.requested_id,
                    outcome.resolved_id,
                    "client wait deadline exceeded; service attempt remains inspectable",
                )
            await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))


class PrimePlayer:
    """Async client of an existing persistent controller service."""

    def __init__(self, socket_path):
        self.socket_path = str(socket_path)
        self.owner_id = uuid.uuid4().hex
        self.handoff = None
        self.session_id = None

    def _request_sync(self, method, params, timeout):
        connection = UnixConnection(self.socket_path, timeout)
        try:
            try:
                connection.connect()
            except OSError as exc:
                raise errors.UnavailableError(
                    f"service unavailable at {self.socket_path}; request was not sent"
                ) from exc
            connection.request(
                "POST",
                "/rpc",
                json.dumps({"method": method, "params": params}),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            wire = response.read(8 * 1024 * 1024 + 1)
            if len(wire) > 8 * 1024 * 1024:
                raise errors.OutcomeUnknownError("service response exceeded size limit")
            value = json.loads(wire)
            if "error" in value:
                e = value["error"]
                cls = getattr(errors, e["type"], errors.PrimeError)
                if not isinstance(cls, type) or not issubclass(cls, errors.PrimeError):
                    cls = errors.PrimeError
                raise cls(e["message"])
            return value["result"]
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            raise errors.OutcomeUnknownError(
                "service connection failed; operation may still be running; do not replay automatically"
            ) from exc
        finally:
            connection.close()

    async def _request(self, method, *, _timeout=60, **params):
        if method in ("broadcasts", "search", "pages", "discover", "resolve", "inspect_page", "play", "cancel", "stop",
                      "vpn_connect", "vpn_disconnect", "vpn_cancel"):
            current = await self.health()
            params.setdefault("ownership", current["ownership"])
            if method in {"broadcasts", "search", "pages", "discover", "resolve", "play"}:
                if current.get("network", {}).get("enabled"):
                    params.setdefault("network_context_id", current["network"].get("context_id"))
                elif params.get("network_context_id") is None:
                    params.pop("network_context_id", None)
        return await asyncio.to_thread(self._request_sync, method, params, _timeout)

    async def compatibility(self):
        """Per-capability availability, reasons and observed resource identities."""
        return await self._request("compatibility")

    async def vpn_locations(self):
        return await self._request("vpn_locations")

    async def vpn_status(self, *, operation_id=None):
        return await self._request("vpn_status", operation_id=operation_id)

    async def vpn_connect(self, target_id, *, operation_id, deadline_at, interrupt_playback=False, qualify=False):
        """Submit one durable managed transition; retain operation_id before calling.

        Read vpn_status(operation_id=...) after lost replies. Does not wait or replay.
        """
        return await self._request("vpn_connect", target_id=target_id, operation_id=operation_id,
                                   deadline_at=deadline_at, interrupt_playback=interrupt_playback, qualify=qualify)

    async def vpn_disconnect(self, *, operation_id, deadline_at, interrupt_playback=False):
        return await self.vpn_connect("direct", operation_id=operation_id, deadline_at=deadline_at,
                                      interrupt_playback=interrupt_playback)

    async def vpn_cancel(self, operation_id):
        return await self._request("vpn_cancel", operation_id=operation_id)

    async def health(self):
        value = await self._request("health")
        if self.session_id is None:
            self.session_id = value.get("session_id") or value.get("ownership", {}).get(
                "session_id"
            )
        return value

    async def search(self, query, timeout=45, *, network_context_id=None):
        return SearchResults.from_dict(
            await self._request(
                "search", query=query, timeout=timeout, _timeout=timeout + 5,
                **({"network_context_id": network_context_id} if network_context_id else {})
            )
        )

    async def pages(self, *, timeout=60, network_context_id=None):
        """Discover current native tabs, including each individual channel tab."""
        return PagesResults.from_dict(
            await self._request("pages", timeout=timeout, _timeout=timeout + 5,
                                **({"network_context_id": network_context_id} if network_context_id else {}))
        )

    async def discover(
        self, pages, limit_per_page=20, *, max_rows=2, max_steps=16, timeout=400, resolve_access=True,
        network_context_id=None
    ):
        """Collect a bounded prefix from each page in one usable result scope."""
        if not isinstance(pages, (list, tuple)):
            raise ValueError("pages must be a list of PageInfo objects, IDs or names")
        return BrowseResults.from_dict(
            await self._request(
                "discover",
                pages=[p.id if isinstance(p, PageInfo) else p for p in pages],
                limit_per_page=limit_per_page,
                max_rows=max_rows,
                max_steps=max_steps,
                resolve_access=resolve_access,
                timeout=timeout,
                _timeout=timeout + 5,
                **({"network_context_id": network_context_id} if network_context_id else {}),
            )
        )

    async def broadcasts(self, content_id, *, timeout=45, network_context_id=None):
        """Return raw live-details broadcast evidence without navigation.

        Complete means the returned response was preserved, not that pagination
        is exhausted or every available variant has been enumerated.
        """
        return await self._request("broadcasts", content_id=content_id, timeout=timeout,
                                   _timeout=timeout + 5,
                                   **({"network_context_id": network_context_id} if network_context_id else {}))

    async def resolve(self, content, *, timeout=45, network_context_id=None):
        """Return action and entitlement for a tile or GTI without playback."""
        params = ({"handle": content.handle} if isinstance(content, SearchTile)
                  else {"content_id": content.content_id if isinstance(content, SearchItem) else content})
        return ContentResolution(**await self._request(
            "resolve", **params, timeout=timeout, _timeout=timeout + 5,
            **({"network_context_id": network_context_id} if network_context_id else {})
        ))

    async def inspect_page(self, *, timeout=30):
        """Capture the current native scene without navigation, including in manual mode."""
        return await self._request("inspect_page", timeout=timeout, _timeout=timeout + 5)

    async def resolution_evidence(self, request_id, *, session_id=None):
        """Read bounded retained evidence for one resolution; no runtime command."""
        return await self._request("resolution_evidence", request_id=request_id,
                                   session_id=session_id or self.session_id)

    async def play(self, content, *, mode="live", attempt_id=None, network_context_id=None):
        """Resolve fresh and request playback; use wait_for_playing to verify it.

        Retain attempt_id to query uncertain delivery without submitting again.
        Replay is explicit; live requests never fall back to start-over.
        """
        attempt_id = attempt_id or uuid.uuid4().hex
        params = (
            {"handle": content.handle}
            if isinstance(content, SearchTile)
            else {"content_id": content.content_id if isinstance(content, SearchItem) else content}
        )
        try:
            value = await self._request(
                "play", **params, mode=mode, attempt_id=attempt_id,
                **({"network_context_id": network_context_id} if network_context_id else {})
            )
        except errors.PrimeError as exc:
            exc.attempt_id = attempt_id
            raise
        return PlaybackAttempt(self, value)

    async def attempt(self, attempt_id):
        return PlaybackAttempt(
            self, await self._request("attempt", attempt_id=attempt_id)
        )

    async def suspend(self, *, owner_id=None, previous=None):
        current = await self.health()
        self.handoff = await self._request(
            "suspend",
            value=True,
            owner_id=owner_id or self.owner_id,
            previous=previous or current["ownership"],
        )
        return self.handoff

    async def cancel(self, *, attempt_id=None, session_id=None):
        if self.session_id is None:
            await self.health()
        return await self._request(
            "cancel", attempt_id=attempt_id, session_id=session_id or self.session_id
        )

    async def stop(self, *, attempt_id, session_id):
        return await self._request(
            "stop", attempt_id=attempt_id, session_id=session_id, _timeout=90
        )

    async def manual_input(self, *, receipt, seq, key=None, text=None):
        return await self._request(
            "manual_input", receipt=receipt, seq=seq, key=key, text=text, _timeout=15
        )

    async def playback_status(self, *, attempt_id, timeout=50):
        return PlaybackStatus(
            **await self._request(
                "playback_status",
                attempt_id=attempt_id,
                timeout=timeout,
                _timeout=timeout + 5,
            )
        )

    async def resume(self, *, receipt=None):
        previous = receipt or self.handoff
        if previous is None:
            raise errors.StaleResultError(
                "resume requires the acknowledged manual handoff receipt"
            )
        result = await self._request("suspend", value=False, previous=previous)
        self.handoff = None
        return result

    async def close(self):
        """Client has no persistent socket. Device session continues in service."""

    async def __aenter__(self):
        await self.health()
        return self

    async def __aexit__(self, *args):
        await self.close()
