#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Low-overhead, single-account social layer for Sanae.

This module deliberately has no model, subprocess, scheduler service, or new
QQ identity.  It keeps bounded group state, decides locally whether a message
is worth an AI turn, and exposes a small OneBot action client for future
social-only tool calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from onebot_utf8 import JSON_CONTENT_TYPE, json_bytes, utf8_text


_CQ_RE = re.compile(r"\[CQ:[^\]]*\]", re.I)
_QUESTION_RE = re.compile(r"[?？吗呢吧]|怎么|为什么|能不能|有没有|谁|哪|啥|什么")
_FOLLOWUP_RE = re.compile(r"^(?:继续|接着|然后呢|说话|你呢|怎么说|看这个|这个呢|那这个|再说|展开|具体点|接着说)[!！。\. ]*$", re.I)
_RELATED_RE = re.compile(r"服务器|服务端|整合包|模组|插件|玩家|在线|掉线|卡顿|卡死|延迟|TPS|炸服|崩溃|进度|成就|截图|蓝图|RCON|传送|效果|配方|矿|维度", re.I)
_COMMAND_RE = re.compile(r"^\s*[!！/]" )
_NOISE_RE = re.compile(r"^(?:嗯|哦|啊|哈哈|笑死|草|好|行|ok|好的|收到)[!！。\. ]*$", re.I)


def split_social_reply(text: Any, max_lines: int = 6, max_chars: int = 120) -> list[str]:
    """Split a model-chosen multiline social reply into bounded QQ messages.

    Newlines are treated as the model's decision to mirror a segmented cadence;
    ordinary one-line replies remain one message.  Bounds prevent accidental
    flooding when a model emits a long essay or formatted block.
    """
    value = str(text or "").strip()
    if not value:
        return []
    lines = [re.sub(r"\s+$", "", line).strip()
             for line in re.split(r"\r?\n+", value) if line.strip()]
    if 2 <= len(lines) <= max(1, int(max_lines)) and all(len(line) <= max_chars for line in lines):
        return lines
    return [value]


