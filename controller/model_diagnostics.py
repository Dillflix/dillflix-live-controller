"""Task-local, bounded model evidence persisted by the calling playback workflow."""

import json
import asyncio
import copy
import logging
import re
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar

_sink = ContextVar("model_diagnostic_sink", default=None)
_archive = ContextVar("model_archive_sink", default=None)
_context = ContextVar("model_capture_context", default=None)
_decision = ContextVar("model_decision", default=None)
log = logging.getLogger(__name__)


@asynccontextmanager
async def capture_model_calls_async(sink, *, recorder=None, context=None):
    """Persist ordered snapshots off-loop; finish all queued writes on cancellation."""
    queue = asyncio.Queue()

    def enqueue(record):
        queue.put_nowait(copy.deepcopy(record))

    async def write():
        while (record := await queue.get()) is not None:
            def persist():
                for callback in (recorder, sink):
                    if callback:
                        try:
                            callback(record)
                        except Exception:
                            log.error("Model capture sink failed for call %s", record.get("call_id"))
            await asyncio.to_thread(persist)

    worker = asyncio.create_task(write())
    try:
        with capture_model_calls(enqueue, context=context):
            yield
    finally:
        queue.put_nowait(None)
        cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
        await worker
        if cancelled:
            raise asyncio.CancelledError


async def report_progress(callback, value):
    """Support asynchronous persistence and existing in-memory chooser callbacks."""
    import inspect
    result = callback(value)
    if inspect.isawaitable(result):
        await result


@contextmanager
def capture_model_calls(sink, *, recorder=None, context=None):
    token = _sink.set(sink)
    archive_token = _archive.set(recorder)
    context_token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(context_token)
        _archive.reset(archive_token)
        _sink.reset(token)


def publish(record):
    scope = _decision.get()
    if scope is not None:
        scope["trace"] = record
    enriched = {**record, "context": _context.get() or {}}
    # Archival and short-lived diagnostics are independent; a capture failure
    # cannot change the chooser's answer or authorize/retry any playback action.
    for sink in (_archive.get(), _sink.get()):
        if sink:
            try:
                sink(enriched)
            except Exception:
                log.error("Model capture sink failed for call %s", record.get("call_id"))


@contextmanager
def model_decision():
    """Mark local schema/evidence validation, never claim teacher correctness."""
    scope = {}
    token = _decision.set(scope)
    validation = {"state": "passed"}
    try:
        yield validation
    except BaseException as exc:
        validation = {"state": "rejected", "error_code": getattr(exc, "code", type(exc).__name__)}
        if getattr(exc, "validation_reason", None):
            validation["reason"] = exc.validation_reason
        raise
    finally:
        _decision.reset(token)
        trace = scope.get("trace")
        if trace is not None and trace.get("state") == "returned":
            trace["validation"] = validation
            publish(trace)


def redact_nested(value):
    """Also redact JSON embedded in user messages or provider answer strings."""
    from .prime_player.diagnostics import redact

    if isinstance(value, dict):
        return {k: redact_nested(v) for k, v in redact(value).items()}
    if isinstance(value, list):
        return [redact_nested(v) for v in value]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except ValueError:
            return "[omitted: malformed embedded JSON]"
        cleaned = redact_nested(parsed)
        if cleaned != parsed:
            return json.dumps(cleaned, ensure_ascii=False)
    return value


def body_evidence(raw, api_key, limit, *, truncated=False):
    """Redact complete JSON before bounding it; omit unsafe non-JSON bodies."""
    original = text = raw.decode("utf-8", errors="replace")
    if api_key:
        text = text.replace(api_key, "[redacted]")
    text = re.sub(r'(?i)Bearer\s+[^\s"\x27]+|\bsk-[A-Za-z0-9_-]+', "[redacted]", text)
    result = {"truncated": truncated or len(raw) > limit, "limit_bytes": limit, "redacted": text != original}
    try:
        value = json.loads(text)
    except ValueError:
        # A partial JSON field or arbitrary error page cannot be reliably scrubbed.
        result.update(text="[omitted: non-JSON or incomplete response body]", redacted=True, omitted=True)
        return result
    cleaned = redact_nested(value)
    result["redacted"] |= cleaned != value
    encoded = json.dumps(cleaned, ensure_ascii=False).encode("utf-8")
    if len(encoded) > limit:
        result.update(truncated=True, text=encoded[:limit].decode("utf-8", errors="replace"))
    else:
        result["json"] = cleaned
    return result
