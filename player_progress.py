"""Quest-book and Thaumcraft research progress per player, from saved world data.

Sources (local copies, read-only):
  <mirror>/world/ftbquests/<team>.snbt      FTB Quests progress per team: completed quest ids and times
  <mirror>/world/ftbteams/{party,player}/   team membership, party names and player names
  <mirror>/config/ftbquests/quests/         the quest book (server_insight.quest_index)
  <ops>/thaumcraft-progress.json            research keys, clues, aspects and warp per player
                                            (remote_server_reader.thaumcraft_progress, world/playerdata)
  <ops>/thaumcraft-research.json            every research key on the server with its name and mod
                                            (build_research_index: `thaumcraft research list` + jar lang files)
All numbers are as of the server's last save, not live. Every function returns plain text.
"""
from __future__ import annotations

import json
import re
import threading
import time
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import server_insight

TZ = timezone(timedelta(hours=8))
MEMBER_RANKS = ('owner', 'officer', 'member')
WARP_LABELS = ('永久', '粘性', '临时')
RESEARCH_TOP = {'research': '完成研究数', 'aspects': '已发现要素', 'warp': '扭曲总值'}
_LOCK = threading.Lock()
_CACHE = {}
_LANG = re.compile(r'assets/[^/]+/lang/(zh_cn|en_us)\.json')


def _stamp(value):
    return datetime.fromtimestamp(float(value), TZ).strftime('%m-%d %H:%M')


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _match(rows, player, key=lambda row: row['name'], score=lambda row: 0):
    """(row, '') for one name match, or (None, message) like player_stats.

    One name can own two UUIDs (an offline-mode and an online account); the one with
    more progress (score) wins.
    """
    wanted = str(player or '').strip().casefold()
    found = ([r for r in rows if str(key(r)).casefold() == wanted]
             or [r for r in rows if str(key(r)).casefold().startswith(wanted)]
             or [r for r in rows if wanted in str(key(r)).casefold()])
    if not found:
        return None, ''
    distinct = list(dict.fromkeys(str(key(r)) for r in found))
    if len(distinct) > 1 and distinct[0].casefold() != wanted:
        return None, '有多个玩家名匹配：' + '、'.join(distinct[:10]) + '。请说完整的名字。'
    same = [r for r in found if str(key(r)).casefold() == str(key(found[0])).casefold()]
    return max(same, key=score), ''


# ===== FTB Quests =====

def _signature(*folders):
    return tuple((str(p), p.stat().st_mtime) for folder in folders if folder.is_dir()
                 for p in sorted(folder.glob('*.snbt')))


