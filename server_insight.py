"""Read-only answers about the game server from data the bridge already keeps.

Sources, all local:
  <ops root>/perf-samples.jsonl  one RCON sample per minute (TPS, MSPT, players)
  <ops root>/events.jsonl        joins, leaves, advancements, deaths, crashes
  <ops root>/runtime.json        the latest sample
  <ops root>/player-stats.json   per-player statistics synced from the remote world
  <ops root>/login-records.json  last login time and login count from the MSLX panel (no IPs)
  <ops root>/recipe-index.json   item names (zh/en) for readable ids
  <mirror>/config/ftbquests/     the quest book, mirrored from the remote server
  <mirror>/config, kubejs, ...   configuration and scripts (admin lookups only)

Every function returns plain text for the model and never touches the server.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
WINDOWS = {'1h': 3600, '6h': 6 * 3600, '24h': 86400, '7d': 7 * 86400}
WINDOW_LABELS = {'1h': '1小时', '6h': '6小时', '24h': '24小时', '7d': '7天'}
_LOCK = threading.Lock()
_CACHE: dict = {}


def _stamp(value):
    return datetime.fromtimestamp(float(value), TZ).strftime('%m-%d %H:%M')


def _window(value, default='24h'):
    value = str(value or default)
    if value not in WINDOWS:
        raise ValueError('window 只能是 1h、6h、24h 或 7d')
    return WINDOW_LABELS[value], WINDOWS[value]


def _tail_rows(path, since, max_bytes=8 * 1024 * 1024):
    rows = []
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            if size > max_bytes:
                stream.readline()
            for raw in stream:
                try:
                    row = json.loads(raw.decode('utf-8-sig'))
                except (ValueError, UnicodeError):
                    continue
                stamp = row.get('timestamp') if isinstance(row, dict) else None
                if isinstance(stamp, (int, float)) and stamp >= since:
                    rows.append(row)
    except OSError:
        return []
    return rows


def _read_json(path, default=None):
    try:
        with open(path, encoding='utf-8-sig') as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def _fmt(value, digits=1):
    if value is None:
        return '未记录'
    text = f'{value:.{digits}f}'
    return text.rstrip('0').rstrip('.') if '.' in text else text


# ===== live status and trends =====

def live_status(ops_root, max_age=300, now=None):
    """'在线 6/20，TPS 19.5，MSPT 37（40 秒前）' or '' when the sample is stale."""
    now = time.time() if now is None else now
    row = _read_json(Path(ops_root) / 'runtime.json', {}) or {}
    stamp = row.get('timestamp')
    if not isinstance(stamp, (int, float)) or not 0 <= now - stamp <= max_age:
        return ''
    parts = []
    if row.get('players') is not None:
        parts.append(f"在线 {row['players']}/{row.get('max_players') or '?'}")
    if row.get('tps') is not None:
        parts.append(f"TPS {_fmt(row['tps'])}")
    if row.get('mspt') is not None:
        parts.append(f"MSPT {_fmt(row['mspt'], 0)}")
    if not parts:
        return ''
    return '，'.join(parts) + f'（{int(now - stamp)} 秒前采样）'


def live_verdict(ops_root, max_age=300, now=None):
    """'流畅' / '有点卡' / '很卡' for the latest sample; '' when stale or unknown.

    Same line as the trends lag count: TPS below 18 or MSPT at 50 or more is lag.
    """
    now = time.time() if now is None else now
    row = _read_json(Path(ops_root) / 'runtime.json', {}) or {}
    stamp, tps, mspt = row.get('timestamp'), row.get('tps'), row.get('mspt')
    if (not isinstance(stamp, (int, float)) or not 0 <= now - stamp <= max_age
            or not isinstance(tps, (int, float)) or not isinstance(mspt, (int, float))
            or row.get('tick_parser', 2) != 2):
        return ''
    if tps >= 18 and mspt < 50:
        return '流畅'
    return '有点卡' if tps >= 12 else '很卡'


def trends(ops_root, window='24h', now=None):
    now = time.time() if now is None else now
    label, seconds = _window(window)
    since = now - seconds
    rows = _tail_rows(Path(ops_root) / 'perf-samples.jsonl', since)
    if not rows:
        return f'最近{label}没有性能采样记录（采集可能中断）。'
    valid = [r for r in rows if r.get('tick_parser') == 2 and isinstance(r.get('tps'), (int, float))
             and isinstance(r.get('mspt'), (int, float))]
    players = [r for r in rows if isinstance(r.get('players'), (int, float))]
    lines = [f'窗口：最近{label}（{_stamp(since)}～{_stamp(now)}，北京时间），采样 {len(rows)} 个，'
             f'有效性能样本 {len(valid)} 个。']
    if valid:
        tps = [float(r['tps']) for r in valid]
        mspt = [float(r['mspt']) for r in valid]
        lag = sum(1 for r in valid if float(r['tps']) < 18 or float(r['mspt']) >= 50)
        worst = min(valid, key=lambda r: float(r['tps']))
        lines.append(f'TPS 平均 {_fmt(sum(tps) / len(tps))}，最低 {_fmt(min(tps))}（{_stamp(worst["timestamp"])}）；'
                     f'MSPT 平均 {_fmt(sum(mspt) / len(mspt), 0)}，最高 {_fmt(max(mspt), 0)}。')
        lines.append(f'卡顿样本（TPS<18 或 MSPT≥50）{lag} 个，占 {_fmt(100 * lag / len(valid))}%。'
                     '样本是每分钟一次的瞬时值，不等于玩家感受到的每一次卡顿。')
    if players:
        counts = [float(r['players']) for r in players]
        peak = max(players, key=lambda r: float(r['players']))
        lines.append(f'在线：平均 {_fmt(sum(counts) / len(counts))} 人，峰值 {int(peak["players"])} 人'
                     f'（{_stamp(peak["timestamp"])}）。')
        if seconds >= 86400:
            by_hour = defaultdict(list)
            for r in players:
                by_hour[datetime.fromtimestamp(r['timestamp'], TZ).hour].append(float(r['players']))
            ranked = sorted(by_hour.items(), key=lambda item: -sum(item[1]) / len(item[1]))[:5]
            lines.append('平均在线最多的时段：' + '、'.join(
                f'{hour}点 {_fmt(sum(v) / len(v))}人' for hour, v in ranked) + '。')
    stamps = sorted(r['timestamp'] for r in rows)
    gaps = [(a, b) for a, b in zip([since] + stamps, stamps + [now]) if b - a > 1800]
    if gaps:
        lines.append('采样中断：' + '、'.join(f'{_stamp(a)}～{_stamp(b)}' for a, b in gaps[:3]) + '。')
    return '\n'.join(lines)


# ===== events =====

_EVENT_KINDS = {
    'join': lambda r: r.get('type') == 'join',
    'leave': lambda r: r.get('type') == 'minecraft_event' and '离开服务器' in str(r.get('text')),
    'advancement': lambda r: r.get('type') == 'minecraft_event' and re.search(r'完成(进度|目标|挑战)', str(r.get('text'))),
    'death': lambda r: r.get('type') == 'minecraft_event' and re.search(r'被 .+ 击杀|淹死了', str(r.get('text'))),
    'crash': lambda r: r.get('type') == 'crash',
}


def events(ops_root, window='24h', player='', kind='', limit=30, now=None, online=None):
    now = time.time() if now is None else now
    label, seconds = _window(window)
    kind = str(kind or '').strip()
    if kind and kind not in _EVENT_KINDS:
        raise ValueError('kind 只能是 join、leave、advancement、death 或 crash')
    rows = [r for r in _tail_rows(Path(ops_root) / 'events.jsonl', now - seconds)
            if any(test(r) for test in _EVENT_KINDS.values())]
    if kind:
        rows = [r for r in rows if _EVENT_KINDS[kind](r)]
    player = str(player or '').strip()
    if player:
        rows = [r for r in rows if player.casefold() in str(r.get('text')).casefold()
                or player.casefold() == str((r.get('details') or {}).get('player') or '').casefold()]
    rows.sort(key=lambda r: r['timestamp'])
    joins = [r for r in rows if r.get('type') == 'join']
    people = {str((r.get('details') or {}).get('player') or '').casefold() for r in joins} - {''}
    head = (f'最近{label}的服务器事件' + (f'（只看 {player}）' if player else '') +
            f'：共 {len(rows)} 条，其中进服 {len(joins)} 次（{len(people)} 人）。')
    if not rows:
        return head + '\n没有符合条件的记录。事件从实时日志记录，采集中断期间会缺失。'
    shown = rows[-max(1, min(int(limit or 30), 60)):]
    body = [f'{_stamp(r["timestamp"])} {re.sub(r"^\[[^\]]+\]\s*", "", str(r.get("text")))[:200]}'
            for r in shown]
    more = f'\n（只列出最近 {len(shown)} 条）' if len(shown) < len(rows) else ''
    return head + '\n' + '\n'.join(body) + more + _open_sessions(ops_root, now, seconds, player, online)


_LEAVE_NAME = re.compile(r'玩家\s+(\S+)\s+离开服务器')


def _open_sessions(ops_root, now, seconds, player='', online=None):
    """For players online right now (live `list`), how long since their last join in the window.

    The live list is required: a crash writes no leave lines, so the log alone keeps
    players "online" for hours after they are gone.

    10-26 16:14 she was asked who had been on longest this session and declined to work
    it out from the join times; the tool now does the arithmetic.
    """
    if not online:
        return ''
    last = {}
    for row in _tail_rows(Path(ops_root) / 'events.jsonl', now - seconds):
        if row.get('type') == 'join':
            name = str((row.get('details') or {}).get('player') or '')
            if name:
                last[name.casefold()] = (name, float(row['timestamp']))
        elif _EVENT_KINDS['leave'](row):
            match = _LEAVE_NAME.search(str(row.get('text') or ''))
            if match:
                last.pop(match.group(1).casefold(), None)
    names = [str(n) for n in online if str(n).strip()]
    if player:
        names = [n for n in names if player.casefold() in n.casefold()]
    known = sorted((last[n.casefold()] for n in names if n.casefold() in last), key=lambda item: item[1])
    unknown = [n for n in names if n.casefold() not in last]
    if not known and not unknown:
        return ''
    parts = [f'{name} {max(0, int((now - joined) // 60))} 分钟（{_stamp(joined)} 进服）' for name, joined in known[:20]]
    text = '\n现在在线的玩家本次已在线（从长到短）：' + ('、'.join(parts) if parts else '无')
    if unknown:
        text += f'；{"、".join(unknown[:10])} 的进服时间不在这段记录里'
    return text + '。时长从最后一次进服算到现在。'


# ===== item names =====

def item_names(ops_root):
    path = Path(ops_root) / 'recipe-index.json'
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _LOCK:
        cached = _CACHE.get(('names', str(path)))
        if cached and cached[0] == mtime:
            return cached[1]
    names = (_read_json(path, {}) or {}).get('names') or {}
    with _LOCK:
        _CACHE[('names', str(path))] = (mtime, names)
    return names


def item_label(names, item_id):
    entry = names.get(str(item_id)) or {}
    label = entry.get('zh') or entry.get('en') if isinstance(entry, dict) else ''
    return f'{label}（{item_id}）' if label else str(item_id)


# ===== player statistics =====

STAT_FIELDS = {
    'play_hours': '游戏时长（小时）', 'deaths': '死亡次数', 'mob_kills': '击杀生物',
    'player_kills': '击杀玩家', 'mined': '挖掘方块', 'crafted': '合成物品',
    'distance_km': '移动距离（公里）', 'jumps': '跳跃次数', 'sessions': '进服次数',
}


def login_records(ops_root):
    """name (casefold) -> {name, last_login, login_count} from the panel's records."""
    data = _read_json(Path(ops_root) / 'login-records.json', {}) or {}
    return {str(r.get('name')).casefold(): r for r in data.get('players') or [] if isinstance(r, dict) and r.get('name')}


