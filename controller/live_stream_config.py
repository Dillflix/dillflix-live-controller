"""Browser-feed settings validation, also used by the stdlib-only host installer."""

import re
from urllib.parse import urlsplit

STREAM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def validate_device_map(value):
    if (not isinstance(value, dict) or len(value) > 64
            or any(not isinstance(key, str) or not STREAM_ID.fullmatch(key)
                   or not isinstance(device, str) or not STREAM_ID.fullmatch(device)
                   for key, device in value.items())):
        raise ValueError("LIVE_BROWSER_DEVICE_MAP must map feed IDs to controller device IDs")


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
