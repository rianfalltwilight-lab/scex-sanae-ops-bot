#!/usr/bin/env python3
"""Small, append-only shared AI knowledge store for generic corrections.

The store is deliberately separate from server facts, chat history, and raw media.
Only short administrator-confirmed text and optional media fingerprints are kept.
"""
import hashlib
import json
import os
import re
import threading
import time


_SENSITIVE = re.compile(
    r"(?:password|passwd|token|secret|api[_ -]?key|密钥|密码|令牌|群号|qq号|qq\s*\d|"
    r"server\.properties|latest\.log|crash-reports?|world/|config/|[A-Za-z]:\\|/Users/|/home/)",
    re.I,
)
_SPLIT = re.compile(r"[\s,，。！？!?；;:：()（）《》<>「」【】\[\]{}]+")


def fingerprint_bytes(data):
    return hashlib.sha256(bytes(data)).hexdigest()


def fingerprint_text(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


class SharedKnowledge:
    def __init__(self, path, max_entries=2000, max_chars=800, max_matches=3):
        self.path = os.path.abspath(path)
        self.max_entries = max(1, int(max_entries))
        self.max_chars = max(80, int(max_chars))
        self.max_matches = max(1, int(max_matches))
        self._lock = threading.RLock()
        self._entries = {}
        self.load()

    @staticmethod
    def _key(topic, conclusion):
        return fingerprint_text(str(topic).strip() + "\n" + str(conclusion).strip())

    def _valid_text(self, value):
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if not value or len(value) > self.max_chars or _SENSITIVE.search(value):
            return None
        return value

    def load(self):
        with self._lock:
            self._entries = {}
            try:
                with open(self.path, encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            item = json.loads(line)
                        except (TypeError, ValueError):
                            continue
                        op = item.get("op", "upsert")
                        key = str(item.get("key") or "")
                        if op == "delete":
                            self._entries.pop(key, None)
                        elif op == "upsert" and key and item.get("topic") and item.get("conclusion"):
                            self._entries[key] = item
            except FileNotFoundError:
                return
            if len(self._entries) > self.max_entries:
                self._entries = dict(list(self._entries.items())[-self.max_entries:])

    def _append(self, item):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")

    def remember(self, topic, conclusion, media_fingerprints=(), source="admin"):
        topic = self._valid_text(topic)
        conclusion = self._valid_text(conclusion)
        if not topic or not conclusion:
            return False, "只允许保存不含敏感信息的短文本通用结论。"
        fps = [str(x).lower() for x in media_fingerprints if re.fullmatch(r"[0-9a-f]{64}", str(x).lower())]
        item = {
            "op": "upsert", "key": self._key(topic, conclusion), "topic": topic,
            "conclusion": conclusion, "media": fps[:3], "source": str(source)[:32],
            "createdAt": int(time.time()),
        }
        with self._lock:
            self._entries[item["key"]] = item
            if len(self._entries) > self.max_entries:
                oldest = next(iter(self._entries))
                self._entries.pop(oldest, None)
            self._append(item)
        return True, "已记住这条通用结论。"

    def delete(self, query):
        query = str(query or "").strip().casefold()
        if not query:
            return 0
        with self._lock:
            keys = [key for key, item in self._entries.items()
                    if query in item.get("topic", "").casefold()
                    or query in item.get("conclusion", "").casefold()]
            for key in keys:
                self._entries.pop(key, None)
                self._append({"op": "delete", "key": key, "deletedAt": int(time.time())})
            return len(keys)

    def search(self, query="", media_fingerprints=()):
        query = str(query or "").strip().casefold()
        wanted = {str(x).lower() for x in media_fingerprints}
        terms = {x for x in _SPLIT.split(query) if len(x) >= 2}
        scored = []
        with self._lock:
            for item in self._entries.values():
                topic = item.get("topic", "")
                conclusion = item.get("conclusion", "")
                media = set(item.get("media") or [])
                score = 100 if wanted & media else 0
                haystack = (topic + " " + conclusion).casefold()
                score += sum(3 for term in terms if term in haystack)
                if score:
                    scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], pair[1].get("createdAt", 0)), reverse=True)
        return [item for _, item in scored[:self.max_matches]]

    def status(self):
        with self._lock:
            return {"enabled": True, "path": self.path, "entries": len(self._entries),
                    "maxEntries": self.max_entries}
