#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small shared contracts for Minecraft events, command results, and OneBot replies."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
import fcntl
from pathlib import Path
from typing import Any


_FINGERPRINT_REPLACEMENTS = (
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{10,13}\b"), "<timestamp>"),
    (re.compile(r"\b-?\d+(?:\.\d+)?\b"), "<n>"),
)


def normalize_log_fingerprint(raw: str) -> str:
    """Return a stable, privacy-reduced fingerprint for a log message.

    This is deliberately only normalization. Alert cooldown, storage, and
    delivery remain owned by their existing callers.
    """
    text = str(raw or "")
    text = re.sub(r"^\s*\[[^\]]+\]\s*(?:\[[^\]]+\]\s*){0,3}", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    for pattern, replacement in _FINGERPRINT_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text[:320]


def event(event_type: str, payload: dict[str, Any], *, source: str = "minecraft_log",
          server: str = "minecraft-server", raw: str = "", occurred_at: float | None = None) -> dict[str, Any]:
    """Create a stable, serializable event envelope."""
    occurred_at = time.time() if occurred_at is None else occurred_at
    basis = json.dumps({"type": event_type, "payload": payload, "raw": raw},
                       ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    event_id = "evt-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
    return {
        "event_id": event_id,
        "type": event_type,
        "source": source,
        "server": server,
        "occurred_at": occurred_at,
        "payload": payload,
    }


def command_result(command: str, *, state: str, output: str = "", error: str | None = None,
                   request_id: str | None = None, actor: str | None = None,
                   started_at: float | None = None, finished_at: float | None = None,
                   server: str = "minecraft-server") -> dict[str, Any]:
    """Create a command result envelope without changing the human renderer."""
    return {
        "request_id": request_id or "req-" + uuid.uuid4().hex[:24],
        "command": command,
        "server": server,
        "state": state,
        "output": output,
        "error": error,
        "actor": actor,
        "started_at": started_at,
        "finished_at": finished_at,
    }


def onebot_success(raw: str) -> tuple[bool, dict[str, Any] | None]:
    """Return success only when a JSON OneBot response explicitly says retcode 0."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False, None
    if data.get("status") == "failed":
        return False, data
    return data.get("retcode") in (0, "0"), data


def render_command_result(result: dict[str, Any], title: str = "控制台") -> str:
    """Human-readable fallback for QQ while preserving structured data internally."""
    output = str(result.get("output") or "").strip()
    if result.get("state") == "success":
        return f"[{title}]\n{output or '(命令已执行，无返回内容)'}"
    return f"[{title}] 执行失败：{result.get('error') or '未知错误'}"


def read_incremental_lines(path: str | Path, state: dict[str, Any]) -> tuple[list[str], dict[str, Any], bool]:
    """Read only complete UTF-8 lines and retain an unterminated tail."""
    path = Path(path)
    if not path.exists():
        return [], state, False
    stat = path.stat()
    inode = stat.st_ino
    size = stat.st_size
    previous_offset = int(state.get("offset", 0)) if state.get("inode") == inode else 0
    # A truncate/recreate can retain the same inode on some filesystems.
    # Treat a file shorter than our committed offset as a new log.
    offset = 0 if previous_offset > size else max(previous_offset, 0)
    if offset and state.get("head"):
        with path.open("rb") as probe:
            current_head = hashlib.sha256(probe.read(min(128, offset))).hexdigest()
        if current_head != state.get("head"):
            offset = 0
    with path.open("rb") as fh:
        fh.seek(offset)
        # The committed offset points before the retained partial tail, so the
        # next read naturally includes that tail. Do not prepend it again.
        raw = fh.read(size - offset)
    if not raw:
        return [], {"inode": inode, "offset": size, "partial": ""}, True
    if raw.endswith(b"\n"):
        complete, partial = raw, b""
    else:
        split_at = raw.rfind(b"\n")
        if split_at < 0:
            complete, partial = b"", raw
        else:
            complete, partial = raw[:split_at + 1], raw[split_at + 1:]
    committed = size - len(partial)
    lines = complete.decode("utf-8", errors="replace").splitlines()
    with path.open("rb") as probe:
        head = hashlib.sha256(probe.read(min(128, committed))).hexdigest()
    return lines, {"inode": inode, "offset": committed,
                   "partial": partial.decode("utf-8", errors="replace"),
                   "head": head}, True


def append_jsonl(path: str | Path, item: dict[str, Any]) -> None:
    """Append one JSON object under a process-safe advisory lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
