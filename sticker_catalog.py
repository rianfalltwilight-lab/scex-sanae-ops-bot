#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Private, explicit sticker catalog for the existing OneBot account."""
from __future__ import annotations
import json
import random
import re
import threading
from pathlib import Path
from urllib.parse import quote


class StickerCatalog:
    MOOD_KEYWORDS = {
        '开心': ('开心', '哈哈', '好耶', '成功', '庆祝', '赞', '感谢', '太好了'),
        '无语': ('无语', '离谱', '绷不住', '什么鬼', '麻了', '服了'),
        '拒绝': ('不行', '拒绝', '不能', '不可以', '别', '禁止'),
        '震惊': ('震惊', '惊了', '卧槽', '没想到', '居然', '竟然'),
        '道歉': ('抱歉', '对不起', '不好意思', '失误'),
        '撒娇': ('求求', '拜托', '呜呜', '嘿嘿', '想要', '夸我'),
        '疑惑': ('怎么', '为什么', '不懂', '疑问', '啥', '什么情况'),
        '委屈': ('委屈', '冤枉', '难过', '哭', '被欺负'),
        '认真': ('建议', '说明', '注意', '结论', '检查'),
        '生气': ('生气', '气死', '恼火', '滚', '烦'),
    }

    def __init__(self, manifest_path: str | Path):
        self.path = Path(manifest_path)
        self._data = {}
        self._pick_lock = threading.Lock()
        self._recent_picks: dict[str, list[str]] = {}
        self._random = random.SystemRandom()
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8').lstrip('\ufeff'))
            self._data = data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            self._data = {}

    @property
    def entries(self):
        return [x for x in self._data.get('entries', []) if isinstance(x, dict)]

    def resolve(self, sticker_id):
        sid = str(sticker_id or '').strip().lower()
        return next((x for x in self.entries if str(x.get('id', '')).lower() == sid), None)

    def list_text(self, category=None):
        rows = self.entries
        if category:
            rows = [x for x in rows if str(x.get('category')) == category or str(x.get('source_folder')) == category]
        if not rows:
            return '暂无贴纸。'
        groups = {}
        for item in rows:
            groups.setdefault(item.get('category') or '未分类', []).append(item)
        lines = [f"贴纸库共 {len(rows)} 张（自动发送当前关闭）："]
        for name, items in groups.items():
            ids = '、'.join(
                f"{x.get('id')}({x.get('primary_mood', '未标注')})"
                for x in items[:18]
            )
            suffix = '……' if len(items) > 18 else ''
            lines.append(f"{name}：{ids}{suffix}")
        return '\n'.join(lines)

    def mood_text(self, mood):
        """List explicit sticker IDs matching a semantic mood."""
        wanted = str(mood or '').strip()
        if not wanted:
            return '用法：!贴纸语义 <开心/无语/拒绝/震惊/道歉/撒娇等>'
        rows = [x for x in self.entries if wanted in (x.get('moods') or [])]
        if not rows:
            return f'暂无“{wanted}”语义贴纸。'
        ids = '、'.join(str(x.get('id')) for x in rows)
        return f'语义「{wanted}」共 {len(rows)} 张：{ids}'

    def recommend(self, text, limit=3):
        """Recommend IDs by deterministic keyword/mood matching."""
        text = str(text or '')
        scores = {}
        for mood, keywords in self.MOOD_KEYWORDS.items():
            score = sum(1 for keyword in keywords if keyword in text)
            if score:
                scores[mood] = score
        if not scores:
            return []
        ranked_moods = sorted(scores, key=lambda mood: (-scores[mood], mood))
        out = []
        for mood in ranked_moods:
            rows = [x for x in self.entries if mood in (x.get('moods') or [])]
            rows.sort(key=lambda x: (x.get('semantic_confidence') != 'high', x.get('id', '')))
            for row in rows:
                out.append({'id': row.get('id'), 'primary_mood': row.get('primary_mood'), 'mood': mood})
                if len(out) >= max(1, int(limit)):
                    return out
        return out

    def recommendation_text(self, text):
        rows = self.recommend(text, limit=5)
        if not rows:
            return '暂未匹配到明确语义；可用 !贴纸语义 <情绪> 查询。'
        return '推荐贴纸（仅推荐，不会自动发送）：' + '、'.join(
            f"{x['id']}（{x['mood']}）" for x in rows)

    def pick_for_send(self, text, scope='default', recent_limit=6):
        """Choose one semantic match while avoiding recently sent stickers.

        ``recommend`` intentionally stays deterministic for diagnostics and
        admin commands.  Actual sending uses this bounded in-memory history so
        repeated replies with the same mood do not always select the first ID.
        No model call or persistent state is involved.
        """
        ranked = self.recommend(text, limit=1)
        if not ranked:
            return None
        mood = ranked[0]['mood']
        candidates = [
            item for item in self.entries
            if item.get('id') and mood in (item.get('moods') or [])
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (
            item.get('semantic_confidence') != 'high', str(item.get('id', ''))))
        scope_key = str(scope or 'default')
        keep = max(1, int(recent_limit))
        with self._pick_lock:
            recent = list(self._recent_picks.get(scope_key) or [])[-keep:]
            eligible = [item for item in candidates if str(item.get('id')) not in recent]
            if not eligible:
                # Once the whole semantic pool has been used, only forbid an
                # immediate repeat and start a fresh cycle.
                last = recent[-1] if recent else ''
                eligible = [item for item in candidates if str(item.get('id')) != last]
                if not eligible:
                    eligible = candidates
            preferred = [item for item in eligible
                         if item.get('semantic_confidence') == 'high']
            chosen = self._random.choice(preferred or eligible)
            chosen_id = str(chosen.get('id'))
            recent.append(chosen_id)
            self._recent_picks[scope_key] = recent[-keep:]
        return {'id': chosen_id, 'primary_mood': chosen.get('primary_mood'), 'mood': mood}

    def cq_image(self, sticker_id):
        item = self.resolve(sticker_id)
        if not item:
            return None
        file_path = (self.path.parent / 'assets' / Path(str(item.get('file', ''))).name).resolve()
        # Manifest file paths include category subdirectory; resolve against the
        # asset root while rejecting traversal and missing files.
        file_path = (self.path.parent / str(item.get('file', ''))).resolve()
        try:
            file_path.relative_to((self.path.parent / 'assets').resolve())
        except ValueError:
            return None
        if not file_path.is_file():
            return None
        return f"[CQ:image,file=file:///{quote(str(file_path).replace('\\', '/'), safe='/:')}]"

    def command(self, raw, privileged=False):
        text = str(raw or '').strip()
        if text.startswith(('!贴纸语义', '！贴纸语义')):
            if not privileged:
                return '贴纸目录和发送仅群主/管理员可用。'
            return self.mood_text(re.sub(r'^[!！]贴纸语义\s*', '', text))
        if text.startswith(('!贴纸推荐', '！贴纸推荐')):
            if not privileged:
                return '贴纸目录和发送仅群主/管理员可用。'
            return self.recommendation_text(re.sub(r'^[!！]贴纸推荐\s*', '', text))
        if not re.match(r'^[!\uff01]\u8d34\u7eb8(?:\s|$)', text) and text not in ('!贴纸列表', '！贴纸列表'):
            return None
        if not privileged:
            return '贴纸目录和发送仅群主/管理员可用。'
        body = re.sub(r'^[!\uff01]\u8d34\u7eb8\s*', '', text).strip()
        if body in ('', '列表', 'list'):
            return self.list_text()
        if body.startswith('分类 '):
            return self.list_text(body[3:].strip())
        image = self.cq_image(body)
        return image or '找不到这个贴纸 ID，先用 !贴纸列表 查看。'


__all__ = ['StickerCatalog']
