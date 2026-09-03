#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发现新的 SimpleBackups ZIP，并交给现有可靠备份流水线处理。"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from mcfile_client import mcfile_get

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
STATE_PATH = LOG_DIR / "backup-watch-state.json"
PIPELINE_STATE_PATH = LOG_DIR / "backup-pipeline-status.json"
PIPELINE_LOCK_PATH = LOG_DIR / "backup-pipeline.lock"
PIPELINE_PATH = HERE / "backup_pipeline.py"
WATCH_LOG_PATH = LOG_DIR / "backup-watch.log"
BACKUP_DIR = "simplebackups/world"
POLL_SECONDS = int(os.environ.get("BACKUP_WATCH_POLL_SECONDS", "30"))
STABLE_CHECKS = 3
MIN_BACKUP_SIZE = 1024 * 1024
_START_LOCK = threading.Lock()
_STARTED = False


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _backups() -> list[dict]:
    entries = json.loads(mcfile_get("/list", path=BACKUP_DIR))
    return sorted(
        (e for e in entries if not e.get("dir") and str(e.get("name", "")).endswith(".zip")),
        key=lambda e: (int(e.get("mtime", 0)), str(e.get("name", ""))),
    )


def _pipeline_busy() -> bool:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with PIPELINE_LOCK_PATH.open("a+") as fp:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
    return False


def _pipeline_already_reported(name: str) -> bool:
    status = _read_json(PIPELINE_STATE_PATH, {})
    return status.get("state") in ("success", "failed") and status.get("file") == name


def _spawn(name: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = WATCH_LOG_PATH.open("a", encoding="utf-8")
    try:
        subprocess.Popen(
            [sys.executable, str(PIPELINE_PATH), "--existing", name, "--source", "scheduled"],
            cwd=str(HERE), stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
        )
    finally:
        log.close()


def check_once() -> str:
    """执行一次发现；返回动作，便于离线测试和运行日志判断。"""
    backups = _backups()
    names = [str(e["name"]) for e in backups]
    state = _read_json(STATE_PATH, {})
    if not state.get("initialized"):
        _write_json(STATE_PATH, {"initialized": True, "handled": names[-64:], "updatedAt": int(time.time())})
        return "baseline"

    handled = list(state.get("handled") or [])
    unseen = [entry for entry in backups if str(entry["name"]) not in set(handled)]
    if not unseen:
        return "idle"

    entry = unseen[0]
    name = str(entry["name"])
    size = int(entry.get("size", 0))
    dispatched = state.get("dispatched")
    if dispatched == name:
        if _pipeline_busy():
            return "busy"
        if _pipeline_already_reported(name):
            handled.append(name)
            _write_json(STATE_PATH, {"initialized": True, "handled": handled[-64:],
                                    "candidate": None, "dispatched": None,
                                    "updatedAt": int(time.time())})
            return "completed"
        state["dispatched"] = None
        _write_json(STATE_PATH, state)

    candidate = state.get("candidate") or {}
    stable = int(candidate.get("stableChecks", 0)) + 1 \
        if candidate.get("name") == name and int(candidate.get("size", -1)) == size else 0
    state = {
        "initialized": True,
        "handled": handled[-64:],
        "candidate": {"name": name, "size": size, "stableChecks": stable},
        "dispatched": state.get("dispatched"),
        "updatedAt": int(time.time()),
    }
    _write_json(STATE_PATH, state)
    if size < MIN_BACKUP_SIZE or stable < STABLE_CHECKS:
        return "stabilizing"

    if _pipeline_busy():
        return "busy"
    if _pipeline_already_reported(name):
        handled.append(name)
        _write_json(STATE_PATH, {"initialized": True, "handled": handled[-64:],
                                "candidate": None, "dispatched": None,
                                "updatedAt": int(time.time())})
        return "deduplicated"

    _spawn(name)
    _write_json(STATE_PATH, {"initialized": True, "handled": handled[-64:],
                            "candidate": None, "dispatched": name,
                            "updatedAt": int(time.time())})
    return "spawned"


def _loop() -> None:
    while True:
        try:
            action = check_once()
            if action not in ("idle", "busy", "stabilizing"):
                print(f"[backup-watch] {action}", flush=True)
        except Exception as exc:
            print(f"[backup-watch] {type(exc).__name__}: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


def start() -> bool:
    global _STARTED
    with _START_LOCK:
        if _STARTED:
            return False
        _STARTED = True
        threading.Thread(target=_loop, name="backup-watch", daemon=True).start()
        return True