def player_stats(ops_root, player='', top='', limit=10):
    data = _read_json(Path(ops_root) / 'player-stats.json', {}) or {}
    rows = data.get('players') or []
    if not rows:
        return '玩家统计还没有从服务器同步过来，稍后再查。'
    when = _stamp(data['generated_at']) if data.get('generated_at') else '未知时间'
    note = f'数据来自服务器存档的统计文件，同步于 {when}；玩家在线时的最新进度要等服务器存盘后才会更新。'
    logins = login_records(ops_root)
    top = str(top or '').strip()
    if top == 'logins':
        ranked = sorted(logins.values(), key=lambda r: -int(r.get('login_count') or 0))[:max(1, min(int(limit or 10), 20))]
        if not ranked:
            return '登录记录还没有从服务器同步过来，稍后再查。'
        return (f'累计登录次数排行（服务器面板记录了 {len(logins)} 名玩家）：\n'
                + '\n'.join(f'{i}. {r["name"]}：{r.get("login_count", 0)} 次（上次 {str(r.get("last_login"))[:16]}）'
                            for i, r in enumerate(ranked, 1))
                + '\n登录记录来自服务器面板（MSLX），只有面板启用后登录过的玩家。')
    if top:
        if top not in STAT_FIELDS:
            raise ValueError('top 只能是 ' + '、'.join(STAT_FIELDS))
        ranked = sorted((r for r in rows if isinstance(r.get(top), (int, float))),
                        key=lambda r: -r[top])[:max(1, min(int(limit or 10), 20))]
        return (f'{STAT_FIELDS[top]}排行（共 {len(rows)} 名玩家有记录）：\n'
                + '\n'.join(f'{i}. {r["name"]}：{_fmt(r[top])}' for i, r in enumerate(ranked, 1))
                + '\n' + note)
    player = str(player or '').strip()
    if not player:
        raise ValueError('需要 player（玩家名）或 top（排行字段）')
    key = player.casefold()
    found = ([r for r in rows if str(r.get('name')).casefold() == key]
             or [r for r in rows if str(r.get('name')).casefold().startswith(key)]
             or [r for r in rows if key in str(r.get('name')).casefold()])
    if not found:
        return f'没有找到名叫 {player} 的玩家统计；名字要和游戏内一致。'
    if len(found) > 1 and str(found[0].get('name')).casefold() != key:
        return '有多个玩家名匹配：' + '、'.join(str(r['name']) for r in found[:10]) + '。请说完整的名字。'
    row = found[0]
    names = item_names(ops_root)
    lines = [f'玩家 {row["name"]} 的统计：']
    lines.extend(f'- {label}：{_fmt(row.get(field))}' for field, label in STAT_FIELDS.items()
                 if row.get(field) is not None)
    if row.get('top_mined'):
        lines.append('- 挖得最多：' + '、'.join(f'{item_label(names, i)} {n}' for i, n in row['top_mined']))
    if row.get('top_killed'):
        lines.append('- 杀得最多：' + '、'.join(f'{item_label(names, i)} {n}' for i, n in row['top_killed']))
    login = logins.get(str(row['name']).casefold())
    if login and login.get('last_login'):
        lines.append(f'- 上次登录：{str(login["last_login"])[:16]}（服务器面板记录），累计登录 {login.get("login_count", 0)} 次')
    if row.get('last_seen'):
        lines.append(f'- 存档最后更新：{_stamp(row["last_seen"])}')
    return '\n'.join(lines) + '\n' + note


