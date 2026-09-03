#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounded, low-cost long-term memory for the approved social group.

Only aggregate topics are learned automatically.  Full chat text belongs in
the separate archive; this file keeps no raw messages and rejects obvious
credential/path material.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any


_CQ = re.compile(r"\[CQ:[^\]]*\]")
_CJK = re.compile(r"[\u4e00-\u9fff]{2,12}")
_ASCII = re.compile(r"(?i)\b[a-z][a-z0-9_-]{1,20}\b")
_SENSITIVE = re.compile(
    r"password|passwd|token|secret|api[_ -]?key|密钥|密码|令牌|[A-Za-z]:\\|/home/|/Users/",
    re.I,
)
_COMMON = {
    "现在", "今天", "这个", "那个", "什么", "怎么", "为什么", "有没有", "一下",
    "真的", "不是", "就是", "然后", "已经", "还是", "大家", "我们", "你们",
    "看看", "服务器", "游戏", "玩家", "消息", "早苗", "群里", "谢谢", "收到",
}


class SocialMemory:
    def __init__(self, path: str | Path, group_id: int | str = 0,
                 max_topics: int = 80):
        self.path = Path(path)
        self.group_id = str(group_id)
        self.max_topics = max(20, int(max_topics))
        self._lock = threading.RLock()
        self._topics: dict[str, dict[str, Any]] = {}
        self._last_save = 0.0
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            rows = data.get("topics", {}) if isinstance(data, dict) else {}
            if isinstance(rows, dict):
                self._topics = {str(k): v for k, v in rows.items() if isinstance(v, dict)}
        except (OSError, ValueError, TypeError):
            self._topics = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "groupId": self.group_id, "topics": self._topics}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _terms(text: Any) -> list[str]:
        clean = _CQ.sub(" ", str(text or ""))
        clean = re.sub(r"\s+", " ", clean).strip()
        if not clean or _SENSITIVE.search(clean):
            return []
        out = []
        for term in _CJK.findall(clean) + _ASCII.findall(clean):
            if term in _COMMON or term.casefold() in {x.casefold() for x in _COMMON}:
                continue
            if term not in out:
                out.append(term)
        return out[:12]

    def observe(self, event: dict[str, Any]) -> None:
        if str(event.get("group_id")) != self.group_id:
            return
        raw = event.get("raw_message") or event.get("message")
        if not raw or str(raw).lstrip().startswith(("!", "！", "/")):
            return
        terms = self._terms(raw)
        if not terms:
            return
        now = int(time.time())
        with self._lock:
            new_topic = False
            for term in terms:
                if term not in self._topics:
                    new_topic = True
                row = self._topics.setdefault(term, {"count": 0, "firstSeen": now})
                row["count"] = int(row.get("count") or 0) + 1
                row["lastSeen"] = now
            if len(self._topics) > self.max_topics:
                ordered = sorted(self._topics.items(), key=lambda x: (int(x[1].get("count") or 0), int(x[1].get("lastSeen") or 0)), reverse=True)
                self._topics = dict(ordered[:self.max_topics])
            # Avoid a JSON rewrite for every chat message.  New topics are
            # flushed immediately; existing counters are persisted at most
            # once per 15 seconds.
            if new_topic or time.time() - self._last_save >= 15:
                self._save()
                self._last_save = time.time()

    def context(self, limit: int = 8) -> str:
        with self._lock:
            rows = sorted(self._topics.items(), key=lambda x: (int(x[1].get("count") or 0), int(x[1].get("lastSeen") or 0)), reverse=True)
            rows = rows[:max(1, min(12, int(limit)))]
        if not rows:
            return ""
        return "〖近期群聊长期主题（仅作语境参考）〗\n" + "、".join(
            f"{term}（{int(row.get('count') or 0)}次）" for term, row in rows
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": True, "groupId": self.group_id, "topics": len(self._topics)}


__all__ = ["SocialMemory"]
