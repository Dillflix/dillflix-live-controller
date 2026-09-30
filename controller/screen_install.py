"""Install the exact upstream scrcpy server used by our capture protocol."""

import argparse
import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path

SERVER_VERSION = "3.3.4"
SERVER_SHA256 = "8588238c9a5a00aa542906b6ec7e6d5541d9ffb9b5d0f6e1bc0e365e2303079e"
SERVER_URL = f"https://github.com/Genymobile/scrcpy/releases/download/v{SERVER_VERSION}/scrcpy-server-v{SERVER_VERSION}"


def verified(data: bytes) -> bool:
    return hashlib.sha256(data).hexdigest() == SERVER_SHA256


def install(destination: Path):
    if destination.is_file() and verified(destination.read_bytes()):
        return
    with urllib.request.urlopen(SERVER_URL, timeout=60) as response:
        data = response.read(2 * 1024 * 1024)
    if not verified(data):
        raise ValueError("scrcpy download checksum does not match the pinned release")
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".scrcpy-")
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", nargs="?", type=Path, default=Path("data/scrcpy-server-v3.3.4"))
    args = parser.parse_args()
    install(args.destination)
    print(f"Verified scrcpy {SERVER_VERSION}: {args.destination}")


if __name__ == "__main__":
    main()