class SocialLite:
    """Bounded, file-backed group state and local reply gate."""

    PHASES = {"watching", "active", "cooldown", "sleeping"}

    def __init__(self, state_path: str | os.PathLike[str], activity_path: str | os.PathLike[str],
                 self_id: str = "0", max_recent: int = 30,
                 cooldown_seconds: float = 12.0, spontaneous_probability: float = 0.0,
                 ambient_reply_weight: float = 1.0):
        self.state_path = Path(state_path)
        self.activity_path = Path(activity_path)
        self.self_id = str(self_id)
        self.max_recent = max(5, int(max_recent))
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self.spontaneous_probability = min(1.0, max(0.0, float(spontaneous_probability)))
        # Weight for non-explicit ambient replies (questions/related chatter).
        # Explicit @/name calls, replies to Sanae and short follow-ups are not
        # probabilistically suppressed; this keeps direct interactions reliable.
        self.ambient_reply_weight = min(1.0, max(0.0, float(ambient_reply_weight)))
        self._lock = threading.RLock()
        self._timers: dict[str, threading.Timer] = {}
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._state: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            if raw.startswith("\ufeff"):
                raw = raw[1:]
            value = json.loads(raw)
            if isinstance(value, dict) and isinstance(value.get("groups"), dict):
                self._state = value["groups"]
        except (OSError, ValueError, TypeError):
            self._state = {}

    @staticmethod
    def _clean_text(raw: Any) -> str:
        text = str(raw or "")
        text = _CQ_RE.sub(" ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _group(self, group_id: Any) -> dict[str, Any]:
        key = str(group_id)
        group = self._state.setdefault(key, {
            "phase": "watching", "lastReplyAt": 0.0, "lastIncomingAt": 0.0,
            "replyCount": 0, "recent": [], "topics": [],
        })
        if group.get("phase") not in self.PHASES:
            group["phase"] = "watching"
        if not isinstance(group.get("recent"), list):
            group["recent"] = []
        if not isinstance(group.get("topics"), list):
            group["topics"] = []
        return group

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "groups": self._state}, ensure_ascii=False, indent=2) + "\n"
        tmp = self.state_path.with_name(self.state_path.name + f".{os.getpid()}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _activity(self, line: str) -> None:
        try:
            self.activity_path.parent.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[\r\n]+", " ", str(line))[:500]
            with self.activity_path.open("a", encoding="utf-8") as stream:
                stream.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {safe}\n")
            lines = self.activity_path.read_text(encoding="utf-8").splitlines()
            if len(lines) > 500:
                self.activity_path.write_text("\n".join(lines[-500:]) + "\n", encoding="utf-8")
        except OSError:
            pass

    def record_incoming(self, event: dict[str, Any]) -> None:
        gid = event.get("group_id")
        if gid is None:
            return
        original = str(event.get("raw_message") or event.get("message") or "")
        text = self._clean_text(original)
        if not text:
            return
        sender = event.get("sender") or {}
        item = {
            "sender": str(sender.get("card") or sender.get("nickname") or event.get("user_id") or "?"),
            "userId": str(event.get("user_id") or ""),
            "text": text[:240], "time": time.time(),
            "lineBreaks": min(12, original.count("\n")),
            "isSelf": str(event.get("user_id") or "") == self.self_id,
        }
        with self._lock:
            group = self._group(gid)
            group["lastIncomingAt"] = item["time"]
            group["recent"].append(item)
            group["recent"] = group["recent"][-self.max_recent:]
            self._save()

    def record_outgoing(self, group_id: Any, text: str, action: str = "reply") -> None:
        now = time.time()
        item = {"sender": "早苗", "userId": self.self_id, "text": self._clean_text(text)[:240],
                "time": now, "isSelf": True, "action": action}
        with self._lock:
            group = self._group(group_id)
            group["lastReplyAt"] = now
            group["replyCount"] = int(group.get("replyCount") or 0) + 1
            group["phase"] = "cooldown"
            group["recent"].append(item)
            group["recent"] = group["recent"][-self.max_recent:]
            self._save()
        self._activity(f"group:{group_id} outgoing[{action}]: {item['text']}")

    def _score(self, event: dict[str, Any], group: dict[str, Any]) -> tuple[int, str]:
        raw = str(event.get("raw_message") or event.get("message") or "")
        text = self._clean_text(raw)
        if not text or _COMMAND_RE.match(text) or _NOISE_RE.match(text):
            return 0, "noise-or-command"
        if str(event.get("user_id") or "") == self.self_id:
            return 0, "self"
        score = 0
        reasons: list[str] = []
        if re.search(r"\[CQ:at,qq=" + re.escape(self.self_id) + r"(?:,[^\]]*)?\]", raw, re.I) or "早苗" in text:
            score += 5; reasons.append("direct")
        reply = event.get("reply")
        if isinstance(reply, dict) and str(reply.get("user_id") or "") == self.self_id:
            score += 4; reasons.append("reply")
        if _QUESTION_RE.search(text):
            score += 2; reasons.append("question")
        # 短促追问只有在早苗刚回复过时才触发，避免把普通“继续/说话”刷成 AI 回复。
        last_reply = float(group.get("lastReplyAt") or 0)
        if _FOLLOWUP_RE.match(text) and last_reply and time.time() - last_reply <= 90:
            score += 3; reasons.append("followup")
        if _RELATED_RE.search(text):
            recent_text = " ".join(str(x.get("text") or "") for x in (group.get("recent") or [])[-8:])
            # 相关话题只在最近上下文也属于服务器讨论时接话，避免见词就抢答。
            if _RELATED_RE.search(recent_text) and len(text) >= 4:
                score += 2; reasons.append("related")
        if group.get("phase") == "active":
            score += 1; reasons.append("active-topic")
        if score == 0 and self.spontaneous_probability > 0:
            # Stable per-message sampling avoids a global RNG loop and keeps idle cost at zero.
            seed = f"{event.get('time') or time.time()}:{event.get('user_id')}:{text}"
            sample = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
            if sample < self.spontaneous_probability:
                score = 1; reasons.append("spontaneous")
        return score, "+".join(reasons) or "ordinary"

    def should_reply(self, event: dict[str, Any]) -> tuple[bool, str]:
        gid = event.get("group_id")
        if gid is None:
            return False, "not-group"
        with self._lock:
            group = self._group(gid)
            score, reason = self._score(event, group)
            now = time.time()
            last = float(group.get("lastReplyAt") or 0)
            if (now - last < self.cooldown_seconds and
                    not any(tag in reason for tag in ("direct", "reply", "followup"))):
                return False, "cooldown"
            if "related" in reason and now - last < max(30.0, self.cooldown_seconds):
                return False, "related-cooldown"
            explicit = any(tag in reason for tag in ("direct", "reply", "followup"))
            if score >= 2 and not explicit and self.ambient_reply_weight < 1.0:
                # Stable per-message sampling keeps the tenfold reduction
                # deterministic for a given event and avoids a global RNG.
                raw = str(event.get("raw_message") or event.get("message") or "")
                seed = f"ambient:{gid}:{event.get('time') or ''}:{event.get('user_id') or ''}:{raw}"
                sample = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
                if sample >= self.ambient_reply_weight:
                    return False, "ambient-weight"
            return score >= 2, reason

    def _context(self, gid: Any, pending: list[dict[str, Any]]) -> str:
        with self._lock:
            group = self._group(gid)
            recent = list(group.get("recent") or [])[-self.max_recent:]
        lines = [f"{x.get('sender', '?')}：{x.get('text', '')}" for x in recent]
        for item in pending:
            line = f"{item.get('sender', '?')}：{self._clean_text(item.get('raw_message'))}"
            if line not in lines:
                lines.append(line)
        return "\n".join(lines[-self.max_recent:])

    def expression_style_hint(self, gid: Any, event: dict[str, Any] | None = None) -> str:
        """Return a model-facing style signal, never a canned reply instruction.

        The signal is derived from the current speaker's recent cadence.  It is
        intentionally descriptive and asks the model to decide whether mirroring
        would help, so no specific phrase or fixed output shape is hard-coded.
        """
        uid = str((event or {}).get("user_id") or "")
        if not uid:
            return ""
        now = time.time()
        with self._lock:
            recent = list(self._group(gid).get("recent") or [])
        entries = [item for item in recent
                   if str(item.get("userId") or "") == uid
                   and not item.get("isSelf")
                   and now - float(item.get("time") or now) <= 8.0]
        if not entries:
            return ""
        entries = entries[-12:]
        texts = [str(item.get("text") or "").strip() for item in entries]
        short = [text for text in texts if text and len(text) <= 12]
        line_breaks = sum(int(item.get("lineBreaks") or 0) for item in entries)
        first_time = float(entries[0].get("time") or now)
        last_time = float(entries[-1].get("time") or now)
        burst = len(short) >= 3 and len(short) == len([text for text in texts if text])
        if not burst and not (line_breaks >= 1 and len(texts) == 1 and short):
            return ""
        elapsed = max(0.0, min(8.0, last_time - first_time))
        average = sum(len(text) for text in short) / max(1, len(short))
        if line_breaks:
            shape = f"单条消息含 {line_breaks} 个换行"
        else:
            shape = f"连续 {len(short)} 条短消息"
        return ("〖表达风格信号〗当前说话者刚才呈现了" + shape +
                f"（平均每段约 {average:.1f} 字，约 {elapsed:.1f} 秒）。"
                "这只是节奏参考，不是内容指令；请你自行判断是否镜像这种分段/短行表达。"
                "只有在能增强自然互动、且不损害清晰度时才采用，不要机械复制段数或字数。")

    def maybe_schedule(self, event: dict[str, Any], callback: Callable[[dict[str, Any], str], Any],
                       delay_seconds: float = 2.5) -> tuple[bool, str]:
        """Record an event and schedule one debounced callback per group."""
        self.record_incoming(event)
        allowed, reason = self.should_reply(event)
        if not allowed:
            return False, reason
        gid = str(event.get("group_id"))
        with self._lock:
            self._pending.setdefault(gid, []).append(dict(event))
            if gid in self._timers:
                return True, "coalesced"
            timer = threading.Timer(max(0.1, delay_seconds), self._flush, args=(gid, callback))
            timer.daemon = True
            self._timers[gid] = timer
            timer.start()
        self._activity(f"group:{gid} scheduled social reply [{reason}]")
        return True, reason

    def _flush(self, gid: str, callback: Callable[[dict[str, Any], str], Any]) -> None:
        with self._lock:
            pending = self._pending.pop(gid, [])
            self._timers.pop(gid, None)
        if not pending:
            return
        latest = dict(pending[-1])
        latest["raw_message"] = self._clean_text(latest.get("raw_message"))
        context = self._context(gid, pending)
        try:
            callback(latest, context)
        except Exception as exc:  # callback owns user-facing error handling
            self._activity(f"group:{gid} social callback error: {type(exc).__name__}")

    def status(self, group_id: Any) -> dict[str, Any]:
        with self._lock:
            group = self._group(group_id)
            return {"phase": group.get("phase"), "recent": len(group.get("recent") or []),
                    "lastReplyAt": group.get("lastReplyAt", 0), "replyCount": group.get("replyCount", 0)}


class OneBotActions:
    """Small, bounded OneBot v11 action wrapper; no credentials are stored here."""

    def __init__(self, api_url: str, token: str = "", timeout: float = 15.0):
        self.api_url = str(api_url).rstrip("/")
        self.token = str(token or "")
        self.timeout = max(2.0, float(timeout))

    def call(self, action: str, params: dict[str, Any] | None = None) -> Any:
        headers = {"Content-Type": JSON_CONTENT_TYPE}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.api_url + "/" + action,
                                         data=json_bytes(params or {}), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(utf8_text(response.read()))
        if payload.get("status") != "ok" or int(payload.get("retcode") or 0) != 0:
            raise RuntimeError(f"OneBot {action} failed: retcode={payload.get('retcode')}")
        return payload.get("data")

    def status(self) -> Any:
        return self.call("get_status")

    def login_info(self) -> Any:
        return self.call("get_login_info")

    def groups(self) -> Any:
        return self.call("get_group_list")

    def members(self, group_id: int | str) -> Any:
        return self.call("get_group_member_list", {"group_id": int(group_id)})

    def history(self, group_id: int | str, count: int = 30) -> Any:
        return self.call("get_group_msg_history", {"group_id": int(group_id), "count": max(1, min(100, int(count)))})

    def message(self, message_id: int | str) -> Any:
        return self.call("get_msg", {"message_id": int(message_id)})

    def send_group(self, group_id: int | str, message: Any) -> Any:
        return self.call("send_group_msg", {"group_id": int(group_id), "message": message})

    def send_private(self, user_id: int | str, message: Any) -> Any:
        return self.call("send_private_msg", {"user_id": int(user_id), "message": message})

    def poke(self, user_id: int | str, group_id: int | str | None = None) -> Any:
        params: dict[str, Any] = {"user_id": int(user_id)}
        if group_id is not None:
            params["group_id"] = int(group_id)
        return self.call("send_poke", params)

    def mute(self, group_id: int | str, user_id: int | str, duration: int = 300) -> Any:
        return self.call("set_group_ban", {
            "group_id": int(group_id),
            "user_id": int(user_id),
            "duration": max(0, min(30 * 24 * 60 * 60, int(duration))),
        })


__all__ = ["SocialLite", "OneBotActions"]
