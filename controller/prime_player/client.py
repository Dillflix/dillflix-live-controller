"""Bounded async client for the existing Prime Player Unix HTTP/RPC service.

No SDK/device runtime is embedded here and no mutation is retried by transport.
"""

import asyncio
import json

import httpx

from ..executor.models import ExecutorError


class PrimePlayerClient:
    cancellation_ready = False

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
        implemented = health.get("capabilities", [])
        available = health.get("compatibility", {}).get("capabilities", {})
        for name in capabilities:
            if name not in implemented or available.get(name, {}).get("available") is not True:
                raise ExecutorError("prime_incompatible", f"Prime Player capability unavailable: {name}")

    async def search(self, query, timeout):
        return await self.rpc("search", query=query, timeout=timeout, budget=timeout + 5)

    async def play(self, handle, attempt_id):
        return await self.rpc("play", handle=handle, mode="live", attempt_id=attempt_id)

    async def attempt(self, attempt_id):
        return await self.rpc("attempt", attempt_id=attempt_id, budget=60)

    async def status(self, attempt_id, timeout):
        return await self.rpc("playback_status", attempt_id=attempt_id, timeout=timeout, budget=timeout + 5)

    async def suspend(self, value):
        result = await self.rpc("suspend", value=value, budget=5)
        if result.get("suspended") is not value:
            raise ExecutorError("prime_handoff_unconfirmed", "Prime Player did not acknowledge suspension")
        return result

    async def cancel_attempt(self, session_id, attempt_id):
        """The service cancel/stop API is being implemented independently.

        Deliberately do not guess its wire shape, kill Prime, or treat suspend as
        active stop. Replace only this method once its released contract is known.
        """
        raise ExecutorError(
            "prime_cancel_contract_pending",
            "Prime Player cancel/stop integration awaits the service's released contract",
            retryable=False,
        )
