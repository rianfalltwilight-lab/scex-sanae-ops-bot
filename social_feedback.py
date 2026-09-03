#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounded explicit feedback store for Social Lite."""
from __future__ import annotations
import json, os, re, threading, time
from pathlib import Path


class FeedbackStore:
    def __init__(self, path: str | Path, group_id: int | str = 0, max_items: int = 500):
        self.path, self.group_id, self.max_items = Path(path), str(group_id), max(50, int(max_items))
        self._lock, self._items = threading.RLock(), []
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8').lstrip('\ufeff'))
            if isinstance(data, list): self._items = [x for x in data if isinstance(x, dict)][-self.max_items:]
        except (OSError, ValueError, TypeError): self._items = []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f'.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(self._items[-self.max_items:], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        tmp.replace(self.path)

    def record(self, user_id, rating, note='', message_id=None):
        rating = 'positive' if str(rating).lower() in ('positive', 'good', 'up', '1', '赞') else 'negative'
        item = {'groupId': self.group_id, 'userId': str(user_id), 'rating': rating,
                'note': re.sub(r'[\x00-\x1f\x7f\r\n]+', ' ', str(note or ''))[:240],
                'messageId': str(message_id or '')[:40], 'time': time.time()}
        with self._lock:
            self._items.append(item)
            self._save()
        return item

    def summary(self):
        with self._lock:
            positive = sum(1 for x in self._items if x.get('rating') == 'positive')
            negative = sum(1 for x in self._items if x.get('rating') == 'negative')
            return {'positive': positive, 'negative': negative, 'total': positive + negative}

    def strategy_hint(self):
        """Return a compact prompt hint; ratings never directly train the model."""
        with self._lock:
            items = list(self._items[-40:])
        positive = sum(1 for x in items if x.get('rating') == 'positive')
        negative = sum(1 for x in items if x.get('rating') == 'negative')
        if not items:
            return '尚无有效反馈：保持简短、自然，不主动堆砌黑话或贴纸。'
        if negative > positive:
            return '近期群友负反馈偏多：回复更短，先直接回答，少用玩笑、黑话和贴纸，避免自作主张。'
        if positive >= negative * 2:
            return '近期群友反馈良好：可保持轻松拟人语气，但仍需就事论事，不连续发送。'
        return '反馈较为均衡：保持自然、克制，优先解决问题再补充情绪。'

    def command(self, raw, user_id, privileged=False):
        text = str(raw or '').strip()
        prefix = '^[!\uff01]' + '\u53cd\u9988'
        if not re.match(prefix, text): return None
        body = re.sub(prefix + r'\s*', '', text).strip()
        if body in ('\u7edf\u8ba1', '\u72b6\u6001'):
            if not privileged: return '\u53cd\u9988\u7edf\u8ba1\u4ec5\u7ba1\u7406\u5458\u53ef\u7528\u3002'
            s = self.summary()
            return f"\u53cd\u9988\u7edf\u8ba1：\u8d5e {s['positive']}，\u8e29 {s['negative']}，\u5171 {s['total']}\u6761\u3002"
        match = re.match(r'^(\u8d5e|\u597d|\u559c\u6b22|\u8e29|\u5dee|\u4e0d\u559c\u6b22)(?:\s+(.+))?$', body, re.S)
        if not match: return '\u7528\u6cd5：!\u53cd\u9988 \u8d5e|\u8e29 [\u8bf4\u660e]，\u6216 !\u53cd\u9988 \u7edf\u8ba1。'
        rating = 'positive' if match.group(1) in ('赞', '好', '喜欢') else 'negative'
        self.record(user_id, rating, match.group(2) or '')
        return '\u5df2\u8bb0\u5f55\uff0c\u4e0d\u4f1a\u7acb\u5373\u6539\u53d8\u56de\u590d\u3002'


__all__ = ['FeedbackStore']
