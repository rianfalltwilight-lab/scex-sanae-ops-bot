#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounded local slang learner for the single approved QQ group."""
from __future__ import annotations
import json, os, re, threading, time
from pathlib import Path
from typing import Any

STATUS = {"candidate", "confirmed", "rejected"}
_ASCII = re.compile(r"(?i)\b[a-z][a-z0-9_-]{1,11}\b")
_CJK = re.compile(r"[\u4e00-\u9fff]{2,8}")
_COMMON = {"\u4eca\u5929","\u73b0\u5728","\u53ef\u4ee5","\u8fd9\u4e2a","\u90a3\u4e2a","\u4ec0\u4e48","\u600e\u4e48","\u4e3a\u4ec0\u4e48","\u6709\u6ca1\u6709","\u4e00\u4e0b","\u771f\u7684","\u4e0d\u662f","\u5c31\u662f","\u7136\u540e","\u5df2\u7ecf","\u8fd8\u662f","\u5927\u5bb6","\u6211\u4eec","\u4f60\u4eec","\u770b\u770b","\u670d\u52a1\u5668","\u6e38\u620f","\u73a9\u5bb6","\u6d88\u606f","\u65e9\u82d7","\u7fa4\u91cc","\u8c22\u8c22","\u6536\u5230"}
_NOISE = re.compile(r"^(?:\u54c8\u54c8+|\u5475\u5475+|\u55ef+|\u54e6+|\u554a+|\u597d\u7684|\u597d\u8036|\u8349+|\u7b11\u6b7b|666+|ok+)$", re.I)
BUILTIN_SLANG = (
    {"term": "\u53c8\u70b8\u4e86\uff1f", "meaning": "怀疑服务或功能又发生故障，带一点关切和吐槽", "usage": "仅在确有异常证据或用户明确报告故障时使用"},
    {"term": "\uff1f", "meaning": "困惑、没听懂或对离谱说法的短促反应", "usage": "最多单独出现一次，后面仍要给出实际解释"},
)

def _clean(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\[CQ:[^\]]*\]", " ", str(value or ""))
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]

