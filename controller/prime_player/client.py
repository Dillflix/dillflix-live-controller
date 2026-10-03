"""Bounded async client for the existing Prime Player Unix HTTP/RPC service.

No SDK/device runtime is embedded here and no mutation is retried by transport.
"""

import asyncio
import json
import logging
import time
import uuid
from contextvars import ContextVar
from functools import wraps

import httpx

from ..diagnostic_log import request_body
from ..executor.models import ExecutorError

rpc_context = ContextVar("prime_rpc_context", default={})


def correlated(function):
    @wraps(function)
    async def wrapped(self, row, *args, **kwargs):
        correlation = (
            {"token": row}
            if isinstance(row, str)
            else {"token": row["token"], "request_id": row["request_id"]}
        )
        reset = rpc_context.set(correlation)
        try:
            return await function(self, row, *args, **kwargs)
        finally:
            rpc_context.reset(reset)

    return wrapped


class PrimePlayerClient:
    def __init__(self, socket_path, *, transport=None):
        self.http = httpx.AsyncClient(
            base_url="http://prime-player",
            transport=transport or httpx.AsyncHTTPTransport(uds=str(socket_path), retries=0),
            trust_env=False,
            follow_redirects=False,
        )

    async def close(self):
        await self.http.aclose()

    async def rpc(self, method, *, budget=60, **params):
        call_id = uuid.uuid4().hex
        started = time.monotonic()
        log = logging.getLogger("controller.prime_rpc")
        evidence = {
            **rpc_context.get(),
            "call_id": call_id,
            "request": request_body(method, params),
            "budget_seconds": budget,
        }
        if method != "diagnostics":
            log.info("Prime RPC started", extra={"diagnostic_rpc": {**evidence, "phase": "request"}})
        data = bytearray()
        try:
            async with asyncio.timeout(budget):
                async with self.http.stream(
                    "POST",
                    "/rpc",
                    json={"method": method, "params": params},
                    timeout=budget,
                    headers={"X-Dillflix-Call-Id": call_id},
                ) as response:
                    evidence["http_status"] = response.status_code
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 8 * 1024 * 1024:
                            raise ValueError("oversized response")
                evidence["http_status"] = response.status_code
                evidence["response"] = data.decode("utf-8", errors="replace")
                body = json.loads(data)
                evidence["response"] = body
                if not isinstance(body, dict):
                    raise ValueError("invalid response")
                if "error" in body:
                    kind = body["error"].get("type")
                    code = {
                        "BusyError": "prime_busy",
                        "StaleResultError": "prime_stale_result",
                        "CompatibilityError": "prime_incompatible",
                    }.get(kind, "prime_operation_unknown")
                    # Server messages can contain arbitrary application content.
                    raise ExecutorError(code, f"Prime Player {method} returned {kind}")
                if response.status_code != 200 or not isinstance(body.get("result"), dict):
                    raise ValueError("invalid response")
                return body["result"]
        except ExecutorError as exc:
            evidence["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError) as exc:
            evidence["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise ExecutorError(
                "prime_transport_unknown",
                f"Prime Player {method} outcome is unknown; inspect the retained attempt before replay",
            ) from exc
        except asyncio.CancelledError:
            evidence["error"] = {
                "type": "CancelledError",
                "message": "Client wait cancelled; server outcome unknown",
            }
            raise
        finally:
            if method != "diagnostics":
                if data and "response" not in evidence:
                    evidence["partial_response"] = data[: 256 * 1024].decode("utf-8", errors="replace")
                    evidence["response_incomplete"] = True
                log.info(
                    "Prime RPC finished",
                    extra={
                        "diagnostic_rpc": {
                            **evidence,
                            "phase": "finished",
                            "duration_seconds": round(time.monotonic() - started, 3),
                        }
                    },
                )

    async def control_health(self):
        """Read service identity even when playback/runtime health has failed."""
        value = await self.rpc("health", budget=5)
        if not isinstance(value.get("session_id"), str) or not value["session_id"]:
            raise ExecutorError("prime_unavailable", "Prime Player service identity is unavailable")
        return value

    async def health(self):
        value = await self.control_health()
        if (
            not isinstance(value.get("session_id"), str)
            or not value["session_id"]
            or value.get("failure")
            or value.get("closed") is not False
        ):
            raise ExecutorError("prime_unavailable", "Prime Player has no healthy service session")
        return value

    @staticmethod
    def require(health, *capabilities):
        if health.get("api_version", 0) < 4:
            raise ExecutorError("prime_incompatible", "Prime Player API 4 or newer is required")
        implemented = health.get("capabilities", [])
        available = health.get("compatibility", {}).get("capabilities", {})
        for name in capabilities:
            if name not in implemented or (
                name in {"search", "play", "playback_status", "stop"}
                and available.get("javascript_navigation" if name == "stop" else name, {}).get("available")
                is not True
            ):
                raise ExecutorError("prime_incompatible", f"Prime Player capability unavailable: {name}")

    @staticmethod
    def ownership(health, *, automatic=False):
        receipt = health.get("ownership", {})
        if (
            receipt.get("acknowledged") is not True
            or receipt.get("session_id") != health.get("session_id")
            or receipt.get("mode") not in ({"automatic"} if automatic else {"automatic", "manual"})
            or type(receipt.get("epoch")) is not int
        ):
            raise ExecutorError("prime_ownership_unavailable", "Prime input ownership is not acknowledged")
        return receipt

    @staticmethod
    def control_ownership(health):
        receipt = health.get("ownership", {})
        if (
            receipt.get("session_id") != health.get("session_id")
            or type(receipt.get("epoch")) is not int
            or receipt.get("mode") not in {"automatic", "manual", "blocked", "transitioning"}
        ):
            raise ExecutorError("prime_ownership_unavailable", "Prime control receipt is invalid")
        return receipt

    async def search(self, query, timeout, ownership):
        return await self.rpc("search", query=query, timeout=timeout, ownership=ownership, budget=timeout + 5)

    async def play(self, handle, attempt_id, ownership):
        return await self.rpc("play", handle=handle, mode="live", attempt_id=attempt_id, ownership=ownership)

    async def attempt(self, attempt_id):
        return await self.rpc("attempt", attempt_id=attempt_id, budget=60)

    async def status(self, attempt_id, timeout):
        return await self.rpc("playback_status", attempt_id=attempt_id, timeout=timeout, budget=timeout + 5)

    async def cancel_work(self, session_id, attempt_id, ownership):
        return await self.rpc(
            "cancel", session_id=session_id, attempt_id=attempt_id, ownership=ownership, budget=45
        )

    async def stop_attempt(self, session_id, attempt_id, ownership):
        return await self.rpc(
            "stop", session_id=session_id, attempt_id=attempt_id, ownership=ownership, budget=60
        )
