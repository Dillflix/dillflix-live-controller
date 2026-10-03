"""Read-only service/compatibility check; does not search, play, or suspend."""

import asyncio
import json

from ..config import Settings
from ..executor.models import ExecutorError
from .client import PrimePlayerClient


async def check(settings):
    config = settings.executor
    if config.mode != "prime-player":
        raise ValueError("Set PLAYBACK_ADAPTER=prime-player and PRIME_PLAYER_SOCKET")
    client = PrimePlayerClient(config.prime_socket)
    try:
        health = await client.health()
        return {
            "session_id": health["session_id"],
            "api_version": health.get("api_version"),
            "suspended": health.get("suspended"),
            "busy": health.get("busy"),
            "implemented_capabilities": health.get("capabilities"),
            "available_capabilities": health.get("compatibility", {}).get("capabilities"),
            "cancel_contract_connected": client.cancellation_ready,
            "match_model_configured": bool(config.prime_match_model),
            "device_mutations": 0,
        }
    finally:
        await client.close()


def main():
    try:
        result = asyncio.run(check(Settings.from_env()))
    except (ExecutorError, ValueError) as error:
        print(json.dumps({"error": str(error), "device_mutations": 0}))
        raise SystemExit(1) from error
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
