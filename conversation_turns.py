"""Bounded, in-memory coordination for explicitly selected text-only chat turns.

The caller keeps QA, operations, recall, search and media on their original route.
This module never invokes a model, sends a message or cancels a running tool.
"""
from __future__ import annotations

import copy
import contextvars
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable
from sanae_identity import BOT_NAME_PATTERN


_CORRECTION = re.compile(r'^(?:不是[，,：:\s]|不对[，,。！!\s]|等等[，,。！!\s]|等一下[，,。！!\s]|'
                         r'我(?:刚才)?说错了|刚才(?:说错|那句|说的)|更正(?:一下)?[，,：:\s]|'
                         r'补充(?:一下)?[，,：:\s]|准确地说|我是说|我的意思是|改成|应该是)')
_CQ = re.compile(r'\[CQ:[^\]]*\]')


def _message_parts(event: dict[str, Any]):
    """Return plain text and the quoted message ID; reject non-text payloads."""
    message = event.get('message')
    refs = []
    if isinstance(message, list):
        texts = []
        for segment in message:
            if not isinstance(segment, dict) or not isinstance(segment.get('data'), dict):
                return None
            kind, data = segment.get('type'), segment['data']
            if kind == 'text':
                texts.append(str(data.get('text') or ''))
            elif kind == 'reply':
                refs.append(str(data.get('id') or ''))
            elif kind != 'at':
                return None
        text = ''.join(texts).strip()
    else:
        raw = str(event.get('raw_message') or message or '')
        for tag in _CQ.findall(raw):
            if not re.match(r'\[CQ:(?:at|reply),', tag):
                return None
        refs = re.findall(r'\[CQ:reply,id=(-?\d+)(?:,|\])', raw)
        text = _CQ.sub('', raw).strip()
    reply = event.get('reply')
    if isinstance(reply, dict):
        ref = str(reply.get('message_id') or reply.get('id') or '')
        if ref:
            refs.append(ref)
    if len(set(refs)) > 1 or any(not re.fullmatch(r'-?\d+', ref) for ref in refs):
        return None
    clean = re.sub(r'^@?' + BOT_NAME_PATTERN + r'[\s，,：:]*', '', text, flags=re.I).strip()
    if not clean or len(text) > 2000 or clean.startswith(('!', '！', '/')):
        return None
    return clean, refs[0] if refs else ''


@dataclass(eq=False)
class TurnToken:
    _owner: 'ConversationTurns'
    _key: tuple[str, str, str]
    version: int
    source_ids: tuple[str, ...]
    created_at: float
    merged_event: dict[str, Any] = field(default_factory=dict, repr=False)
    _finished: bool = False
    _protected: bool = False

    def current(self) -> bool:
        """Check immediately before each chat send and before saving history."""
        with self._owner._lock:
            return self._owner._current_locked(self)

    def commit(self, callback: Callable, *args, **kwargs) -> bool:
        """Atomically guard a short local history write; never use for network I/O."""
        with self._owner._lock:
            if not self._owner._current_locked(self):
                return False
            callback(*args, **kwargs)
            return True

    def commit_if_current(self, callback: Callable) -> bool:
        return self.commit(callback)

    def protect_tools(self) -> bool:
        """Call just before executing actual tool calls, not on tool declaration.

        A stale chat request cannot execute tools. Once execution starts, later
        chat messages cannot cancel it or replay its prompt in a correction.
        """
        with self._owner._lock:
            if not self._owner._current_locked(self):
                return False
            if not self._protected:
                if len(self._owner._protected) >= self._owner.max_lanes:
                    return False
                self._protected = True
                self._owner._protected[self.version] = self
            return True

    def finish(self) -> None:
        with self._owner._lock:
            self._finished = True
            self._owner._protected.pop(self.version, None)
            lane = self._owner._lanes.get(self._key)
            if lane and lane.token is self:
                lane.inflight = []
                lane.token = None


