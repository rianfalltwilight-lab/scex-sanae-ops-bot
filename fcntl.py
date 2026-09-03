"""Minimal Windows compatibility shim for the Linux-only fcntl.flock API.

The standalone Sanae service has one process and uses these locks only to
serialize its own append/state writes.  A process-local reentrant lock keeps
those writes safe on Windows; the Linux WSL copy continues using real fcntl.
"""
from __future__ import annotations

import threading

LOCK_EX = 1
LOCK_NB = 2
LOCK_UN = 8

_LOCK = threading.RLock()


def flock(_fd: int, operation: int) -> None:
    if operation & LOCK_UN:
        try:
            _LOCK.release()
        except RuntimeError:
            pass
        return
    _LOCK.acquire()
