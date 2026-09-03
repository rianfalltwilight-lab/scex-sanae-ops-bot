#!/usr/bin/env python3
"""Durable per-group chat archive with daily UTF-8 log rotation."""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_SAFE_NAME = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._ -]+")
try:
    ARCHIVE_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # The production Windows venv intentionally has no tzdata package.
    # UTC+8 is stable for this deployment and avoids adding a dependency.
    ARCHIVE_TZ = timezone(timedelta(hours=8), name="CST")


def safe_group_name(name: str, group_id: object) -> str:
    """Return a readable, traversal-safe directory name."""
    value = str(name or "").strip()
    value = _SAFE_NAME.sub("_", value).strip(" ._")
    return value[:80] or f"group-{group_id}"


class ChatArchive:
    """Append-only archive: ``root/<group-name>/YYYY-MM-DD.log``."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    def append(self, group_id: object, group_name: str, sender: str,
               text: str, when: datetime | None = None) -> Path | None:
        text = str(text or "").replace("\r", " ").replace("\n", " ").strip()
        if not text:
            return None
        when = when or datetime.now(ARCHIVE_TZ)
        directory = self.root / safe_group_name(group_name, group_id)
        path = directory / f"{when:%Y-%m-%d}.log"
        line = f"{when:%H:%M:%S} {sender or group_id}：{text}\n"
        with self._lock:
            directory.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
        return path

    def read(self, group_id: object, group_name: str, date: str,
             count: int = 100, player: str = "", keyword: str = "") -> list[str]:
        path = self.root / safe_group_name(group_name, group_id) / f"{date}.log"
        if not path.is_file():
            return []
        count = max(1, min(int(count), 500))
        with self._lock:
            lines = path.read_text(encoding="utf-8").splitlines()
        hits = [line for line in lines
                if (not player or player in line) and (not keyword or keyword in line)]
        return hits[-count:]
