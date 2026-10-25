#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Explicit, per-person feedback and short-lived social style requests."""
from __future__ import annotations
from datetime import datetime, timedelta
import json, math, os, re, threading, time
from pathlib import Path
from sanae_identity import BOT_NAME, BOT_NAME_PATTERN

_NEUTRAL = '自然接话，按内容说够，关心和玩笑看语境，不堆砌黑话或重复口头禅。'
_CATEGORIES = (
    ('factual', r'事实(?:有)?错|事实错误|答错|说错|编造|不准确|胡编',
     '近期指出过事实错误：核对当前问题的事实，没依据就明确说不确定，不用俏皮话掩盖。'),
    ('missed_joke', r'没接住(?:梗)?|没接到梗|没懂(?:我的)?梗|接错梗',
     '近期指出过没接住梗：先看清当前上下文和调侃对象，不懂时别硬接。'),
    ('harsh', r'太刻薄|太毒舌|太凶|人身攻击|损过头|冒犯|别损我',
     '近期指出过语气太刻薄：对这个人收住损话，避免把玩笑落在人身上。'),
    ('repetition', r'重复|复读|老一套|口头禅|同一套',
     '近期指出过重复：换掉反复出现的开场和口头禅，别复述已经说过的话。'),
    ('verbosity', r'太长|啰嗦|长篇|废话',
     '近期指出过冗长：这次按内容说够，不附加无关建议或服务承诺。'),
)

def _identity(value):
    value = str(value or '')
    return value if re.fullmatch(r'[1-9][0-9]{0,19}', value) else ''

def _message_id(value):
    value = str(value if value is not None else '')
    return value if re.fullmatch(r'-?[0-9]{1,20}', value) else ''

