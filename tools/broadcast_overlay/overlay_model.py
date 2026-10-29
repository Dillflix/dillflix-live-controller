"""Bounded public overlay leases, fetched away from the video/main threads."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import unicodedata
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

HEADLINES = {
    "waiting": "Waiting for live sports",
    "switching": "Switching live coverage",
    "recovering": "Reconnecting live coverage",
    "standby": "Live coverage on standby",
}
MAX_RESPONSE = 16384
MAX_LEASE = 5.0
LOG = logging.getLogger("dillflix.broadcast_overlay")


@dataclass(frozen=True)
class OverlayState:
    name: str = "Dillflix"
    state: str = "hidden"
    headline: str = ""
    event_title: str | None = None
    visible: bool = False


def clean_text(value: str, limit: int) -> str:
    """Treat names as plain text, including removing control/bidi directives."""
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in value)
    return " ".join(text.split())[:limit]


def timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise ValueError("Missing lease timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Lease timestamp requires timezone")
    result = parsed.timestamp()
    if not math.isfinite(result):
        raise ValueError("Invalid lease timestamp")
    return result


def decode_payload(payload, device_id: str, *, wall_time: float):
    """Return state, generation and remaining lifetime; no remote operational copy."""
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ValueError("Unsupported overlay response")
    if payload.get("device_id") != device_id:
        raise ValueError("Overlay device does not match this feed")
    generated = timestamp(payload.get("generated_at"))
    valid_until = timestamp(payload.get("valid_until"))
    # Do not prolong a lease using a delayed/cached response or a remote clock
    # substantially ahead of ours. Hosts should use normal time synchronization.
    if generated > wall_time + 1 or not 0 < valid_until - generated <= MAX_LEASE + .01:
        raise ValueError("Invalid overlay lease")
    lifetime = min(MAX_LEASE, valid_until - wall_time)
    if lifetime <= 0:
        raise ValueError("Expired overlay lease")
    state, visible = payload.get("state"), payload.get("visible")
    if type(visible) is not bool or state not in {"hidden", *HEADLINES} or visible != (state != "hidden"):
        raise ValueError("Invalid overlay state")
    name, title = payload.get("device_name"), payload.get("event_title")
    if not isinstance(name, str) or not clean_text(name, 48):
        raise ValueError("Missing public stream name")
    if title is not None and not isinstance(title, str):
        raise ValueError("Invalid event title")
    return OverlayState(
        name=clean_text(name, 48), state=state, visible=visible,
        headline=HEADLINES.get(state, ""),
        event_title=clean_text(title, 300) if title and state in {"switching", "recovering"} else None,
    ), generated, lifetime


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an installation's optional authentication headers.
        return None


class OverlayPoller:
    def __init__(self, url, device_id, on_update, headers_file=None, name="Dillflix"):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.fragment):
            raise ValueError("Overlay URL must be HTTP(S), without credentials or fragment")
        if not device_id:
            raise ValueError("Overlay device ID is required")
        self.url, self.device_id, self.on_update = url, device_id, on_update
        self.hidden = OverlayState(name=clean_text(name, 48) or "Dillflix")
        self._state, self._expires, self._generation = self.hidden, 0.0, float("-inf")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._last_warning = float("-inf")
        headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
        if headers_file:
            raw = Path(headers_file).read_bytes()
            if len(raw) > MAX_RESPONSE:
                raise ValueError("Overlay headers file is too large")
            extra = json.loads(raw)
            if (not isinstance(extra, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) or not k
                or any(ord(c) < 32 or ord(c) > 126 for c in k)
                or ":" in k or any(c in v for c in "\r\n") for k, v in extra.items()
            )):
                raise ValueError("Invalid overlay headers file")
            headers.update(extra)
        self._request = urllib.request.Request(url, headers=headers)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def current_state(self):
        # Never wait for HTTP. The runner calls this independently every 250ms.
        with self._lock:
            return self._state if time.monotonic() < self._expires else self.hidden

    def accept(self, payload):
        state, generated, lifetime = decode_payload(payload, self.device_id, wall_time=time.time())
        expires = time.monotonic() + lifetime
        with self._lock:
            if generated < self._generation:
                raise ValueError("Out-of-order overlay response")
            if generated == self._generation:
                # Identical timestamps never extend a lease, even if local clock
                # changes or a caching intermediary repeats a response.
                expires = min(expires, self._expires)
            self._state, self._expires, self._generation = state, expires, generated
        self.on_update(state)

    def poll_once(self):
        with self._opener.open(self._request, timeout=1.5) as response:
            if response.headers.get_content_type() != "application/json":
                raise ValueError("Unexpected overlay response type")
            data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ValueError("Overlay response too large")
        self.accept(json.loads(data))

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception as exc:
                if started - self._last_warning >= 30:
                    # Deliberately omit URL, response body, headers and exception
                    # message: these may contain deployment credentials.
                    LOG.warning("Overlay update unavailable (%s); current lease will expire", type(exc).__name__)
                    self._last_warning = started
            self._stop.wait(max(.05, 1.0 - (time.monotonic() - started)))

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="broadcast-overlay", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=.2)
