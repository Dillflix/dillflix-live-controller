"""Copied behind a fixture-specific shebang by test_screen.py; never used by production."""

import base64
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path

root = Path(__file__).parent
args = sys.argv[1:]
with (root / "adb-calls.jsonl").open("a") as output:
    output.write(json.dumps(args) + "\n")
if args[:1] == ["connect"]:
    print("connected")
elif args[-1:] == ["get-state"]:
    print("device")
elif "forward" in args and "tcp:0" in args:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    (root / "port").write_text(str(port))
    print(port)
elif args[-1].startswith("echo $$"):
    print(os.getpid(), flush=True)
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", int((root / "port").read_text())))
        listener.listen()
        conn, _ = listener.accept()
        with conn:
            conn.sendall(
                b"\x00" + b"Fixture TV".ljust(64, b"\x00") + struct.pack(">III", 0x68323634, 320, 180)
            )
            config = base64.b64decode("AAAAAWdCwAvZBQZ+fAQAAAABaM4PyA==")
            conn.sendall(struct.pack(">QI", 1 << 63, len(config)) + config)
            try:
                for index in range(1000):
                    frame = b"\x00\x00\x00\x01\x65fixture"
                    conn.sendall(struct.pack(">QI", (1 << 62) | (index * 100000), len(frame)) + frame)
                    time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError):
                pass
