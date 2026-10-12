from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import time

from .errors import BusyError


@contextmanager
def device_lock(directory: Path, key: str, deadline: float):
    """Shared by CLI/MCP on this host. Never unlink a lock inode on release."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(key.encode()).hexdigest() + ".lock"
    fd = os.open(directory / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BusyError("Instrument busy: lock deadline exceeded")
                time.sleep(min(0.02, remaining))
        yield
    finally:
        os.close(fd)