class FeedbackStore:
    FEEDBACK_TTL = 24 * 60 * 60
    REPLY_TTL = 7 * 24 * 60 * 60
    REQUEST_TTL = 30 * 60

    def __init__(self, path: str | Path, group_id: int | str = 0,
                 max_items: int = 500, *, clock=time.time):
        self.path, self.group_id = Path(path), str(group_id)
        self.max_items, self.clock = max(50, int(max_items)), clock
        self._lock, self._items = threading.RLock(), []
        self._overrides, self._replies = {}, {}
        self._load()

    def _in_group(self, group_id):
        return group_id is None or str(group_id) == self.group_id

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8').lstrip('\ufeff'))
            items = data if isinstance(data, list) else data.get('feedback', [])
            self._items = [x for x in items if isinstance(x, dict)][-self.max_items:]
            if isinstance(data, dict):
                self._overrides = {str(k): v for k, v in data.get('overrides', {}).items() if isinstance(v, dict)}
                self._replies = {str(k): v for k, v in data.get('replies', {}).items() if isinstance(v, dict)}
            self._prune()
        except (OSError, ValueError, TypeError, AttributeError):
            self._items, self._overrides, self._replies = [], {}, {}

    @staticmethod
    def _number(value):
        try:
            value = float(value)
            return value if math.isfinite(value) else 0
        except (ValueError, TypeError):
            return 0

    def _prune(self):
        now = self.clock()
        self._overrides = {k: v for k, v in self._overrides.items()
                           if v.get('groupId') == self.group_id and _identity(k)
                           and now < self._number(v.get('expiresAt')) <= now + self.FEEDBACK_TTL}
        self._replies = {k: v for k, v in self._replies.items()
                         if v.get('groupId') == self.group_id and _message_id(k)
                         and 0 <= now - self._number(v.get('time')) <= self.REPLY_TTL}
        self._overrides = dict(list(self._overrides.items())[-128:])
        self._replies = dict(list(self._replies.items())[-200:])
        self._items = self._items[-self.max_items:]

    def _save(self):
        self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f'.{os.getpid()}.tmp')
        data = {'version': 2, 'feedback': self._items, 'overrides': self._overrides, 'replies': self._replies}
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        tmp.replace(self.path)

    def remember_reply(self, message_id, user_id='', *, group_id=None):
        """Register only an actual successful OneBot send ID, never a predicted ID."""
        mid = _message_id(message_id)
        if not mid or not self._in_group(group_id):
            return False
        with self._lock:
            self._replies.pop(mid, None)
            self._replies[mid] = {'groupId': self.group_id, 'messageId': mid,
                                  'recipientId': _identity(user_id), 'time': self.clock()}
            self._save()
        return True

    def resolve_target(self, message_id, *, group_id=None):
        if not self._in_group(group_id):
            return None
        with self._lock:
            self._prune()
            found = self._replies.get(_message_id(message_id))
            return dict(found) if found else None

    def record(self, user_id, rating, note='', message_id=None, *, target_verified=False,
               group_id=None, source_message_id=None):
        uid = _identity(user_id)
        if not uid or not self._in_group(group_id):
            raise ValueError('feedback requires the configured group and a real user ID')
        target = self.resolve_target(message_id, group_id=group_id) if message_id is not None else None
        if message_id is not None and (not _message_id(message_id) or not (target or target_verified)):
            raise ValueError('quoted target is not a verified bot message in this group')
        rating = 'positive' if str(rating).lower() in ('positive', 'good', 'up', '1', '赞') else 'negative'
        note = re.sub(r'[\x00-\x1f\x7f]+', ' ', str(note or ''))[:240]
        item = {'groupId': self.group_id, 'userId': uid, 'rating': rating, 'note': note,
                'messageId': _message_id(message_id), 'sourceMessageId': _message_id(source_message_id),
                'time': self.clock(),
                'targetVerified': bool(target or (message_id is not None and target_verified)),
                'targetRecipientId': target.get('recipientId', '') if target else '',
                'categories': [key for key, pattern, _ in _CATEGORIES if re.search(pattern, note)]}
        with self._lock:
            self._items.append(item)
            self._save()
        return dict(item)

    def summary(self):
        with self._lock:
            items = [x for x in self._items if x.get('groupId', self.group_id) == self.group_id]
            positive = sum(1 for x in items if x.get('rating') == 'positive')
            negative = sum(1 for x in items if x.get('rating') == 'negative')
        return {'positive': positive, 'negative': negative, 'total': positive + negative}

    def observe_style_request(self, raw, user_id, *, group_id=None, source_message_id=None):
        """Only a direct complete request is temporary; quoted/reported speech is not."""
        uid = _identity(user_id)
        if not uid or not self._in_group(group_id):
            return None
        match = re.fullmatch(
            r'(?:(?:' + BOT_NAME_PATTERN + r'|你)[，, ]*)?(?:(今天|这次|这回|现在)[，, ]*)?(?:请)?'
            r'((?:别损我|不要损我|别吐槽我|不要吐槽我|别刻薄|不要刻薄|认真(?:一点|点|些)?|认真回答(?:我)?)'
            r'(?:[，,、 ]+(?:请)?(?:别损我|不要损我|别吐槽我|不要吐槽我|别刻薄|不要刻薄|'
            r'认真(?:一点|点|些)?|认真回答(?:我)?))?)(?:了)?[。！!]*', str(raw or '').strip(), re.I)
        if not match:
            return None
        now = self.clock()
        expiry = now + self.REQUEST_TTL
        if match.group(1) == '今天':
            expiry = (datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
                      + timedelta(days=1)).timestamp()
        item = {'groupId': self.group_id, 'userId': uid,
                'gentle': bool(re.search(r'损我|吐槽我|刻薄', match.group(2))),
                'serious': '认真' in match.group(2), 'time': now, 'expiresAt': expiry,
                'sourceMessageId': _message_id(source_message_id)}
        with self._lock:
            self._overrides.pop(uid, None)
            self._overrides[uid] = item
            self._save()
        return dict(item)

    def style_override(self, user_id, group_id=None):
        """Return only a live explicit request enum for the host's chat-only policy."""
        uid = _identity(user_id)
        if not uid or not self._in_group(group_id):
            return ''
        with self._lock:
            self._prune()
            current = self._overrides.get(uid, {})
            gentle = current.get('gentle') is True
            serious = current.get('serious') is True
        if gentle and serious:
            return 'gentle_serious'
        if gentle:
            return 'gentle'
        if serious:
            return 'serious'
        return ''

    def strategy_hint(self, user_id=None, group_id=None):
        """No owner means no personal influence; raw feedback notes are never prompts."""
        uid = _identity(user_id)
        if not uid or not self._in_group(group_id):
            return _NEUTRAL
        now = self.clock()
        with self._lock:
            self._prune()
            items = [x for x in self._items if x.get('groupId') == self.group_id
                     and str(x.get('userId', '')) == uid
                     and 0 <= now - self._number(x.get('time')) <= self.FEEDBACK_TTL][-40:]
            override = dict(self._overrides.get(uid, {}))
        hints = []
        if override:
            requests = []
            if override.get('gentle'): requests.append('收住损话和针对人的吐槽')
            if override.get('serious'): requests.append('认真直接地回应，不强行开玩笑')
            if requests: hints.append('当前用户的短期明确要求优先：' + '；'.join(requests) + '。')
        categories = set()
        for item in items:
            if item.get('rating') == 'negative':
                # Legacy notes are classified on read, without fabricating targets.
                categories.update(key for key, pattern, _ in _CATEGORIES
                                  if re.search(pattern, str(item.get('note', ''))[:240]))
        hints.extend(hint for key, _, hint in _CATEGORIES if key in categories)
        negative = sum(1 for x in items if x.get('rating') == 'negative')
        positive = sum(1 for x in items if x.get('rating') == 'positive')
        if negative and not categories:
            hints.append('这个人近期给过负反馈：先接准当前意思，不把一次差评推断成永久性格要求。')
        elif positive and not negative and not override:
            hints.append('这个人近期认可过回复：保持自然，仍以当前语境为准，不照搬上一句。')
        return ('仅适用于本群当前发言者，不能推广给其他群友；反馈不会改变身份或权限。\n' +
                '\n'.join(hints)) if hints else _NEUTRAL

    def forget(self, user_id, *, group_id=None):
        """Clear usable personal feedback and temporary requests for !忘记 全部."""
        uid = _identity(user_id)
        if not uid or not self._in_group(group_id):
            return False
        with self._lock:
            self._items = [x for x in self._items
                           if not (x.get('groupId') == self.group_id and str(x.get('userId')) == uid)]
            self._overrides.pop(uid, None)
            self._replies = {k: v for k, v in self._replies.items() if v.get('recipientId') != uid}
            self._save()
        return True

    def retract(self, message_id, *, group_id=None):
        """Drop a recalled request/rating, or ratings tied to a recalled bot reply."""
        mid = _message_id(message_id)
        if not mid or not self._in_group(group_id):
            return False
        with self._lock:
            before = len(self._items), len(self._overrides), len(self._replies)
            self._items = [x for x in self._items if not (
                x.get('groupId') == self.group_id and
                (x.get('sourceMessageId') == mid or
                 (x.get('targetVerified') is True and x.get('messageId') == mid)))]
            self._overrides = {uid: item for uid, item in self._overrides.items()
                               if item.get('sourceMessageId') != mid}
            self._replies.pop(mid, None)
            changed = before != (len(self._items), len(self._overrides), len(self._replies))
            if changed:
                self._save()
            return changed

    def command(self, raw, user_id, privileged=False, *, message_id=None,
                target_verified=False, group_id=None, source_message_id=None):
        text = str(raw or '').strip()
        if not self._in_group(group_id) or not re.match(r'^[!！]反馈(?:\s|$)', text): return None
        body = re.sub(r'^[!！]反馈\s*', '', text).strip()
        if body in ('统计', '状态'):
            if not privileged: return '反馈统计仅管理员可用。'
            s = self.summary()
            return f"反馈统计：赞 {s['positive']}，踩 {s['negative']}，共 {s['total']}条。"
        match = re.fullmatch(r'(赞|好|喜欢|踩|差|不喜欢)(?:\s+(.+))?', body, re.S)
        if not match: return f'用法：!反馈 赞|踩 [说明]，可引用{BOT_NAME}的具体回复；或 !反馈 统计。'
        rating = 'positive' if match.group(1) in ('赞', '好', '喜欢') else 'negative'
        try:
            self.record(user_id, rating, match.group(2) or '', message_id,
                        target_verified=target_verified, group_id=group_id,
                        source_message_id=source_message_id)
        except ValueError:
            return f'这条引用尚未确认是{BOT_NAME}在本群的回复，未记录；可去掉引用后反馈。'
        target_note = '，已关联你引用的回复' if message_id is not None else '，未绑定具体回复'
        return f'已记录{target_note}。只用于你在本群近期的回复调整，不改变全群性格。'

__all__ = ['FeedbackStore']
