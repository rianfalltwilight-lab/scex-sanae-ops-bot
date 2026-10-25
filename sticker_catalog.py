#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Private, explicit sticker catalog for the existing OneBot account."""
from __future__ import annotations
import json
import os
import random
import re
import threading
from pathlib import Path
from urllib.parse import quote
from sanae_identity import BOT_NAME_PATTERN


class StickerCatalog:
    _DISTRESS = re.compile(r'难过|难受|伤心|委屈|被欺负|心情不好|不开心|想哭|哭了|崩溃|害怕|撑不住|去世|失恋|生病')
    _PLAYFUL_REQUEST = re.compile(
        r'(?:@?' + BOT_NAME_PATTERN + r'[，,、：:\s]*)?(?:'
        r'你(?:为什么|怎么)(?:这么|那么)(?:可爱|厉害|聪明|会聊天|会接梗|嘴甜)(?:呀|啊|呢|嘛)?|'
        r'(?:你)?(?:能不能|可不可以|可以)?(?:夸夸我|夸我(?:一下|一句|两句)?)(?:吗|嘛|呀|啊)?'
        r')[?？!！~～。]*', re.I)
    _PRAISE = re.compile(r'(?:' + BOT_NAME_PATTERN + r'|你).{0,8}(?:真|太|好|很|挺).{0,4}(?:可爱|厉害|聪明|会聊天|会接梗|棒)', re.I)
    MOOD_KEYWORDS = {
        '开心': ('开心', '哈哈', '好耶', '成功', '庆祝', '赞', '感谢', '太好了'),
        '无语': ('无语', '离谱', '绷不住', '什么鬼', '麻了', '服了'),
        '拒绝': ('我拒绝', '才不要', '想都别想', '不要啦'),
        '震惊': ('震惊', '惊了', '卧槽', '没想到', '居然', '竟然'),
        '道歉': ('抱歉', '对不起', '不好意思', '失误'),
        '撒娇': ('求求', '拜托', '呜呜', '嘿嘿', '想要', '夸我'),
        '疑惑': ('一头雾水', '满头问号', '啥情况', '什么鬼'),
        '委屈': ('委屈', '冤枉', '难过', '哭', '被欺负'),
        '认真': (),
        '生气': ('生气', '气死', '恼火', '滚', '烦'),
        '得意': ('得意', '骄傲', '那当然', '厉害吧', '尽在掌握'),
        '害羞': ('害羞', '脸红', '难为情', '怪不好意思'),
        '期待': ('期待', '等不及', '好期待', '快让我看看'),
        '调皮': ('逗你的', '开个玩笑', '嘿嘿', '比个耶', '眨个眼'),
        '尴尬': ('尴尬', '尬住', '汗流浃背', '强装镇定'),
        '困倦': ('困了', '好困', '晚安', '睡觉', '打哈欠', '累坏了'),
        '观察': ('围观', '吃瓜', '看热闹', '观察一下', '我先看看'),
        '招呼': ('早上好', '早安', '晚上好', '我来啦', '挥挥手'),
        '夸赞': ('真棒', '厉害', '做得漂亮', '干得漂亮', '做得好', '好模型'),
        '赞同': ('赞同', '同意', '说得对', '有道理', '确实如此'),
        '吐槽': ('吐槽', '离谱', '绷不住', '真有你的', '这也行'),
        '悲伤': ('伤心', '悲伤', '难过', '心碎'),
        '安抚': ('摸摸头', '抱抱你', '我陪着你', '慢慢来'),
        '玩梗': (),
    }

    def __init__(self, manifest_path: str | Path):
        self.path = Path(manifest_path)
        self._data = {}
        self._pick_lock = threading.Lock()
        self._recent_picks: dict[str, list[str]] = {}
        self._random = random.SystemRandom()
        self._load()
        self._history_path = self.path.with_name('selection-history.json')
        self._history_writable = True
        try:
            saved = json.loads(self._history_path.read_text(encoding='utf-8'))
            if not isinstance(saved, dict) or any(not isinstance(v, list) or
                    any(not isinstance(x, str) for x in v) for v in saved.values()):
                raise ValueError('invalid selection history')
            self._recent_picks = {str(k):v[-64:] for k,v in list(saved.items())[-64:]}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self._history_writable = False
            print('[stickers] selection history unavailable; preserving existing file', flush=True)

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
        lines = [f"贴纸库共 {len(rows)} 张（自动配图由语境与冷却决定）："]
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
        """Use the same image definitions as selection, without reserving a send."""
        ranked = self._ranked_candidates(text, auto_only=False)
        out, seen = [], set()
        for item, score, mood, direct in ranked:
            family = self._rotation_group(item)
            if family in seen:
                continue
            seen.add(family)
            out.append({'id': item['id'], 'primary_mood': item.get('primary_mood'),
                        'mood': mood, 'title': item.get('title', ''),
                        'meaning': item.get('meaning', ''),
                        'auto_allowed': self._auto_allowed(item)})
            if len(out) >= max(1, int(limit)):
                break
        return out

    def recommendation_text(self, text):
        rows = self.recommend(text, limit=5)
        if not rows:
            return '暂未匹配到明确语义；可用 !贴纸语义 <情绪> 查询。'
        return '推荐贴纸（仅推荐，不会自动发送）：' + '、'.join(
            f"{x['id']}（{x['title'] or x['mood']}；{x['meaning'] or x['mood']}"
            + ('；仅手动' if not x['auto_allowed'] else '') + '）' for x in rows)

    @staticmethod
    def _current_text(text):
        # Never infer a sticker from quoted history or injected internal hints.
        text = re.split(r'【最近群聊上下文|【引用消息|〖|\[引用消息\]', str(text or ''))[0]
        return re.sub(r'\[CQ:[^\]]*\]', '', text).strip()

    @classmethod
    def allows_auto(cls, request, reply):
        request = cls._current_text(request)
        serious = r'^[!！]|认真|正经|别开玩笑|不要开玩笑|详细|讲清楚|解释|教程|步骤|流程|配方|充电|报错|排查|如何|区别|分析|代码|配置|合成'
        if re.search(serious, request, re.I) or cls._DISTRESS.search(request):
            return False
        if (re.search(r'怎么|为什么|帮我|能不能|可不可以|是否', request)
                and not cls._PLAYFUL_REQUEST.fullmatch(request)):
            return False
        # The full model body controls size/format, even if a marker is quoted.
        text = str(reply or '')
        if len(text) > 180 or re.search(r'```|(?:^|\n)\s*(?:\d+[.、]|[-*] )|建议|说明|检查|配置|配方', text):
            return False
        return bool(text)

    @staticmethod
    def _phrase_matches(text, phrase):
        # Local negation, not a full language parser. Keep punctuation boundaries.
        for match in re.finditer(re.escape(phrase), text):
            before = text[max(0, match.start() - 12):match.start()]
            if re.search(r'(?:不愧是|不得不)[^，。！？；,\n.!?;]{0,2}$', before):
                return True
            if not re.search(r'(?:不|没|别|并非)[^，。！？；,\n.!?;]{0,7}$', before):
                return True
        return False

    @staticmethod
    def _auto_allowed(item):
        return item.get('allow_auto') is not False and item.get('semantic_auto_ok') is not False

    @staticmethod
    def _rotation_group(item):
        return str(item.get('rotation_group') or item['id'])

    def _ranked_candidates(self, text, request='', auto_only=True):
        """A picture's own phrases take priority over a broad shared mood."""
        text, request = self._current_text(text), self._current_text(request)
        if not text or (auto_only and self._DISTRESS.search(request)):
            return []
        scores = {m:sum(self._phrase_matches(text, k) for k in words)
                  for m,words in self.MOOD_KEYWORDS.items()}
        scores = {m:2*v + sum(k in request for k in self.MOOD_KEYWORDS[m])
                  for m,v in scores.items() if v}
        if re.search(r'(?:不|别|没(?:有)?).{0,5}(?:开心|好笑|庆祝|感谢|夸)', text):
            scores.pop('开心', None)
            scores.pop('撒娇', None)
        # A clear compliment or celebration can support a neutral warm reply.
        # Other request moods are never copied onto the bot's response.
        if not scores and not re.search(r'不|别|没|拒绝|抱歉|危险|担心|难过|生气', text):
            if self._PLAYFUL_REQUEST.fullmatch(request):
                # Praising the user is not the bot asking to be indulged.
                scores['夸赞' if '夸' in request else '开心'] = 1
            elif (not re.search(r'不|别|没|才怪|反话|阴阳', request) and
                  (self._PRAISE.search(request) or re.search(r'哈哈|好耶|庆祝|太好了|成功了|谢谢|感谢', request))):
                scores['开心'] = 1
        ranked = []
        for item in self.entries:
            if not item.get('id') or (auto_only and not self._auto_allowed(item)):
                continue
            if any(word in text or word in request for word in item.get('exclude_keywords', [])):
                continue
            hits = [word for word in item.get('match_keywords', []) if self._phrase_matches(text, word)]
            if item.get('match_mode') == 'specific' and not hits:
                continue
            moods = item.get('moods', [])
            primary = item.get('primary_mood')
            mood_score = scores.get(primary, 0) * 2 + max((scores.get(m, 0) for m in moods), default=0)
            if not (hits or mood_score) or not self.cq_image(item['id']):
                continue
            mood = primary if hits or scores.get(primary) else max(moods, key=lambda m: scores.get(m, 0))
            score = mood_score + sum(min(len(word), 8) for word in hits)
            ranked.append((item, score, mood, bool(hits)))
        # Do not randomly replace a precise caption/action with a vaguely happy face.
        if any(row[3] for row in ranked):
            ranked = [row for row in ranked if row[3]]
        elif any(scores.get(row[0].get('primary_mood')) for row in ranked):
            ranked = [row for row in ranked if scores.get(row[0].get('primary_mood'))]
        return sorted(ranked, key=lambda row: (-row[1], row[0].get('semantic_confidence') != 'high', row[0]['id']))

    def pick_for_send(self, text, scope='default', recent_limit=6, request=''):
        """Choose without recording delivery; only mark_sent changes history."""
        ranked = self._ranked_candidates(text, request=request)
        if not ranked:
            return None
        key = str(scope or 'default')
        with self._pick_lock:
            recent = [self._rotation_group(self.resolve(sid) or {'id': sid})
                      for sid in self._recent_picks.get(key, [])[-64:]]
            families = {self._rotation_group(row[0]) for row in ranked}
            window = min(max(1, int(recent_limit)), max(1, len(families)-1))
            eligible = [row for row in ranked if self._rotation_group(row[0]) not in recent[-window:]]
            if not eligible:
                eligible = [row for row in ranked if not recent or self._rotation_group(row[0]) != recent[-1]]
            if not eligible:
                eligible = ranked  # A singleton may recur after the bridge cooldown.
            weights = []
            for item, relevance, mood, direct in eligible:
                confidence = 1.3 if item.get('semantic_confidence') == 'high' else 1.0
                family = self._rotation_group(item)
                variants = sum(self._rotation_group(row[0]) == family for row in eligible)
                weights.append(relevance * confidence / ((1 + recent.count(family))**2 * variants))
            chosen, _, mood, _ = self._random.choices(eligible, weights=weights, k=1)[0]
            sid = str(chosen['id'])
            return {'id':sid, 'primary_mood':chosen.get('primary_mood'), 'mood':mood}

    def mark_sent(self, sticker_id, scope='default'):
        """Record an acknowledged delivery; callers must not retry the send."""
        item = self.resolve(sticker_id)
        if not item:
            return False
        sid, key = str(item['id']), str(scope or 'default')
        with self._pick_lock:
            recent = self._recent_picks.get(key, [])[-64:]
            self._recent_picks[key] = (recent + [sid])[-64:]
            while len(self._recent_picks) > 64:
                self._recent_picks.pop(next(iter(self._recent_picks)))
            if self._history_writable:
                temporary = self._history_path.with_suffix('.tmp')
                try:
                    temporary.write_text(json.dumps(self._recent_picks), encoding='utf-8')
                    os.replace(temporary, self._history_path)
                except OSError:
                    print('[stickers] selection history save failed', flush=True)
        return True

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
        normalized_path = str(file_path).replace('\\', '/')
        return f"[CQ:image,file=file:///{quote(normalized_path, safe='/:')}]"

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
