#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounded, low-cost long-term memory for the approved social group.

Only aggregate topics are learned automatically.  Full chat text belongs in
the separate archive; this file keeps no raw messages and rejects obvious
credential/path material.
"""
from __future__ import annotations

import json
import math
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
                now = int(time.time())
                for term, row in list(rows.items())[:2000]:
                    if not isinstance(row,dict) or _SENSITIVE.search(str(term)):
                        continue
                    last = int(row.get('lastSeen') or 0)
                    if last > now+60 or last < now-7*86400:
                        continue
                    # Legacy totals have no daily distribution or speaker IDs.
                    # Retain only the single observation evidenced by lastSeen.
                    buckets = row.get('buckets') if data.get('version') == 2 else {str(last//86400):1}
                    if not isinstance(buckets,dict):
                        continue
                    self._topics[str(term).casefold()] = dict(row,buckets={
                        str(day):max(0,min(100000,int(count))) for day,count in buckets.items()
                        if str(day).isdigit() and now//86400-6 <= int(day) <= now//86400})
                self._prune(now)
        except (OSError, ValueError, TypeError):
            self._topics = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 2, "groupId": self.group_id, "topics": self._topics}
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
            term = term.casefold()
            if term in _COMMON or term.casefold() in {x.casefold() for x in _COMMON}:
                continue
            if term not in out:
                out.append(term)
        return out[:12]

    def _prune(self, now):
        today = now//86400
        for term, row in list(self._topics.items()):
            buckets = {day:count for day,count in row.get('buckets',{}).items()
                       if today-6 <= int(day) <= today and count > 0}
            if not buckets:
                del self._topics[term]
                continue
            row['buckets'] = buckets
            row['count'] = sum(buckets.values())
        if len(self._topics) > self.max_topics:
            # Reserve space for recent arrivals so first observations can grow.
            recent = sorted(self._topics,key=lambda k:self._topics[k].get('lastSeen',0),reverse=True)
            keep = recent[:max(1,self.max_topics//4)]
            ranked = sorted(self._topics,key=lambda k:self._score(self._topics[k],now),reverse=True)
            keep += [term for term in ranked if term not in keep][:self.max_topics-len(keep)]
            self._topics = {term:self._topics[term] for term in keep}

    @staticmethod
    def _score(row, now):
        return sum(count*math.exp(-max(0,now-int(day)*86400)/(3*86400))
                   for day,count in row.get('buckets',{}).items())

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
            self._prune(now)
            new_topic = False
            for term in terms:
                if term not in self._topics:
                    new_topic = True
                row = self._topics.setdefault(term, {"count": 0, "firstSeen": now,'buckets':{}})
                day = str(now//86400)
                row['buckets'][day] = row['buckets'].get(day,0)+1
                row["count"] = sum(row['buckets'].values())
                row["lastSeen"] = now
            self._prune(now)
            # Avoid a JSON rewrite for every chat message.  New topics are
            # flushed immediately; existing counters are persisted at most
            # once per 15 seconds.
            if new_topic or time.time() - self._last_save >= 15:
                self._save()
                self._last_save = time.time()

    def context(self, limit: int = 8) -> str:
        with self._lock:
            now = int(time.time())
            self._prune(now)
            rows = sorted(self._topics.items(), key=lambda x:self._score(x[1],now),reverse=True)
            rows = rows[:max(1, min(12, int(limit)))]
        if not rows:
            return ""
        return ("〖近七天群话题线索；不是发言统计，也不属于任何特定群友〗\n" +
                "、".join(term for term,row in rows) +
                "\n只在与当前话题相关时参考；总结群聊、个人次数或排名必须查询真实记录。")

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": True, "groupId": self.group_id, "topics": len(self._topics)}


__all__ = ["SocialMemory"]
