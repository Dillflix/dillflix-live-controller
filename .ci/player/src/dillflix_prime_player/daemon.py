"""Environment-based system service entry point; no shell or venv activation."""

import os
import sys

from .cli import main


def arguments(env):
    required = (
        "PRIME_PLAYER_SOCKET",
        "PRIME_PLAYER_SERIAL",
        "PRIME_PLAYER_SERVER_BINARY",
        "PRIME_PLAYER_STATE_DIR",
    )
    for key in required:
        if not env.get(key):
            raise ValueError(key + " must be configured in the service environment")
    args = [
        "prime-player",
        "--socket",
        env["PRIME_PLAYER_SOCKET"],
        "serve",
        "--serial",
        env["PRIME_PLAYER_SERIAL"],
        "--server-binary",
        env["PRIME_PLAYER_SERVER_BINARY"],
        "--state-dir",
        env["PRIME_PLAYER_STATE_DIR"],
        "--connection-timeout",
        env.get("PRIME_PLAYER_CONNECTION_TIMEOUT", "120"),
        "--restart-prime",
        "--supervised",
        "--shared-socket",
    ]
    if env.get("PRIME_PLAYER_ROOT_PREFIX"):
        args.extend(["--root-prefix", env["PRIME_PLAYER_ROOT_PREFIX"]])
    return args


if __name__ == "__main__":
    sys.argv = arguments(os.environ)
    main()
