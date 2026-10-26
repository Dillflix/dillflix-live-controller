"""Read-only browser access to the live stack; never controls a source device."""

import asyncio
import json
import re
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

PUBLIC_ROOT = "/api/public/v1/streams"
UPSTREAM_ROOT = "/api/browser/v1"
STREAM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,160}\.ts\Z")
RANGE = re.compile(r"bytes=(?:[0-9]{1,12}-[0-9]{0,12}|-[0-9]{1,12})\Z")
STATUSES = {"live", "offline", "unknown"}
MAX_JSON = 256 * 1024
MAX_PLAYLIST = 64 * 1024
MAX_SEGMENT = 32 * 1024 * 1024
MAX_CONNECTIONS = 32


def validate_configuration(url, key):
    if not url and not key:
        return
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and not parsed.username and not parsed.password
                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("LIVE_BROWSER_API_URL must be an HTTP(S) origin without credentials or a path")
    if not key or len(key) > 1024 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("LIVE_BROWSER_API_KEY must be a nonempty printable ASCII service key")


def failure(status=502):
    detail = {
        400: "Invalid live stream request.",
        404: "Live stream or session not found.",
        409: "This live stream is unavailable. Try again shortly.",
        410: "The viewing session expired. Reconnect to watch live.",
        413: "Live stream request is too large.",
        416: "Requested media range is unavailable.",
        429: "Live stream capacity is busy. Try again shortly.",
        503: "Live streaming is unavailable. Try again shortly.",
    }.get(status, "The live streaming service could not complete this request.")
    return HTTPException(status, detail)


