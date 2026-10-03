"""Task-local, bounded model evidence persisted by the calling playback workflow."""

import json
import re
from contextlib import contextmanager
from contextvars import ContextVar

_sink = ContextVar('model_diagnostic_sink', default=None)


@contextmanager
def capture_model_calls(sink):
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def publish(record):
    sink = _sink.get()
    if sink:
        sink(record)


def body_evidence(raw, api_key, limit, *, truncated=False):
    """Retain JSON when complete, text otherwise; redact before persistence."""
    text = raw.decode('utf-8', errors='replace')
    if api_key:
        text = text.replace(api_key, '[redacted]')
    text = re.sub(r'(?i)Bearer\s+[^\s"\x27]+|\bsk-[A-Za-z0-9_-]+', '[redacted]', text)
    encoded = text.encode('utf-8')
    truncated = truncated or len(encoded) > limit
    text = encoded[:limit].decode('utf-8', errors='replace')
    result = {'truncated': truncated, 'limit_bytes': limit}
    try:
        value = json.loads(text)
    except ValueError:
        result['text'] = text
    else:
        from .prime_player.diagnostics import redact

        result['json'] = redact(value)
    return result