@dataclass
class _Lane:
    version: int
    first_at: float
    updated_at: float
    pending: list[dict[str, Any]] = field(default_factory=list)
    inflight: list[dict[str, Any]] = field(default_factory=list)
    timer: Any = None
    token: TurnToken | None = None
    dispatch: Callable | None = None
    context: Any = None


class ConversationTurns:
    def __init__(self, allowed_group_ids=(), *, quiet_seconds=1.0, max_wait_seconds=3.0,
                 ttl_seconds=300.0, max_lanes=128, max_messages=12,
                 clock=time.monotonic, timer_factory=threading.Timer):
        self.allowed_groups = {str(value) for value in allowed_group_ids}
        self.quiet_seconds = max(0.8, min(1.2, float(quiet_seconds)))
        self.max_wait_seconds = max(self.quiet_seconds, min(5.0, float(max_wait_seconds)))
        self.ttl_seconds = max(10.0, float(ttl_seconds))
        self.max_lanes = max(1, int(max_lanes))
        self.max_messages = max(2, min(24, int(max_messages)))
        self._clock, self._timer_factory = clock, timer_factory
        self._lock = threading.RLock()
        self._lanes: OrderedDict[tuple[str, str, str], _Lane] = OrderedDict()
        self._seen: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._protected: dict[int, TurnToken] = {}
        self._serial = 0
        self._closed = False
        self.dispatch_failures = 0

    def _current_locked(self, token):
        if token._finished or self._clock() - token.created_at >= self.ttl_seconds:
            return False
        if token._protected:
            return self._protected.get(token.version) is token
        lane = self._lanes.get(token._key)
        return bool(not self._closed and lane and lane.token is token and
                    lane.version == token.version)

    def _drop_locked(self, key):
        lane = self._lanes.pop(key, None)
        if lane and lane.timer:
            lane.timer.cancel()

    def _prune_locked(self, now):
        for key, lane in list(self._lanes.items()):
            if now - lane.updated_at >= self.ttl_seconds:
                self._drop_locked(key)
        for version, token in list(self._protected.items()):
            if token._finished or now - token.created_at >= self.ttl_seconds:
                self._protected.pop(version, None)
        while self._seen and (now - next(iter(self._seen.values())) >= self.ttl_seconds or
                              len(self._seen) > self.max_lanes * 16):
            self._seen.popitem(last=False)

    def submit(self, event: dict[str, Any], dispatch: Callable[[dict[str, Any]], Any], *,
               chat_only=False, directed=True) -> bool:
        """Schedule selected chat; False means caller should retain its old route.

        True also covers duplicate message IDs already accepted. Undirected
        input is accepted only for an explicit correction of one active lane.
        A queue callback returning False is treated as failed dispatch.
        """
        if not chat_only or not isinstance(event, dict) or event.get('message_type') != 'group':
            return False
        gid, uid, mid = (str(event.get(key) or '') for key in ('group_id', 'user_id', 'message_id'))
        if gid not in self.allowed_groups or not uid.isdigit() or not re.fullmatch(r'-?\d+', mid):
            return False
        parsed = _message_parts(event)
        if parsed is None:
            return False
        clean, reference = parsed
        correction = bool(_CORRECTION.match(clean))
        if not directed:
            message = event.get('message')
            ats = ([str(seg['data'].get('qq') or '') for seg in message if seg.get('type') == 'at']
                   if isinstance(message, list) else
                   re.findall(r'\[CQ:at,qq=([^,\]]+)', str(event.get('raw_message') or message or '')))
            if any(qq != str(event.get('self_id') or '') for qq in ats):
                return False
        now = self._clock()
        with self._lock:
            if self._closed:
                return False
            self._prune_locked(now)
            if (gid, mid) in self._seen:
                return True
            key = (gid, uid, reference)
            if not directed:
                if not correction:
                    return False
                candidates = [k for k, row in self._lanes.items() if k[:2] == (gid, uid)
                              and (not reference or k[2] == reference)
                              and (row.pending or (row.token and row.token.current() and
                                                   not row.token._protected))]
                if len(candidates) != 1:
                    return False
                key = candidates[0]
            lane = self._lanes.get(key)
            pending = list(lane.pending) if lane else []
            if (not pending and lane and lane.token and lane.token.current() and
                    not lane.token._protected and
                    (correction or (directed and now - lane.token.created_at <= 8.0))):
                pending = list(lane.inflight)
            if len(pending) >= self.max_messages:
                return False
            # Source events remain immutable; internal token metadata is never copied.
            item = copy.deepcopy({k: v for k, v in event.items() if not k.startswith('_conversation_')})
            pending.append(item)
            if lane is None:
                while len(self._lanes) >= self.max_lanes:
                    self._drop_locked(next(iter(self._lanes)))
                lane = _Lane(0, now, now)
                self._lanes[key] = lane
            if not lane.pending:
                lane.first_at = now
            if lane.timer:
                lane.timer.cancel()
            self._serial += 1
            lane.version, lane.updated_at = self._serial, now
            lane.pending = pending
            # A correction amends the existing invocation. Keep its route and
            # privileges (for example Social Lite's tool-free callback), rather
            # than replacing them with the caller's generic correction route.
            if directed or lane.dispatch is None:
                lane.dispatch = dispatch
            lane.context = contextvars.copy_context()
            self._lanes.move_to_end(key)
            self._seen[(gid, mid)] = now
            self._prune_locked(now)
            delay = max(0.0, min(self.quiet_seconds, lane.first_at + self.max_wait_seconds - now))
            version = lane.version
            timer = self._timer_factory(delay, lambda: self._flush(key, version))
            timer.daemon = True
            lane.timer = timer
            timer.start()
            return True

    def _flush(self, key, version):
        with self._lock:
            lane = self._lanes.get(key)
            if self._closed or not lane or lane.version != version or not lane.pending:
                return
            if self._clock() - lane.updated_at >= self.ttl_seconds:
                self._drop_locked(key)
                return
            pending = lane.pending
            merged = copy.deepcopy(pending[-1])
            merged['raw_message'] = '\n'.join(str(row.get('raw_message') or
                                                   _message_parts(row)[0]) for row in pending)
            # Keep genuine at/reply segments for the bridge invocation recheck.
            if all(isinstance(row.get('message'), list) for row in pending):
                merged['message'] = []
                for index, row in enumerate(pending):
                    if index:
                        merged['message'].append({'type': 'text', 'data': {'text': '\n'}})
                    merged['message'].extend(copy.deepcopy(row['message']))
            else:
                merged['message'] = merged['raw_message']
            source_ids = tuple(str(row['message_id']) for row in pending)
            token = TurnToken(self, key, version, source_ids, self._clock(), merged)
            merged['_conversation_turn'] = token
            merged['_conversation_source_ids'] = source_ids
            lane.pending, lane.inflight, lane.token, lane.timer = [], pending, token, None
            dispatch, context = lane.dispatch, lane.context
        try:
            if context.run(dispatch, merged) is False:
                token.finish()
                with self._lock:
                    self.dispatch_failures += 1
        except Exception:
            token.finish()
            with self._lock:
                self.dispatch_failures += 1

    def forget(self, group_id, user_id=None):
        """Invalidate queued/generating chat, preserving already started tools."""
        gid, uid = str(group_id), None if user_id is None else str(user_id)
        with self._lock:
            for key in list(self._lanes):
                if key[0] == gid and (uid is None or key[1] == uid):
                    self._drop_locked(key)

    def retract(self, group_id, message_id):
        gid, mid = str(group_id), str(message_id)
        with self._lock:
            for key, lane in list(self._lanes.items()):
                rows = lane.pending + lane.inflight
                if key[0] == gid and any(str(row.get('message_id')) == mid for row in rows):
                    self._drop_locked(key)

    def close(self):
        with self._lock:
            self._closed = True
            for key in list(self._lanes):
                self._drop_locked(key)


__all__ = ['ConversationTurns', 'TurnToken']