def _parse(path):
    try:
        value = server_insight.parse_snbt(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def quest_teams(mirror_root):
    """Teams with quest progress: {'teams': {id: {...}}, 'players': {uuid: name}, 'member_of': {uuid: team}}.

    A player in a party shares the party's progress; their own player team is left out.
    """
    world = Path(mirror_root) / 'world'
    folders = (world / 'ftbquests', world / 'ftbteams' / 'party', world / 'ftbteams' / 'player')
    signature = _signature(*folders)
    with _LOCK:
        cached = _CACHE.get(('teams', str(world)))
        if cached and cached[0] == signature:
            return cached[1]
    players, member_of, parties = {}, {}, {}
    for path in sorted(folders[2].glob('*.snbt')) if folders[2].is_dir() else []:
        row = _parse(path)
        players[path.stem.lower()] = str(row.get('player_name') or path.stem[:8])
    for path in sorted(folders[1].glob('*.snbt')) if folders[1].is_dir() else []:
        row = _parse(path)
        members = [str(u).lower() for u, rank in (row.get('ranks') or {}).items() if rank in MEMBER_RANKS]
        props = row.get('properties') or {}
        parties[path.stem.lower()] = {'name': str(props.get('ftbteams:display_name') or '队伍'), 'members': members}
        for uuid in members:
            member_of[uuid] = path.stem.lower()
    teams = {}
    for path in sorted(folders[0].glob('*.snbt')) if folders[0].is_dir() else []:
        team = path.stem.lower()
        if team in parties:
            label, members, party = parties[team]['name'], parties[team]['members'], True
        elif team in players and team not in member_of:
            label, members, party = players[team], [team], False
        else:
            continue          # deleted team, or a player now playing in a party
        completed = _parse(path).get('completed') or {}
        teams[team] = {'name': label, 'members': members, 'party': party, 'saved_at': path.stat().st_mtime,
                       'completed': {str(k): v for k, v in completed.items() if isinstance(v, (int, float))}}
    data = {'teams': teams, 'players': players, 'member_of': member_of}
    with _LOCK:
        _CACHE[('teams', str(world))] = (signature, data)
    return data


def _team_label(team, players, limit=4):
    if not team['party']:
        return team['name']
    names = [players.get(u, u[:8]) for u in team['members']]
    return f'队伍「{team["name"]}」（' + '、'.join(names[:limit]) + (f' 等 {len(names)} 人' if len(names) > limit else '') + '）'


def quest_progress(mirror_root, ops_root, player='', limit=10):
    quests = server_insight.quest_index(mirror_root, ops_root)
    if not quests:
        return '任务书还没有从服务器同步过来，或服务器没有安装 FTB 任务。'
    data = quest_teams(mirror_root)
    if not data['teams']:
        return '任务进度还没有从服务器同步过来，稍后再查。'
    by_id = {q['id']: q for q in quests}
    newest = max(team['saved_at'] for team in data['teams'].values())
    note = (f'进度来自服务器存档（队伍共享进度），最近一次存档 {_stamp(newest)}；'
            '玩家在线时刚完成的任务要等服务器存盘、同步后才会算进来。')
    player = str(player or '').strip()
    if not player:
        ranked = sorted(data['teams'].values(), key=lambda t: -sum(k in by_id for k in t['completed']))
        ranked = [t for t in ranked if any(k in by_id for k in t['completed'])]
        rows = ranked[:max(1, min(int(limit or 10), 20))]
        return (f'任务完成排行（任务书共 {len(quests)} 个任务，{len(ranked)} 支队伍或个人有进度）：\n'
                + '\n'.join(f'{i}. {_team_label(t, data["players"])}：{n} 个（{n * 100 // len(quests)}%）'
                            for i, t in enumerate(rows, 1)
                            for n in [sum(k in by_id for k in t['completed'])])
                + '\n' + note)
    people = [{'uuid': uuid, 'name': name} for uuid, name in data['players'].items()]

    def progress(person):
        team = data['teams'].get(data['member_of'].get(person['uuid'], person['uuid'])) or {}
        return sum(k in by_id for k in team.get('completed') or {})
    row, message = _match(people, player, score=progress)
    if not row:
        return message or f'没有找到名叫 {player} 的玩家；名字要和游戏内一致。'
    team_id = data['member_of'].get(row['uuid'], row['uuid'])
    team = data['teams'].get(team_id)
    if not team:
        return f'玩家 {row["name"]} 还没有任务进度记录。'
    done = [(by_id[k], v) for k, v in team['completed'].items() if k in by_id]
    head = f'玩家 {row["name"]} 的任务进度' + (f'（{_team_label(team, data["players"])}，进度全队共享）' if team['party'] else '')
    lines = [head + '：', f'- 已完成 {len(done)}/{len(quests)} 个任务（{len(done) * 100 // len(quests)}%）']
    totals = Counter(q['chapter'] for q in quests)
    finished = Counter(q['chapter'] for q, _ in done)
    chapters = list(dict.fromkeys(q['chapter'] for q in quests))
    if finished:
        lines.append('- 各章节：' + '、'.join(f'{c} {finished[c]}/{totals[c]}' for c in chapters if finished[c]))
    untouched = [c for c in chapters if not finished[c]]
    if untouched:
        lines.append('- 还没开始的章节：' + '、'.join(untouched))
    recent = sorted(done, key=lambda item: -item[1])[:3]
    if recent:
        lines.append('- 最近完成：' + '、'.join(f'「{q["title"]}」（{q["chapter"]}，{_stamp(at / 1000)}）'
                                             for q, at in recent))
    open_chapters = sorted((c for c in chapters if finished[c] and finished[c] < totals[c]),
                           key=lambda c: finished[c] / totals[c], reverse=True)[:3]
    if open_chapters:
        lines.append('- 进行中、离完成最近的章节：' + '、'.join(
            f'{c}（还差 {totals[c] - finished[c]} 个）' for c in open_chapters))
    lines.append(f'- 队伍存档更新于 {_stamp(team["saved_at"])}')
    return '\n'.join(lines) + '\n' + note


# ===== Thaumcraft research =====

def parse_research_list(text):
    """Research keys from `thaumcraft research list`, one per line; [] when the output is not a list."""
    lines = [line.strip() for line in str(text or '').splitlines() if line.strip()]
    keys = [line for line in lines if len(line) <= 80 and re.fullmatch(r'[A-Za-z0-9_.:@ -]+', line)]
    return keys if len(keys) >= max(5, len(lines) // 2) else []


def build_research_index(mods_dir, inventory_path, listing_text, previous=None):
    """{'generated_at', 'keys', 'names', 'mods'}: every research on the server, its zh name and mod.

    Names come from `tc.research_name.<key>` in each jar's lang files; a key belongs to the base
    mod when its research JSON ships in the Thaumcraft jar, otherwise to the addon naming it.
    """
    keys = parse_research_list(listing_text) or list((previous or {}).get('keys') or [])
    labels = {str(row.get('file')): str(row.get('name') or '')
              for row in (_read_json(inventory_path, {}) or {}).get('mods') or [] if isinstance(row, dict)}
    named, base = {}, set()        # named[key][mod] = {'zh_cn': ..., 'en_us': ...}
    for jar in sorted(Path(mods_dir).glob('*.jar')):
        try:
            with zipfile.ZipFile(jar) as archive:
                for name in archive.namelist():
                    if name.startswith('data/thaumcraft/research/') and name.endswith('.json'):
                        base.add(name.rsplit('/', 1)[-1][:-5])
                    match = _LANG.fullmatch(name)
                    if not match:
                        continue
                    try:
                        lang = json.loads(archive.read(name).decode('utf-8-sig'))
                    except (ValueError, UnicodeDecodeError, KeyError):
                        continue
                    for key, value in (lang.items() if isinstance(lang, dict) else ()):
                        if isinstance(key, str) and key.startswith('tc.research_name.') and isinstance(value, str):
                            short = key[len('tc.research_name.'):]
                            mod = labels.get(jar.name) or jar.stem
                            named.setdefault(short, {}).setdefault(mod, {}).setdefault(match.group(1), value)
        except (OSError, zipfile.BadZipFile):
            continue
    base_label = next((v for k, v in labels.items() if k.lower().startswith('thaumcraft-')), 'Thaumcraft')
    mods, names = {}, {}
    for key in keys or sorted(named):
        by_mod = named.get(key) or {}
        addons = [mod for mod in by_mod if mod != base_label]
        mods[key] = base_label if key in base or not addons else addons[0]
        # The owning mod's own translation first, then any other jar's; Chinese before English.
        rows = [by_mod.get(mods[key]) or {}] + [row for mod, row in by_mod.items() if mod != mods[key]]
        name = next((row[lang] for lang in ('zh_cn', 'en_us') for row in rows if row.get(lang)), '')
        if name:
            names[key] = name
    return {'generated_at': time.time(), 'keys': keys, 'names': names, 'mods': mods}


def research_progress(ops_root, player='', top='', limit=10, mirror_root=None):
    data = _read_json(Path(ops_root) / 'thaumcraft-progress.json', {}) or {}
    rows = [dict(row, uuid=uuid) for uuid, row in (data.get('players') or {}).items() if isinstance(row, dict)]
    if not rows:
        return '神秘时代研究进度还没有从服务器同步过来，稍后再查。'
    # usercache.json forgets players after a while; FTB Teams keeps every name it has seen.
    known = quest_teams(mirror_root)['players'] if mirror_root else {}
    for row in rows:
        if not row.get('name') or row['name'] == row['uuid'][:8]:
            row['name'] = known.get(row['uuid']) or row['uuid'][:8]
    index = _read_json(Path(ops_root) / 'thaumcraft-research.json', {}) or {}
    keys = set(index.get('keys') or [])
    names = index.get('names') or {}
    mods = index.get('mods') or {}
    total = len(keys)

    def done(row):
        return [k for k in row.get('research') or [] if not keys or k in keys]

    def warp(row):
        return sum(int(v or 0) for v in (row.get('warp') or [])[:3])
    note = (f'数据来自服务器存档的玩家文件，同步于 {_stamp(data["generated_at"]) if data.get("generated_at") else "未知时间"}；'
            '在线时刚解锁的研究要等服务器存盘后才会更新。')
    top = str(top or '').strip()
    player = str(player or '').strip()
    if top or not player:
        if top and top not in RESEARCH_TOP:
            raise ValueError('top 只能是 ' + '、'.join(RESEARCH_TOP))
        field = top or 'research'
        value = {'research': lambda r: len(done(r)), 'aspects': lambda r: int(r.get('aspects') or 0),
                 'warp': warp}[field]
        ranked = sorted(rows, key=lambda r: (-value(r), str(r.get('name'))))[:max(1, min(int(limit or 10), 20))]
        head = (f'神秘时代{RESEARCH_TOP[field]}排行（{len(rows)} 名玩家有研究记录'
                + (f'，服务器共 {total} 项研究' if total and field == 'research' else '') + '）：')
        lines = [head] + [f'{i}. {r.get("name") or r["uuid"][:8]}：{value(r)}'
                          + (f'（{value(r) * 100 // total}%）' if field == 'research' and total else '')
                          + (f'（永久 {r["warp"][0]}、粘性 {r["warp"][1]}、临时 {r["warp"][2]}）'
                             if field == 'warp' and len(r.get('warp') or []) >= 3 else '')
                          for i, r in enumerate(ranked, 1)]
        best = sum(value(r) == value(ranked[0]) for r in rows)
        if best > len(ranked):
            lines.append(f'并列第一（{value(ranked[0])}）的共有 {best} 人，上面只列出一部分。')
        if not top:
            counts = sorted(len(done(r)) for r in rows)
            lines.append(f'中位数 {counts[len(counts) // 2]} 项；' + (f'完成全部研究的有 {sum(c >= total for c in counts)} 人。'
                                                                   if total else ''))
        return '\n'.join(lines) + '\n' + note
    row, message = _match(rows, player, key=lambda r: r.get('name') or '', score=lambda r: len(done(r)))
    if not row:
        return message or f'没有找到 {player} 的神秘时代研究记录（可能还没开始研究，或名字和游戏内不一致）。'
    finished = done(row)
    lines = [f'玩家 {row["name"]} 的神秘时代研究进度：',
             f'- 已完成 {len(finished)}' + (f'/{total} 项研究（{len(finished) * 100 // total}%）' if total else ' 项研究')]
    if mods:
        have = Counter(mods.get(k, '其他') for k in finished)
        every = Counter(mods.get(k, '其他') for k in keys)
        lines.append('- 按模组：' + '、'.join(f'{mod} {have[mod]}/{count}' for mod, count in every.most_common()))
    lines.append(f'- 线索 {int(row.get("clues") or 0)} 条（还没完成的研究的提示），已发现要素 {int(row.get("aspects") or 0)} 种')
    if len(row.get('warp') or []) >= 3:
        lines.append(f'- 扭曲：永久 {row["warp"][0]}、粘性 {row["warp"][1]}、临时 {row["warp"][2]}（合计 {warp(row)}）')
    missing = sorted(keys - set(finished))
    if missing and len(missing) <= 30:
        lines.append(f'- 还差的 {len(missing)} 项：' + '、'.join(names.get(k) or k for k in missing[:12])
                     + (' 等' if len(missing) > 12 else ''))
    if row.get('saved_at'):
        lines.append(f'- 玩家存档更新于 {_stamp(row["saved_at"])}')
    return '\n'.join(lines) + '\n' + note


# ===== advancements =====
#   <mirror>/world/advancements/<uuid>.json   per-player progress, mirrored every 30 minutes
#   <ops>/advancement-index.json              every advancement shown in the advancement screen:
#                                             vanilla (from the server jar) and mods (local mirror)
_ADV_PATH = re.compile(r'data/([a-z0-9_.-]+)/advancements?/(.+)\.json')
_ADV_FILES = {}


def _component_key(value):
    """(translate key, literal text) of a display title component."""
    if isinstance(value, dict):
        return str(value.get('translate') or ''), str(value.get('text') or value.get('fallback') or '')
    if isinstance(value, list) and value:
        return _component_key(value[0])
    return '', str(value or '')


def build_advancement_index(mods_dir, inventory_path, vanilla, overrides_root=None, vanilla_lang=None):
    """{'generated_at', 'advancements': {id: {name, mod, frame, hidden}}}.

    Only advancements with a display count; recipe unlocks and hidden triggers have none.
    Names: the bot's zh overrides first, then any jar's zh_cn, then en_us, then the raw text.
    """
    labels = {str(row.get('file')): str(row.get('name') or '')
              for row in (_read_json(inventory_path, {}) or {}).get('mods') or [] if isinstance(row, dict)}
    zh, en, found = {}, {}, {}
    for key, value in (_read_json(vanilla_lang, {}) or {}).items() if vanilla_lang else ():
        zh.setdefault(key, value)
    for jar in sorted(Path(mods_dir).glob('*.jar')):
        mod = labels.get(jar.name) or jar.stem
        try:
            with zipfile.ZipFile(jar) as archive:
                for name in archive.namelist():
                    match = _LANG.fullmatch(name)
                    if match:
                        try:
                            lang = json.loads(archive.read(name).decode('utf-8-sig'))
                        except (ValueError, UnicodeDecodeError, KeyError):
                            continue
                        target = zh if match.group(1) == 'zh_cn' else en
                        for key, value in (lang.items() if isinstance(lang, dict) else ()):
                            if isinstance(value, str):
                                target.setdefault(key, value)
                        continue
                    match = _ADV_PATH.fullmatch(name)
                    if not match or '/recipes/' in name or match.group(2).startswith('recipes/'):
                        continue
                    try:
                        row = json.loads(archive.read(name).decode('utf-8-sig'))
                    except (ValueError, UnicodeDecodeError, KeyError):
                        continue
                    display = row.get('display') if isinstance(row, dict) else None
                    if isinstance(display, dict):
                        key, text = _component_key(display.get('title'))
                        found[f'{match.group(1)}:{match.group(2)}'] = {
                            'key': key, 'text': text, 'mod': mod, 'frame': str(display.get('frame') or 'task'),
                            'hidden': bool(display.get('hidden'))}
        except (OSError, zipfile.BadZipFile):
            continue
    overrides = {}
    for path in sorted(Path(overrides_root).glob('assets/*/lang/zh_cn.json')) if overrides_root else []:
        overrides.update({k: v for k, v in (_read_json(path, {}) or {}).items() if isinstance(v, str)})
    for ident, row in (vanilla or {}).items():
        found[ident] = {'key': row.get('key') or '', 'text': row.get('en') or '', 'mod': 'Minecraft',
                        'frame': row.get('frame') or 'task', 'hidden': bool(row.get('hidden'))}
    out = {}
    for ident, row in found.items():
        key = row['key']
        name = (overrides.get(key) or zh.get(key) or en.get(key) or row['text'] or key or ident) if key else (row['text'] or ident)
        out[ident] = {'name': re.sub(r'\s+', ' ', re.sub(r'§.', '', str(name))).strip(), 'mod': row['mod'],
                      'frame': row['frame'],
                      'hidden': row['hidden']}
    return {'generated_at': time.time(), 'advancements': out}


def _completed_at(entry):
    """Epoch of the last criterion of a done advancement ("2026-08-10 11:39:13 +0800")."""
    best = None
    for value in (entry.get('criteria') or {}).values():
        try:
            stamp = datetime.strptime(str(value), '%Y-%m-%d %H:%M:%S %z').timestamp()
        except ValueError:
            continue
        best = stamp if best is None or stamp > best else best
    return best


def _advancement_rows(mirror_root, index):
    """{uuid: {id: completed_at}} for indexed, done advancements; each file re-read only when it changes."""
    folder = Path(mirror_root) / 'world' / 'advancements'
    rows = {}
    for path in sorted(folder.glob('*.json')) if folder.is_dir() else []:
        stat = path.stat()
        key = str(path)
        cached = _ADV_FILES.get(key)
        if not cached or cached[0] != (stat.st_mtime, stat.st_size):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            done = {ident: _completed_at(entry) for ident, entry in data.items()
                    if isinstance(entry, dict) and entry.get('done')}
            cached = ((stat.st_mtime, stat.st_size), done)
            _ADV_FILES[key] = cached
        rows[path.stem.lower()] = {k: v for k, v in cached[1].items() if k in index}
    return rows


def warm_advancements(mirror_root, ops_root):
    """Parse changed progress files now (after a sync) so the next question does not wait for it."""
    index = (_read_json(Path(ops_root) / 'advancement-index.json', {}) or {}).get('advancements') or {}
    return len(_advancement_rows(mirror_root, index)) if index else 0


def known_players(mirror_root, ops_root):
    """uuid -> current name from FTB Teams, the Thaumcraft snapshot and the panel's login records."""
    names = dict(quest_teams(mirror_root)['players']) if mirror_root else {}
    for uuid, row in ((_read_json(Path(ops_root) / 'thaumcraft-progress.json', {}) or {}).get('players') or {}).items():
        if isinstance(row, dict) and row.get('name') and row['name'] != uuid[:8]:
            names[uuid] = row['name']
    for row in (_read_json(Path(ops_root) / 'login-records.json', {}) or {}).get('players') or []:
        if isinstance(row, dict) and row.get('uuid') and row.get('name'):
            names[str(row['uuid']).lower()] = str(row['name'])
    return names


def advancement_progress(mirror_root, ops_root, player='', query='', limit=None, order='recent'):
    index = (_read_json(Path(ops_root) / 'advancement-index.json', {}) or {}).get('advancements') or {}
    if not index:
        return '进度目录还没有建立（每天同步一次），稍后再查。'
    rows = _advancement_rows(mirror_root, index)
    if not rows:
        return '玩家进度还没有从服务器同步过来，稍后再查。'
    names = known_players(mirror_root, ops_root)
    people = [{'uuid': uuid, 'name': names.get(uuid, uuid[:8]), 'done': done} for uuid, done in rows.items()]
    total = len(index)
    counts = {}
    for person in people:
        for ident in person['done']:
            counts[ident] = counts.get(ident, 0) + 1
    note = '数据来自服务器存档的进度文件（每 30 分钟同步）；只算进度界面里能看到的进度，不含配方解锁。'
    query = str(query or '').strip()
    if query:
        wanted = query.casefold()
        hits = [i for i, row in index.items() if wanted in (row['name'].casefold(), i.casefold())]
        hits = hits or [i for i, row in index.items() if wanted in row['name'].casefold() or wanted in i.casefold()]
        if not hits:
            return f'没有找到名字里带「{query}」的进度。' + '\n' + note
        if len(hits) > 1:
            hits.sort(key=lambda i: -counts.get(i, 0))
            return (f'有 {len(hits)} 个进度匹配「{query}」：' + '、'.join(
                f'「{index[i]["name"]}」（{index[i]["mod"]}，{counts.get(i, 0)} 人完成）' for i in hits[:10])
                    + '。请说具体是哪一个。')
        ident = hits[0]
        row = index[ident]
        who = sorted(((p['done'][ident] or 0, p['name']) for p in people if ident in p['done']))
        head = (f'进度「{row["name"]}」（{row["mod"]}' + ('，挑战' if row['frame'] == 'challenge' else '')
                + ('，隐藏' if row['hidden'] else '') + f'）：全服 {len(who)} 人完成')
        if not who:
            return head + '。\n' + note
        lines = [head, '- 最早完成：' + '、'.join(f'{n}（{_stamp(t)}）' if t else n for t, n in who[:5])]
        if len(who) > 5:
            lines.append('- 最近完成：' + '、'.join(f'{n}（{_stamp(t)}）' if t else n for t, n in who[-3:][::-1]))
        return '\n'.join(lines) + '\n' + note
    player = str(player or '').strip()
    if not player:
        ranked = sorted(people, key=lambda p: (-len(p['done']), p['name']))
        ranked = [p for p in ranked if p['done']]
        top = ranked[:max(1, min(int(limit or 10), 20))]
        lines = [f'进度完成排行（共 {total} 个进度，{len(ranked)} 名玩家有记录）：'] + [
            f'{i}. {p["name"]}：{len(p["done"])} 个（{len(p["done"]) * 100 // total}%）' for i, p in enumerate(top, 1)]
        if ranked:
            lines.append(f'中位数 {len(ranked[len(ranked) // 2]["done"])} 个。')
            # 10-28 07:15 "全服玩家最多完成的是哪个成就": she had to query advancements one by one.
            popular = sorted(counts, key=lambda i: (-counts[i], index[i]['name']))[:10]
            lines.append('全服完成人数最多的进度：' + '、'.join(
                f'「{index[i]["name"]}」（{index[i]["mod"]}，{counts[i]} 人）' for i in popular))
        return '\n'.join(lines) + '\n' + note
    person, message = _match(people, player, score=lambda p: len(p['done']))
    if not person:
        return message or f'没有找到 {player} 的进度记录；名字要和游戏内一致。'
    done = person['done']
    lines = [f'玩家 {person["name"]} 的进度：', f'- 已完成 {len(done)}/{total} 个（{len(done) * 100 // total}%）']
    by_mod, all_mod = Counter(index[i]['mod'] for i in done), Counter(row['mod'] for row in index.values())
    if by_mod:
        lines.append('- 按模组（完成最多的）：' + '、'.join(f'{m} {n}/{all_mod[m]}（{n * 100 // all_mod[m]}%）'
                                                for m, n in by_mod.most_common(8)))
        # 10-28 07:19 asked for the mods with the highest share; tiny mods (1/1) would top any list.
        share = sorted((m for m in by_mod if all_mod[m] >= 5), key=lambda m: (-by_mod[m] / all_mod[m], m))[:5]
        if share:
            lines.append('- 完成比例最高的模组（至少 5 个进度）：' + '、'.join(
                f'{m} {by_mod[m] * 100 // all_mod[m]}%（{by_mod[m]}/{all_mod[m]}）' for m in share))
    challenges = [i for i in done if index[i]['frame'] == 'challenge']
    lines.append(f'- 其中挑战类 {len(challenges)}/{sum(1 for r in index.values() if r["frame"] == "challenge")} 个')
    try:
        shown = max(1, min(int(limit or 5), 20))
    except (TypeError, ValueError):
        shown = 5
    earliest = str(order or '') == 'earliest'
    recent = sorted(((t, i) for i, t in done.items() if t), reverse=not earliest)[:shown]
    if recent:
        lines.append(('- 最早完成：' if earliest else '- 最近完成：')
                     + '、'.join(f'「{index[i]["name"]}」（{index[i]["mod"]}，{_stamp(t)}）' for t, i in recent))
    stamps = sorted(t for t in done.values() if t)
    if stamps:
        # 10-28 07:20 "我还没开始忙loot，不知道哪儿来的成就": Lootr and many root advancements are
        # granted on the first join; this player got 10+ of them in the same minute.
        first = [t for t in stamps if t - stamps[0] <= 60]
        if len(first) >= 5:
            lines.append(f'- 第一次进服时（{_stamp(stamps[0])}）一分钟内就得到 {len(first)} 个，'
                         '多半是各模组进服就自动给的入门进度，不是自己做出来的')
    rare = sorted(done, key=lambda i: (counts.get(i, 0), index[i]['name']))[:3]
    rare = [i for i in rare if counts.get(i, 0) <= 3]
    if rare:
        lines.append('- 少有人完成的：' + '、'.join(f'「{index[i]["name"]}」（全服 {counts[i]} 人）' for i in rare))
    return '\n'.join(lines) + '\n' + note
