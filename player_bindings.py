#!/usr/bin/env python3
"""Persistent QQ <-> Minecraft player binding with conservative validation."""
import json
import os
import re
import tempfile
import threading
import time

NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")


class PlayerBindings:
    def __init__(self, path, require_seen=False):
        self.path = os.path.abspath(path)
        self.require_seen = bool(require_seen)
        self._lock = threading.RLock()
        self._by_qq = {}
        self._by_name = {}
        self.load()

    def load(self):
        with self._lock:
            self._by_qq = {}
            self._by_name = {}
            try:
                with open(self.path, encoding="utf-8") as stream:
                    data = json.load(stream)
            except (FileNotFoundError, ValueError, OSError):
                return
            rows = data if isinstance(data, list) else data.get("bindings", [])
            for row in rows:
                qq, name = str(row.get("qq", "")), str(row.get("name", ""))
                if qq and NAME_RE.fullmatch(name):
                    item = {"qq": qq, "name": name, "seen": bool(row.get("seen")),
                            "updatedAt": int(row.get("updatedAt") or 0)}
                    self._by_qq[qq] = item
                    self._by_name[name.casefold()] = item

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {"version": 1, "bindings": list(self._by_qq.values())}
        fd, temp_path = tempfile.mkstemp(prefix=".qq-player-binds-", dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def bind(self, qq, name, seen=True):
        qq, name = str(qq).strip(), str(name).strip()
        if not qq or not NAME_RE.fullmatch(name):
            return False, "游戏 ID 只能是 1-16 位字母、数字或下划线。"
        if self.require_seen and not seen:
            return False, "这个游戏 ID 尚未在服务器出现，暂不能绑定。"
        with self._lock:
            old = self._by_qq.get(qq)
            occupied = self._by_name.get(name.casefold())
            if occupied and occupied["qq"] != qq:
                return False, "这个游戏 ID 已经被其他 QQ 绑定。"
            if old:
                self._by_name.pop(old["name"].casefold(), None)
            item = {"qq": qq, "name": name, "seen": bool(seen), "updatedAt": int(time.time())}
            self._by_qq[qq] = item
            self._by_name[name.casefold()] = item
            self._save()
        return True, f"已绑定：{name}。"

    def unbind(self, qq):
        with self._lock:
            old = self._by_qq.pop(str(qq), None)
            if not old:
                return False, "你还没有绑定游戏 ID。"
            self._by_name.pop(old["name"].casefold(), None)
            self._save()
            return True, f"已解绑：{old['name']}。"

    def get_qq(self, qq):
        with self._lock:
            return dict(self._by_qq[str(qq)]) if str(qq) in self._by_qq else None

    def get_name(self, name):
        with self._lock:
            item = self._by_name.get(str(name).casefold())
            return dict(item) if item else None

    def mark_seen(self, name):
        with self._lock:
            item = self._by_name.get(str(name).casefold())
            if item:
                item["seen"] = True
                self._save()

    def list(self):
        with self._lock:
            return [dict(item) for item in self._by_qq.values()]
