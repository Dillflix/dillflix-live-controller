"""Bounded async client for the existing Prime Player Unix HTTP/RPC service.

No SDK/device runtime is embedded here and no mutation is retried by transport.
"""

import asyncio
import json

import httpx

from ..executor.models import ExecutorError


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
        try:
            async with asyncio.timeout(budget):
                async with self.http.stream(
                    "POST", "/rpc", json={"method": method, "params": params}, timeout=budget
                ) as response:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 8 * 1024 * 1024:
                            raise ValueError("oversized response")
                body = json.loads(data)
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
        except ExecutorError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError) as exc:
            raise ExecutorError(
                "prime_transport_unknown",
                f"Prime Player {method} outcome is unknown; inspect the retained attempt before replay",
            ) from exc

    async def health(self):
        value = await self.rpc("health", budget=5)
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
        if {"pages", "discover"}.intersection(capabilities) and health.get("api_version", 0) < 6:
            raise ExecutorError("prime_incompatible", "Prime Player API 6 is required for discovery")
        implemented = health.get("capabilities", [])
        available = health.get("compatibility", {}).get("capabilities", {})
        for name in capabilities:
            if name not in implemented or (
                name in {"search", "play", "playback_status", "stop", "pages", "discover"}
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

    async def search(self, query, timeout, ownership):
        return await self.rpc("search", query=query, timeout=timeout, ownership=ownership, budget=timeout + 5)

    async def pages(self, ownership):
        return await self.rpc("pages", timeout=60, ownership=ownership, budget=65)

    async def discover(self, settings, ownership):
        timeout = 100 * len(settings["enabled_pages"])
        return await self.rpc(
            "discover",
            pages=settings["enabled_pages"],
            limit_per_page=settings["limit_per_page"],
            max_rows=settings["max_rows"],
            timeout=timeout,
            ownership=ownership,
            budget=timeout + 5,
        )

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
