"""Cooperative process lock: running controllers share it, restore excludes them."""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def database_guard(path, *, exclusive=False):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Database is in use. Stop the controller before restore; retry after maintenance."
            ) from exc
        yield
    finally:
        os.close(fd)
