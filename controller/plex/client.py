"""Narrow Plex API boundary. No generic metadata mutations or database access."""

import asyncio
import hashlib
import io
import re
import warnings
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

import httpx
from PIL import Image, ImageChops, ImageStat

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 32_000_000
SLOTS = {"poster": "thumb", "background": "art"}


class PlexError(Exception):
    def __init__(self, message, *, permanent=False):
        super().__init__(message)
        self.permanent = permanent


def base_url(value):
    try:
        p = urlsplit(value.strip())
        if (
            p.scheme not in {"http", "https"}
            or not p.hostname
            or p.username
            or p.password
            or p.query
            or p.fragment
        ):
            raise ValueError()
        _ = p.port
        return value.strip().rstrip("/")
    except ValueError:
        raise ValueError("Use an HTTP(S) Plex server URL without credentials or query parameters") from None


def rating_key(value):
    value = unquote(unquote(value.strip()))
    if value.isascii() and value.isdigit() and int(value) > 0:
        return str(int(value))
    match = re.search(r"/library/metadata/([1-9][0-9]*)(?:[/&#?]|$)", value)
    if match:
        return match[1]
    raise ValueError("Enter a numeric Plex item ID or a Plex item link")


@dataclass(frozen=True)
class Asset:
    data: bytes
    digest: str
    mime: str
    width: int
    height: int


def image_asset(data):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise PlexError("Images must contain between 1 byte and 8 MiB", permanent=True)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                    raise ValueError()
                width, height = image.size
                if width * height > MAX_PIXELS or min(width, height) < 1:
                    raise ValueError()
                mime = Image.MIME[image.format]
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise PlexError(
            "Use a valid static PNG, JPEG or WebP image of at most 32 megapixels", permanent=True
        ) from None
    return Asset(data, hashlib.sha256(data).hexdigest(), mime, width, height)


def same_image(source, served):
    if source.digest == served.digest:
        return True
    if abs(source.width / source.height - served.width / served.height) > 0.01:
        return False
    # Tolerate ordinary Plex resizing/JPEG encoding, not an arbitrary changed image.
    with Image.open(io.BytesIO(source.data)) as a, Image.open(io.BytesIO(served.data)) as b:
        a = a.convert("RGB").resize((128, 128))
        b = b.convert("RGB").resize((128, 128))
        stats = ImageStat.Stat(ImageChops.difference(a, b))
        return max(stats.mean) <= 2 and max(stats.rms) <= 6


def protected_metadata(item):
    """Exclude artwork/bookkeeping and session-specific decisions, not media facts."""
    result = {
        "item": {
            k: item.get(k)
            for k in (
                "ratingKey",
                "guid",
                "type",
                "title",
                "summary",
                "duration",
                "originalTitle",
                "titleSort",
                "year",
                "studio",
                "contentRating",
                "originallyAvailableAt",
            )
        }
    }
    volatile = {"selected", "decision", "videoDecision", "audioDecision", "subtitleDecision"}

    def tree(node):
        return {
            "tag": node.tag,
            "attrs": {k: v for k, v in sorted(node.attrib.items()) if k not in volatile},
            "children": [tree(child) for child in node],
        }

    result["media"] = [tree(node) for node in item.findall("Media")]
    return result


def target_identity(item):
    return {
        "rating_key": item.get("ratingKey"),
        "guid": item.get("guid"),
        "type": item.get("type"),
        "section": item.get("librarySectionID"),
        "parts": sorted((p.get("id", ""), p.get("file", "")) for p in item.findall("Media/Part")),
    }


class PlexClient:
    def __init__(self, url, token, transport=None):
        self.url = base_url(url)
        self.http = httpx.AsyncClient(
            headers={"X-Plex-Token": token, "Accept": "application/xml"},
            timeout=httpx.Timeout(15, connect=5),
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )

    async def close(self):
        await self.http.aclose()

    async def _request(self, method, path, *, data=None, mime=None, limit=MAX_IMAGE_BYTES):
        try:
            async with asyncio.timeout(25):
                async with self.http.stream(
                    method, self.url + path, content=data, headers={"Content-Type": mime} if mime else None
                ) as response:
                    if response.status_code != 200:
                        raise PlexError(
                            f"Plex returned HTTP {response.status_code}",
                            permanent=response.status_code
                            in {301, 302, 303, 307, 308, 400, 401, 403, 404, 405},
                        )
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise PlexError("Plex response exceeds the size limit", permanent=True)
                        chunks.append(chunk)
                    return b"".join(chunks)
        except (httpx.HTTPError, TimeoutError):
            # Never leak request headers, token, URLs or server bodies into logs.
            raise PlexError("Plex request timed out or the server is unreachable") from None

    async def _xml(self, path):
        body = await self._request("GET", path, limit=1024 * 1024)
        try:
            if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
                raise ValueError()
            return ET.fromstring(body)
        except (ET.ParseError, ValueError):
            raise PlexError("Plex returned invalid metadata XML", permanent=True) from None

    async def identity(self):
        item = await self._xml("/identity")
        if not item.get("machineIdentifier"):
            raise PlexError("Plex did not return a server identity", permanent=True)
        return {
            "machine_id": item.get("machineIdentifier"),
            "version": item.get("version", ""),
            "name": item.get("friendlyName", "Plex Media Server"),
        }

    async def metadata(self, key):
        key = rating_key(key)
        root = await self._xml(f"/library/metadata/{key}")
        items = [node for node in root if node.get("ratingKey") == key]
        if len(items) != 1 or items[0].tag != "Video" or items[0].get("type") != "movie":
            raise PlexError("Select a single Plex movie/video library item", permanent=True)
        return items[0]

    async def image(self, key, slot, item=None):
        key = rating_key(key)
        element = SLOTS[slot]
        item = item if item is not None else await self.metadata(key)
        path = item.get(element)
        if not path:
            kind = "coverPoster" if slot == "poster" else "background"
            path = next((n.get("url") for n in item.findall("Image") if n.get("type") == kind), None)
        if not path or not re.fullmatch(rf"/library/metadata/{key}/{element}/[0-9]+", path):
            raise PlexError(f"Plex has no supported current {slot} URL", permanent=True)
        return image_asset(await self._request("GET", path))

    async def set_image(self, key, slot, asset):
        # The only mutation in this client: a single item's thumb or art, raw bytes.
        await self._request(
            "POST",
            f"/library/metadata/{rating_key(key)}/{SLOTS[slot]}",
            data=asset.data,
            mime=asset.mime,
            limit=1024 * 1024,
        )


async def download_image(url, transport=None):
    try:
        p = urlsplit(url)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            raise PlexError("Image source must be an HTTP(S) URL", permanent=True)
        async with asyncio.timeout(25):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15, connect=5),
                follow_redirects=True,
                max_redirects=3,
                transport=transport,
                trust_env=False,
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_IMAGE_BYTES:
                            raise PlexError("Image source exceeds 8 MiB")
                        chunks.append(chunk)
                return image_asset(b"".join(chunks))
    except (ValueError, httpx.HTTPError, TimeoutError):
        raise PlexError("Event artwork download failed") from None