class SlangLearner:
    def __init__(self, path: str | Path, group_id: int | str = 0, max_entries: int = 300, max_evidence: int = 8):
        self.path, self.group_id = Path(path), str(group_id)
        self.max_entries, self.max_evidence = max(30, int(max_entries)), max(2, int(max_evidence))
        self._lock, self._entries = threading.RLock(), []
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8").lstrip("\ufeff"))
            if isinstance(data, list): self._entries = [self._normalize(x) for x in data if isinstance(x, dict)]
        except (OSError, ValueError, TypeError): self._entries = []
        # Built-ins are confirmed vocabulary, but remain local and editable.
        for item in BUILTIN_SLANG:
            if not any(x.get("term") == item["term"] for x in self._entries):
                self._entries.append({"term": item["term"], "meaning": item["meaning"],
                                      "usage": item["usage"], "status": "confirmed",
                                      "count": 0, "evidence": [], "updatedAt": time.time()})

    def _normalize(self, item):
        status = str(item.get("status") or "candidate")
        if status not in STATUS: status = "candidate"
        evidence = [{"sender": _clean(x.get("sender"),40), "text": _clean(x.get("text"),180), "time": float(x.get("time") or 0)} for x in (item.get("evidence") or []) if isinstance(x, dict)]
        return {"term": _clean(item.get("term"),32), "meaning": _clean(item.get("meaning"),160), "usage": _clean(item.get("usage"),160), "status": status, "count": max(0,int(item.get("count") or 0)), "evidence": evidence[-self.max_evidence:], "updatedAt": float(item.get("updatedAt") or time.time())}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self._entries[-self.max_entries:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _terms(text):
        terms = []
        for term in _ASCII.findall(_clean(text)) + _CJK.findall(_clean(text)):
            if term.lower() in _COMMON or term in _COMMON or _NOISE.match(term) or term.startswith(("CQ", "http")) or term.isdigit(): continue
            if term not in terms: terms.append(term)
        return terms[:20]

    def observe(self, event):
        if str(event.get("group_id")) != self.group_id: return
        text = _clean(event.get("raw_message") or event.get("message"))
        if not text or text.startswith(("!", "\uff01", "/")): return
        sender = event.get("sender") or {}; source = _clean(sender.get("card") or sender.get("nickname") or event.get("user_id") or "?",40); now = time.time(); changed = False
        with self._lock:
            for term in self._terms(text):
                entry = next((x for x in self._entries if x.get("term") == term), None)
                if entry is None:
                    entry = {"term": term, "meaning": "", "usage": "", "status": "candidate", "count": 0, "evidence": [], "updatedAt": now}; self._entries.append(entry)
                if entry.get("status") == "rejected": continue
                entry["count"] = int(entry.get("count") or 0) + 1; entry["updatedAt"] = now
                entry.setdefault("evidence", []).append({"sender": source, "text": text, "time": now}); entry["evidence"] = entry["evidence"][-self.max_evidence:]; changed = True
            if changed: self._save()

    def _find(self, term): return next((x for x in self._entries if x.get("term") == _clean(term,32)), None)
    def confirm(self, term, meaning="", usage=""):
        with self._lock:
            entry = self._find(term)
            if not entry: return False
            entry["status"] = "confirmed"; entry["meaning"] = _clean(meaning,160) if meaning else entry.get("meaning",""); entry["usage"] = _clean(usage,160) if usage else entry.get("usage",""); entry["updatedAt"] = time.time(); self._save(); return True
    def reject(self, term):
        with self._lock:
            entry = self._find(term)
            if not entry: return False
            entry["status"] = "rejected"; entry["updatedAt"] = time.time(); self._save(); return True
    def forget(self, term):
        with self._lock:
            old = len(self._entries); self._entries = [x for x in self._entries if x.get("term") != _clean(term,32)]
            if len(self._entries) == old: return False
            self._save(); return True
    def context(self, limit=12):
        with self._lock:
            items = [x for x in self._entries if x.get("status") == "confirmed" and x.get("meaning")]; items.sort(key=lambda x:int(x.get("count") or 0), reverse=True)
            rows = [f"- {x['term']}：{x['meaning']}" + (f"（{x['usage']}）" if x.get("usage") else "") for x in items[:max(1,min(30,int(limit)))]]
            return ("〖群聊黑话表〗只在自然语境下参考，不要刻意堆砌：\n" + "\n".join(rows)) if rows else ""
    def report(self, candidates=False):
        with self._lock:
            status = "candidate" if candidates else "confirmed"; items = [x for x in self._entries if x.get("status") == status]; items.sort(key=lambda x:int(x.get("count") or 0), reverse=True)
            if not items: return "暂无待确认黑话。" if candidates else "暂无已确认黑话。"
            return "\n".join(f"{x.get('term')}（{x.get('count',0)} 次）" + (f"：{x.get('meaning')}" if x.get("meaning") else "") for x in items[:20])
    def command(self, raw, privileged):
        text = _clean(raw,400); prefix = "^[!\uff01]\u9ed1\u8bdd"
        if not re.match(prefix, text): return None
        if not privileged: return "\u9ed1\u8bdd\u7ba1\u7406\u4ec5\u7fa4\u4e3b/\u7ba1\u7406\u5458\u53ef\u7528\u3002"
        body = re.sub(prefix + r"\s*", "", text).strip()
        if body in ("", "\u5217\u8868", "\u5019\u9009"): return "\u5f85\u786e\u8ba4\u9ed1\u8bdd:\\n" + self.report(True)
        if body in ("\u5df2\u786e\u8ba4", "\u786e\u8ba4\u5217\u8868"): return "\u5df2\u786e\u8ba4\u9ed1\u8bdd:\\n" + self.report(False)
        m = re.match(r"^(?:\u786e\u8ba4|\u8bb0\u4f4f)\s+([^=:]+?)(?:\s*[=:]\s*(.+))?$", body, re.S)
        if m: return "\u5df2\u786e\u8ba4\u5e76\u8bb0\u4f4f\u3002" if self.confirm(m.group(1), (m.group(2) or "").strip()) else "\u6ca1\u627e\u5230\u8fd9\u4e2a\u5019\u9009\u8bcd\u3002"
        m = re.match(r"^(?:\u62d2\u7edd|\u5ffd\u7565)\s+(.+)$", body, re.S)
        if m: return "\u5df2\u6807\u8bb0\u5ffd\u7565\u3002" if self.reject(m.group(1)) else "\u6ca1\u627e\u5230\u8fd9\u4e2a\u5019\u9009\u8bcd\u3002"
        m = re.match(r"^(?:\u5220\u9664|\u5fd8\u8bb0)\s+(.+)$", body, re.S)
        if m: return "\u5df2\u5220\u9664\u3002" if self.forget(m.group(1)) else "\u6ca1\u627e\u5230\u8fd9\u4e2a\u8bcd\u3002"
        return "\u7528\u6cd5: !\u9ed1\u8bdd\u5019\u9009, !\u9ed1\u8bdd\u786e\u8ba4 <\u8bcd> = <\u542b\u4e49>, !\u9ed1\u8bdd\u62d2\u7edd <\u8bcd>, !\u9ed1\u8bdd\u5220\u9664 <\u8bcd>。"

__all__ = ["SlangLearner"]
