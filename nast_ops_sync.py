#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import NAST artifacts through the existing token-authenticated file API.

No secret is copied into an artifact or log. Production callers must explicitly
enable the cross-host synchronization gate before invoking this module.
"""
from __future__ import annotations

import os
import json
import threading
import time
from pathlib import Path

from ops_telemetry import decode_snapshot_export, ingest_snapshot


REMOTE_PATH = os.environ.get(
    "OPS_NAST_EXPORT_REMOTE_PATH", "ops-export/ops-snapshot.json.gz.b64")
_SYNC_LOCK = threading.Lock()


def snapshot_age(state_base, now=None):
    """Return seconds since the last successful local ingest, or infinity."""
    path = Path(state_base) / "nast" / "snapshot-meta.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8-sig"))
        received_at = float(meta["received_at"])
        return max(0.0, float(time.time() if now is None else now) - received_at)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return float("inf")


def sync_nast_snapshot(state_base, get_fn=None):
    if get_fn is None:
        from mcfile_client import mcfile_get
        get_fn = mcfile_get
    encoded = get_fn("/read", path=REMOTE_PATH)
    payload = decode_snapshot_export(encoded)
    root = ingest_snapshot(Path(state_base), payload, expected_server="nast")
    return {"ok": True, "server": "nast", "path": str(root)}


def sync_if_stale(state_base, max_age=300, get_fn=None, now=None):
    """Refresh through McFile only when the local NAST cache is stale.

    The second age check inside the lock prevents concurrent QQ queries and the
    background refresher from duplicating a cross-host read.
    """
    age = snapshot_age(state_base, now=now)
    if age <= max_age:
        return {"ok": True, "server": "nast", "cached": True, "age": age}
    with _SYNC_LOCK:
        age = snapshot_age(state_base, now=now)
        if age <= max_age:
            return {"ok": True, "server": "nast", "cached": True, "age": age}
        result = sync_nast_snapshot(state_base, get_fn=get_fn)
        result.update({"cached": False, "age": 0.0})
        return result