def checked_id(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise failure(400)
    return value


def label(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 120 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid label")
    return value


def catalog(value):
    """Project only public metadata, dropping upstream routes and secrets."""
    items = value["streams"]
    if not isinstance(items, list) or len(items) > 256:
        raise ValueError("Invalid catalog")
    result, seen = [], set()
    for item in items:
        stream_id = item["id"]
        if not isinstance(stream_id, str) or not STREAM_ID.fullmatch(stream_id) or stream_id in seen:
            raise ValueError("Invalid stream ID")
        seen.add(stream_id)
        status, default = item["status"], item["default_tier"]
        if status not in STATUSES or type(default) is not int or not 0 <= default <= 5:
            raise ValueError("Invalid stream status/tier")
        tiers, tier_ids = [], set()
        if not isinstance(item["tiers"], list) or not 1 <= len(item["tiers"]) <= 6:
            raise ValueError("Invalid tiers")
        for tier in item["tiers"]:
            number = tier["id"]
            if type(number) is not int or not 0 <= number <= 5 or number in tier_ids:
                raise ValueError("Invalid tier ID")
            if tier["status"] not in STATUSES:
                raise ValueError("Invalid tier status")
            tier_ids.add(number)
            tiers.append({"id": number, "label": label(tier["label"]), "status": tier["status"]})
        if default not in tier_ids:
            raise ValueError("Missing default tier")
        result.append({"id": stream_id, "label": label(item["label"]), "status": status,
                       "default_tier": default, "tiers": tiers})
    return result


def playlist(data):
    """The receiver's HLS contract is a media playlist with relative TS names."""
    value = data.decode("utf-8")
    if not value.startswith("#EXTM3U\n") and not value.startswith("#EXTM3U\r\n"):
        raise ValueError("Not an HLS playlist")
    for line in value.splitlines():
        if not line:
            continue
        if line.startswith("#"):
            # No external keys, maps, alternate renditions or nested manifests.
            if re.search(r"URI\s*=", line, re.I) or line.startswith(("#EXT-X-STREAM-INF", "#EXT-X-DEFINE")):
                raise ValueError("Unsupported playlist reference")
        elif not SEGMENT.fullmatch(line):
            raise ValueError("Nonlocal segment")
    return data


class MediaResponse(StreamingResponse):
    """Close the upstream even if the client disconnects before iteration starts."""

    def __init__(self, response, release, headers):
        self.upstream = response
        self.release = release
        super().__init__(self.chunks(), status_code=response.status_code, headers=headers)

    async def chunks(self):
        count = 0
        async for chunk in self.upstream.aiter_bytes(64 * 1024):
            count += len(chunk)
            if count > MAX_SEGMENT:
                raise RuntimeError("Live segment exceeds size limit")
            yield chunk

    async def __call__(self, scope, receive, send):
        try:
            with anyio.fail_after(45):
                await super().__call__(scope, receive, send)
        finally:
            await self.release(self.upstream)


class LiveStreams:
    def __init__(self, settings, *, transport=None):
        self.enabled = bool(settings.live_browser_api_url)
        self.slots = asyncio.Semaphore(MAX_CONNECTIONS)
        self.client = httpx.AsyncClient(
            base_url=settings.live_browser_api_url.rstrip("/"),
            headers={"X-Dillflix-Live-Key": settings.live_browser_api_key, "Accept-Encoding": "identity"},
            timeout=httpx.Timeout(8, connect=3, pool=0.5),
            limits=httpx.Limits(max_connections=MAX_CONNECTIONS, max_keepalive_connections=16),
            follow_redirects=False, trust_env=False, transport=transport,
        ) if self.enabled else None

    async def close(self):
        if self.client:
            await self.client.aclose()

    async def release(self, response):
        try:
            with anyio.CancelScope(shield=True):
                await response.aclose()
        finally:
            self.slots.release()

    async def open(self, method, path, **kwargs):
        if not self.client:
            raise failure(503)
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=0.5)
        except TimeoutError:
            raise failure(429) from None
        response = None
        try:
            request = self.client.build_request(method, UPSTREAM_ROOT + path, **kwargs)
            response = await self.client.send(request, stream=True)
            if response.status_code not in {200, 201, 204, 206}:
                status = response.status_code if response.status_code in {400, 404, 409, 410, 416, 429, 503} else 502
                raise failure(status)
            if response.headers.get("content-encoding", "identity") != "identity":
                raise failure()
            return response
        except BaseException as exc:
            if response:
                await self.release(response)
            else:
                self.slots.release()
            if isinstance(exc, httpx.HTTPError):
                raise failure(503) from None
            raise

    async def read(self, response, limit):
        data = bytearray()
        async for chunk in response.aiter_bytes(64 * 1024):
            if len(data) + len(chunk) > limit:
                raise failure()
            data.extend(chunk)
        return bytes(data)

    async def json_request(self, method, path, **kwargs):
        response = None
        try:
            async with asyncio.timeout(12):
                response = await self.open(method, path, **kwargs)
                if response.status_code == 204:
                    return None
                return json.loads(await self.read(response, MAX_JSON))
        except (httpx.HTTPError, TimeoutError):
            raise failure(503) from None
        except (ValueError, TypeError, RecursionError):
            raise failure() from None
        finally:
            if response is not None:
                await self.release(response)

    async def streams(self):
        if not self.enabled:
            return {"enabled": False, "streams": []}
        value = await self.json_request("GET", "/streams")
        try:
            return {"enabled": True, "streams": catalog(value)}
        except (ValueError, TypeError, KeyError):
            raise failure() from None

    async def create(self, request):
        data = bytearray()
        try:
            async with asyncio.timeout(8):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > 4096:
                        raise failure(413)
                    data.extend(chunk)
        except TimeoutError:
            raise failure(400) from None
        try:
            value = json.loads(data)
            if not isinstance(value, dict) or set(value) != {"stream_id", "tier"}:
                raise ValueError("Invalid request")
            checked_id(value["stream_id"], STREAM_ID)
            if type(value["tier"]) is not int or not 0 <= value["tier"] <= 5:
                raise ValueError("Invalid tier")
        except (ValueError, TypeError, RecursionError):
            raise failure(400) from None
        result = await self.json_request("POST", "/sessions", json=value)
        try:
            session_id = result["session_id"]
            if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
                raise ValueError("Invalid session")
            expiry = result["expires_in"]
            if type(expiry) is not int or not 1 <= expiry <= 86400:
                raise ValueError("Invalid expiry")
        except (ValueError, TypeError, KeyError):
            raise failure() from None
        return {"session_id": session_id, "expires_in": expiry,
                "playlist_url": f"{PUBLIC_ROOT}/sessions/{session_id}/hls/index.m3u8"}

    async def media(self, request, session_id, filename):
        checked_id(session_id, SESSION_ID)
        is_playlist = filename == "index.m3u8"
        if not is_playlist and not SEGMENT.fullmatch(filename):
            raise failure(404)
        headers = {}
        # Native HLS clients can request bytes=0- on the manifest. Ignore ranges
        # for playlists so their complete, bounded contents can be validated.
        if not is_playlist and request.headers.get("range"):
            if not RANGE.fullmatch(request.headers["range"]):
                raise failure(416)
            headers["Range"] = request.headers["range"]
        response = await self.open(request.method, f"/sessions/{session_id}/hls/{filename}", headers=headers)
        handed_off = False
        try:
            if response.status_code not in ({200} if is_playlist else {200, 206}):
                raise failure()
            limit = MAX_PLAYLIST if is_playlist else MAX_SEGMENT
            length = response.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > limit):
                raise failure()
            outgoing = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                        "Content-Type": "application/vnd.apple.mpegurl" if is_playlist else "video/mp2t"}
            forwarded_headers = ("content-length",) if is_playlist else ("content-length", "content-range", "accept-ranges")
            for name in forwarded_headers:
                if name in response.headers:
                    outgoing[name] = response.headers[name]
            if is_playlist:
                outgoing["Accept-Ranges"] = "none"
            if request.method == "HEAD":
                return Response(status_code=response.status_code, headers=outgoing)
            if is_playlist:
                async with asyncio.timeout(12):
                    data = playlist(await self.read(response, MAX_PLAYLIST))
                return Response(data, status_code=response.status_code, headers=outgoing)
            media_response = MediaResponse(response, self.release, outgoing)
            handed_off = True
            return media_response
        except (ValueError, UnicodeError):
            raise failure() from None
        except (httpx.HTTPError, TimeoutError):
            raise failure(503) from None
        finally:
            if not handed_off:
                await self.release(response)


def install_live_streams_api(app, gateway):
    @app.get(PUBLIC_ROOT)
    async def streams():
        return JSONResponse(await gateway.streams(), headers={"Cache-Control": "no-store"})

    @app.post(PUBLIC_ROOT + "/sessions")
    async def create_session(request: Request):
        return JSONResponse(await gateway.create(request), headers={"Cache-Control": "no-store"})

    @app.delete(PUBLIC_ROOT + "/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str):
        checked_id(session_id, SESSION_ID)
        await gateway.json_request("DELETE", f"/sessions/{session_id}")
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.get(PUBLIC_ROOT + "/sessions/{session_id}/hls/{filename}")
    @app.head(PUBLIC_ROOT + "/sessions/{session_id}/hls/{filename}", include_in_schema=False)
    async def media(request: Request, session_id: str, filename: str):
        return await gateway.media(request, session_id, filename)