# ===== SNBT (FTB Quests files) =====

class _Snbt:
    _NUMBER = re.compile(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?[bBsSlLfFdD]?$')

    def __init__(self, text):
        self.text, self.pos = text, 0

    def parse(self):
        value = self.value()
        self.space()
        return value

    def space(self):
        text, pos = self.text, self.pos
        while pos < len(text):
            char = text[pos]
            if char in ' \t\r\n,':
                pos += 1
            elif char == '/' and text.startswith('//', pos):
                end = text.find('\n', pos)
                pos = len(text) if end < 0 else end + 1
            else:
                break
        self.pos = pos

    def value(self):
        self.space()
        char = self.text[self.pos] if self.pos < len(self.text) else ''
        if char == '{':
            return self.compound()
        if char == '[':
            return self.array()
        if char in '"\'':
            return self.string()
        return self.bare()

    def compound(self):
        self.pos += 1
        result = {}
        while True:
            self.space()
            if self.pos >= len(self.text):
                raise ValueError('unterminated compound')
            if self.text[self.pos] == '}':
                self.pos += 1
                return result
            key = self.string() if self.text[self.pos] in '"\'' else self.word()
            self.space()
            if self.pos < len(self.text) and self.text[self.pos] == ':':
                self.pos += 1
            result[key] = self.value()

    def array(self):
        self.pos += 1
        self.space()
        if re.match(r'[BIL];', self.text[self.pos:self.pos + 2]):
            self.pos += 2
        items = []
        while True:
            self.space()
            if self.pos >= len(self.text):
                raise ValueError('unterminated list')
            if self.text[self.pos] == ']':
                self.pos += 1
                return items
            items.append(self.value())

    def string(self):
        quote = self.text[self.pos]
        self.pos += 1
        out = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == '\\' and self.pos + 1 < len(self.text):
                nxt = self.text[self.pos + 1]
                out.append({'n': '\n', 't': '\t'}.get(nxt, nxt))
                self.pos += 2
                continue
            if char == quote:
                self.pos += 1
                return ''.join(out)
            out.append(char)
            self.pos += 1
        raise ValueError('unterminated string')

    def word(self):
        match = re.compile(r'[^\s:,{}\[\]"\']+').match(self.text, self.pos)
        if not match:
            raise ValueError(f'unexpected character at {self.pos}')
        self.pos = match.end()
        return match.group(0)

    def bare(self):
        word = self.word()
        if word in ('true', 'false'):
            return word == 'true'
        if self._NUMBER.match(word):
            core = word[:-1] if word[-1] in 'bBsSlLfFdD' and not word[-1].isdigit() else word
            try:
                return float(core) if any(c in core for c in '.eE') else int(core)
            except ValueError:
                return word
        return word


def parse_snbt(text):
    return _Snbt(str(text)).parse()


# FTB Quests colour/format codes: &a, &l, &z, &#ff69b4, §x.
_FORMAT = re.compile(r'&#[0-9A-Fa-f]{6}|[&§][0-9a-zA-Z]')


def _plain(value):
    if isinstance(value, list):
        value = ' '.join(str(v) for v in value if str(v).strip())
    text = _FORMAT.sub('', str(value or ''))
    text = re.sub(r'\{image:[^}]*\}|\{@pagebreak\}', '', text)
    return re.sub(r'\s+', ' ', text.replace('\\n', ' ')).strip()


_TICK_STATS = {'minecraft:total_world_time', 'minecraft:play_time', 'minecraft:play_one_minute'}


def _task_text(kind, task, names):
    """Readable text for the non-item FTB task types seen in the quest book."""
    value = task.get('value')
    if kind == 'stat':
        stat = str(task.get('stat') or '')
        text = f'统计 {stat} 达到 {value}'
        if stat in _TICK_STATS and isinstance(value, (int, float)):
            minutes = value / 1200
            text += ('（进入世界即可完成）' if minutes < 1 else f'（约 {minutes:.0f} 分钟游戏时间）')
        return text
    if kind == 'kill':
        return f'击杀 {item_label(names, task.get("entity"))}' + (f' ×{value}' if value and value != 1 else '')
    if kind == 'biome':
        return f'到达生物群系 {task.get("biome")}'
    if kind == 'dimension':
        return f'进入维度 {task.get("dimension")}'
    if kind == 'observation':
        return f'观察 {item_label(names, task.get("to_observe"))}'
    if kind == 'advancement':
        return f'完成进度 {task.get("advancement")}'
    detail = task.get('entity') or task.get('dimension') or task.get('advancement') or ''
    return kind + (f'：{detail}' if detail else '')


def quest_index(mirror_root, ops_root):
    """Parse the mirrored quest book once per change; cached in memory."""
    base = Path(mirror_root) / 'config' / 'ftbquests' / 'quests'
    files = sorted(base.glob('chapters/*.snbt')) + [base / 'lang' / 'zh_cn.snbt', base / 'lang' / 'en_us.snbt']
    signature = tuple((str(p), p.stat().st_mtime) for p in files if p.is_file())
    if not signature:
        return []
    with _LOCK:
        cached = _CACHE.get(('quests', str(base)))
        if cached and cached[0] == signature:
            return cached[1]
    lang = {}
    for name in ('en_us.snbt', 'zh_cn.snbt'):   # zh overrides en
        path = base / 'lang' / name
        if path.is_file():
            try:
                parsed = parse_snbt(path.read_text(encoding='utf-8'))
                lang.update({str(k).upper(): v for k, v in parsed.items()} if isinstance(parsed, dict) else {})
            except ValueError:
                pass
    names = item_names(ops_root)

    def text(kind, ident, field):
        return _plain(lang.get(f'{kind}.{ident}.{field}'.upper(), ''))

    quests = []
    for path in sorted(base.glob('chapters/*.snbt')):
        try:
            chapter = parse_snbt(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(chapter, dict):
            continue
        chapter_id = str(chapter.get('id') or '')
        chapter_title = text('chapter', chapter_id, 'title') or str(chapter.get('filename') or path.stem)
        for quest in chapter.get('quests') or []:
            if not isinstance(quest, dict):
                continue
            ident = str(quest.get('id') or '')
            tasks, rewards = [], []
            for task in quest.get('tasks') or []:
                if not isinstance(task, dict):
                    continue
                kind = str(task.get('type') or '')
                title = text('task', task.get('id'), 'title')
                item = task.get('item')
                if isinstance(item, dict):
                    item = item.get('id')
                if title:
                    tasks.append(title)
                elif kind == 'item' and item:
                    count = task.get('count') or 1
                    tasks.append(item_label(names, item) + (f' ×{count}' if count != 1 else ''))
                elif kind == 'checkmark':
                    tasks.append('勾选完成')
                elif kind:
                    tasks.append(_task_text(kind, task, names))
            for reward in quest.get('rewards') or []:
                if not isinstance(reward, dict):
                    continue
                kind = str(reward.get('type') or '')
                title = text('reward', reward.get('id'), 'title')
                item = reward.get('item')
                if isinstance(item, dict):
                    count = item.get('count') or 1
                    item = item.get('id')
                else:
                    count = reward.get('count') or 1
                if title:
                    rewards.append(title)
                elif kind == 'item' and item:
                    rewards.append(item_label(names, item) + (f' ×{count}' if count != 1 else ''))
                elif kind == 'random':
                    rewards.append('随机奖励')
                elif kind in ('xp', 'xp_levels'):
                    rewards.append(f'经验 {reward.get("xp") or reward.get("xp_levels") or ""}'.strip())
                elif kind:
                    rewards.append(kind)
            title = text('quest', ident, 'title')
            if not title and tasks:
                title = tasks[0]
            quests.append({
                'id': ident, 'chapter': _plain(chapter_title), 'title': title or '（无标题）',
                'subtitle': text('quest', ident, 'quest_subtitle'),
                'description': text('quest', ident, 'quest_desc'),
                'tasks': tasks, 'rewards': rewards,
                'dependencies': len(quest.get('dependencies') or []),
            })
    with _LOCK:
        _CACHE[('quests', str(base))] = (signature, quests)
    return quests


def quest_lookup(mirror_root, ops_root, query, limit=5):
    quests = quest_index(mirror_root, ops_root)
    if not quests:
        return '任务书还没有从服务器同步过来，或服务器没有安装 FTB 任务。'
    query = str(query or '').strip()
    if not query:
        chapters = Counter(q['chapter'] for q in quests)
        return (f'任务书共 {len(chapters)} 章、{len(quests)} 个任务：\n'
                + '\n'.join(f'- {name}（{count} 个任务）' for name, count in chapters.most_common()))
    terms = [t for t in re.split(r'\s+', query.casefold()) if t] or [query.casefold()]
    scored = []
    for quest in quests:
        fields = [(quest['title'], 6), (quest['chapter'], 3), (quest['subtitle'], 3),
                  (' '.join(quest['tasks']), 4), (' '.join(quest['rewards']), 2), (quest['description'], 1)]
        score = sum(weight for term in terms for value, weight in fields if term in value.casefold())
        if query.casefold() in quest['title'].casefold():
            score += 10
        if score:
            scored.append((score, quest))
    if not scored:
        chapters = [c for c in dict.fromkeys(q['chapter'] for q in quests) if query.casefold() in c.casefold()]
        if chapters:
            rows = [q for q in quests if q['chapter'] in chapters]
            return (f'章节「{chapters[0]}」共 {len(rows)} 个任务：' +
                    '、'.join(q['title'] for q in rows[:40]))
        return f'任务书里没有找到和「{query}」相关的任务。'
    scored.sort(key=lambda item: -item[0])
    # "第一个任务怎么做": a chapter named in the query lists its quests without prerequisites.
    starters = []
    for chapter in [c for c in dict.fromkeys(q['chapter'] for q in quests)
                    if query.casefold() in c.casefold()][:3]:
        titles = [q['title'] for q in quests if q['chapter'] == chapter and not q['dependencies']]
        if titles:
            starters.append(f'章节「{chapter}」没有前置、可以先做的任务：' + '、'.join(titles[:10]))
    out = []
    for _, quest in scored[:max(1, min(int(limit or 5), 8))]:
        line = f'章节「{quest["chapter"]}」· 任务「{quest["title"]}」'
        if quest['subtitle']:
            line += f'：{quest["subtitle"][:120]}'
        if quest['description']:
            line += f'\n  说明：{quest["description"][:300]}'
        if quest['tasks']:
            line += '\n  要求：' + '、'.join(quest['tasks'][:8])
        if quest['rewards']:
            line += '\n  奖励：' + '、'.join(quest['rewards'][:8])
        line += (f'\n  前置任务 {quest["dependencies"]} 个' if quest['dependencies']
                 else '\n  没有前置任务，可以直接做')
        out.append(line)
    return (f'任务书匹配「{query}」的任务（共 {len(scored)} 个，列出前 {len(out)} 个）：\n' + '\n'.join(out)
            + ''.join('\n' + line for line in starters))


# ===== configuration lookup (admins only; enforced by the caller) =====

CONFIG_ROOTS = ('config', 'kubejs', 'defaultconfigs', 'world/serverconfig')
CONFIG_FILES = ('server.properties',)
_SECRET_KEY = re.compile(
    r'(?i)((?:rcon[._-]?)?pass(?:word|wd)?|secret|token|api[_-]?key|webhook|private[_-]?key|'
    r'credential|auth[_-]?key|session)\b(\s*["\']?\s*[:=]\s*)(["\']?)([^"\'\s,}]+)')


def redact(text):
    return _SECRET_KEY.sub(lambda m: m.group(1) + m.group(2) + m.group(3) + '<已隐藏>', str(text))


def _config_path(mirror_root, relative):
    relative = str(relative or '').replace('\\', '/').strip().lstrip('/')
    root = Path(mirror_root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('路径不在服务器目录内')
    rel = path.relative_to(root).as_posix()
    if rel not in CONFIG_FILES and not any(rel == r or rel.startswith(r + '/') for r in CONFIG_ROOTS):
        raise ValueError('只能读取 config、kubejs、defaultconfigs、world/serverconfig 和 server.properties')
    return path, rel


def read_config(mirror_root, relative, start_line=1, max_lines=300):
    path, rel = _config_path(mirror_root, relative)
    if path.is_dir():
        entries = sorted(p.name + ('/' if p.is_dir() else '') for p in path.iterdir())
        return f'{rel}/ 目录（{len(entries)} 项）：\n' + '\n'.join(entries[:200])
    if not path.is_file():
        return f'{rel} 不存在；可以先用 search_server_config 找文件名。'
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    start = max(1, int(start_line or 1))
    chunk = lines[start - 1:start - 1 + max(1, min(int(max_lines or 300), 400))]
    text = '\n'.join(f'{start + i}: {line[:300]}' for i, line in enumerate(chunk))
    tail = (f'\n（共 {len(lines)} 行，本次显示 {start}～{start + len(chunk) - 1} 行）'
            if len(lines) > len(chunk) else '')
    return f'{rel}（同步自服务器，敏感值已隐藏）：\n' + redact(text) + tail


def search_config(mirror_root, query, scope='', limit=20):
    query = str(query or '').strip()
    if len(query) < 2:
        raise ValueError('关键词至少 2 个字符')
    roots = [scope] if scope else list(CONFIG_ROOTS) + list(CONFIG_FILES)
    hits, files_hit = [], set()
    needle = query.casefold()
    for relative in roots:
        try:
            base, _rel = _config_path(mirror_root, relative)
        except ValueError as exc:
            return str(exc)
        paths = [base] if base.is_file() else sorted(p for p in base.rglob('*') if p.is_file()) if base.is_dir() else []
        for path in paths:
            rel = path.relative_to(Path(mirror_root).resolve()).as_posix()
            if needle in rel.casefold():
                files_hit.add(rel)
            try:
                lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, 1):
                if needle in line.casefold():
                    hits.append(f'{rel}:{number}: {redact(line.strip())[:200]}')
                    if len(hits) >= max(1, min(int(limit or 20), 50)):
                        break
            if len(hits) >= max(1, min(int(limit or 20), 50)):
                break
    head = []
    if files_hit:
        head.append('文件名匹配：' + '、'.join(sorted(files_hit)[:20]))
    if not hits and not head:
        return f'配置和脚本里没有找到「{query}」。'
    return '\n'.join(head + (['内容匹配：'] + hits if hits else []))
