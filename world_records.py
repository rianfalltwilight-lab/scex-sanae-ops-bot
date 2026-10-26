"""Saved world records read on demand: the SI futures market and Thaumcraft's world data.

Sources (remote_server_reader, read-only SSH, as of the server's last save):
  market_snapshot    world/data/mio_icif_future_market.dat and mio_icif_exchange.dat: prices,
                     20-day history, market events, commissions, daily bars, coin ledger.
                     Per-player mailbox coins, positions and volume stay on the host.
  thaumcraft_world   world/**/data/thaumcraft_aura_nodes.dat, Outer Lands mazes and boss rooms,
                     shock fields. Coordinates are kept only for tainted/hungry/energized nodes
                     and maze portals, and shown only to admins.
Each reader is cached for a short time so a burst of questions costs one SSH call.
"""
from __future__ import annotations

import json
import re
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import remote_server_reader
import server_insight

TZ = timezone(timedelta(hours=8))
MARKET_CACHE_SECONDS = 120
TC_WORLD_CACHE_SECONDS = 600
EVENT_NAMES = {'RISING_TREND': '上涨趋势', 'FALLING_TREND': '下跌趋势', 'HIGH_VOLATILITY': '剧烈波动',
               'LOW_VOLATILITY': '走势平稳', 'LONG_BUBBLE': '长期泡沫', 'SHORT_BUBBLE': '短期泡沫',
               'LONG_CRASH_REBOUND': '暴跌后长期反弹', 'SHORT_CRASH_REBOUND': '暴跌后短期反弹',
               'PRICE_STABILIZATION': '价格回稳'}
NODE_TYPES = {'NORMAL': '普通', 'DARK': '凶险', 'PURE': '纯净', 'UNSTABLE': '震荡', 'HUNGRY': '饕餮', 'TAINTED': '污染'}
NODE_MODIFIERS = {'0': '明亮', '1': '苍白', '2': '衰退'}
RARE_NODE_TYPES = ('TAINTED', 'HUNGRY')
MAZE_STATES = {'reserved': '已登记入口、内部未生成', 'complete': '已完整生成', 'generating': '生成中'}
BOSS_TYPES = {'warden': '守望者', 'taint': '腐化触手群', 'golem': '傀儡', 'cultist': '邪教徒'}
BOSS_STATES = {'defeated': '已击败', 'active': '战斗中', 'dormant': '未触发', 'spawned': '已出现'}
VANILLA_LANG = Path(__file__).with_name('resources') / 'minecraft-1.21.1' / 'lang'
DIM_NAMES = {'minecraft:overworld': '主世界', 'minecraft:the_nether': '下界', 'minecraft:the_end': '末地',
             'thaumcraft:outer_lands': '外域'}
_CACHE = {}
_LOCK = threading.Lock()


def _stamp(value):
    return datetime.fromtimestamp(float(value), TZ).strftime('%m-%d %H:%M')


def _cached(key, seconds, loader):
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < seconds:
            return hit[1]
    data = loader()
    with _LOCK:
        _CACHE[key] = (time.time(), data)
    return data


def item_names(ops_root):
    """The recipe index's item names, with vanilla zh/en names from the bundled lang files filled in."""
    key = ('vanilla-names', str(VANILLA_LANG))
    with _LOCK:
        vanilla = _CACHE.get(key, (0, None))[1]
    if vanilla is None:
        vanilla = {}
        for language, field in (('en_us', 'en'), ('zh_cn', 'zh')):
            try:
                lang = json.loads((VANILLA_LANG / f'{language}.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            for name, value in lang.items():
                match = re.fullmatch(r'(?:item|block)\.minecraft\.([a-z0-9_]+)', name)
                if match and isinstance(value, str):
                    vanilla.setdefault('minecraft:' + match.group(1), {})[field] = value
        with _LOCK:
            _CACHE[key] = (time.time(), vanilla)
    merged = {k: dict(v) for k, v in vanilla.items()}
    # The recipe index lists vanilla items with empty names; only its non-empty names win.
    for item, entry in (server_insight.item_names(ops_root) or {}).items():
        if isinstance(entry, dict):
            row = merged.setdefault(item, {})
            row.update({k: v for k, v in entry.items() if v})
    return merged


def market(server):
    return _cached(('market', server['id']), MARKET_CACHE_SECONDS, lambda: remote_server_reader.market_snapshot(server))


def thaumcraft(server):
    return _cached(('tc', server['id']), TC_WORLD_CACHE_SECONDS, lambda: remote_server_reader.thaumcraft_world(server))


def _label(names, item_id):
    entry = (names or {}).get(str(item_id)) or {}
    label = (entry.get('zh') or entry.get('en')) if isinstance(entry, dict) else ''
    label = str(label or '').replace('<MISSING>', '').strip()
    return label or str(item_id)


def _match_items(query, ids, names):
    """Item ids whose id or zh/en name matches; an exact hit wins."""
    wanted = str(query or '').strip().casefold()
    exact, partial = [], []
    for item in ids:
        entry = (names or {}).get(item) or {}
        labels = [item, item.split(':', 1)[-1]] + ([entry.get('zh') or '', entry.get('en') or ''] if isinstance(entry, dict) else [])
        labels = [str(v).casefold() for v in labels if v]
        if wanted in labels:
            exact.append(item)
        elif any(wanted in v for v in labels):
            partial.append(item)
    return exact or partial


def _change(history, days):
    """Percent change of the last price against `days` days earlier; None without enough history."""
    if len(history) <= days or not history[-1 - days]:
        return None
    return (history[-1] - history[-1 - days]) * 100 / history[-1 - days]


def _reward(names, row):
    """A commission's reward with its unit: RewardKind is COIN (exchange coins) or ITEM (RewardItem x RewardTotal)."""
    total = row.get('reward')
    kind = str(row.get('kind') or '').upper()
    reward_item = row.get('reward_item') or ''
    if isinstance(reward_item, dict):
        reward_item = reward_item.get('id') or reward_item.get('Item') or ''
    if kind == 'ITEM' and reward_item:
        return f'{_label(names, reward_item)} ×{total}'
    if kind == 'ITEM':
        return f'{total} 个物品（存档没写是哪种）'
    if kind in ('', 'COIN'):
        return f'{total} 币'
    return f'{total}（{kind}）'


def format_market(prefix, data, names=None, item=''):
    prices = (data or {}).get('prices') or {}
    if not prices:
        return f'{prefix} 市场数据暂时取不到（存档里没有期货市场，或主机连不上）。'
    history = data.get('history') or {}
    events = data.get('events') or {}
    day = data.get('day')
    saved = f'，存档于 {_stamp(data["saved_at"])}' if data.get('saved_at') else ''
    note = f'价格按游戏天结算（现在第 {day} 天{saved}）；单位是交易所的币。'
    if item:
        found = _match_items(item, prices, names)
        if not found:
            return f'{prefix} 期货市场里没有「{item}」。市场只交易 {len(prices)} 种商品，例如：' + '、'.join(
                _label(names, k) for k in list(prices)[:12]) + '。'
        if len(found) > 1:
            return f'{prefix} 有多种商品匹配「{item}」：' + '、'.join(
                f'{_label(names, k)} {prices[k]}' for k in found[:10]) + '。请说具体一点。'
        key = found[0]
        rows = [int(v) for v in history.get(key) or []]
        lines = [f'{prefix} {_label(names, key)}（{key}）现价 {prices[key]}']
        if rows:
            lines.append(f'- 最近 {min(10, len(rows))} 天：' + ' → '.join(str(v) for v in rows[-10:])
                         + f'（近 {len(rows)} 天最低 {min(rows)}、最高 {max(rows)}）')
            parts = [f'{label} {value:+.0f}%' for label, value in (('1 天', _change(rows, 1)), ('5 天', _change(rows, 5)))
                     if value is not None]
            if parts:
                lines.append('- 涨跌：' + '，'.join(parts))
        for event in events.get(key) or []:
            lines.append(f'- 进行中的行情：{EVENT_NAMES.get(event["type"], event["type"])}'
                         f'（第 {event["start"]} 天开始，持续 {event["duration"]} 天，强度 {event["intensity"]}）')
        bars = (data.get('bars') or {}).get(key) or []
        if bars:
            volume = sum(int(b.get('volume') or 0) for b in bars)
            lines.append(f'- 交易所近 {len(bars)} 天成交量 {volume}' + ('（没有玩家成交）' if not volume else ''))
        for row in data.get('commissions') or []:
            if row.get('item') == key:
                lines.append(f'- 有收购委托：{row["quantity"]} 个，报酬 {_reward(names, row)}，第 {row["expiry"]} 天截止')
        return '\n'.join(lines) + '\n' + note
    moves = []
    for key, price in prices.items():
        value = _change([int(v) for v in history.get(key) or []], 5)
        if value is not None:
            moves.append((value, key, price))
    moves.sort()
    lines = [f'{prefix} 期货市场（{len(prices)} 种商品）']
    rising = [m for m in moves[::-1] if m[0] > 0][:5]
    falling = [m for m in moves if m[0] < 0][:5]
    if rising:
        lines.append('5 天涨得最多：' + '、'.join(f'{_label(names, k)} {p}（{v:+.0f}%）' for v, k, p in rising))
    if falling:
        lines.append('5 天跌得最多：' + '、'.join(f'{_label(names, k)} {p}（{v:+.0f}%）' for v, k, p in falling))
    if events:
        counts = {}
        for rows in events.values():
            for event in rows:
                counts[event['type']] = counts.get(event['type'], 0) + 1
        lines.append(f'进行中的行情事件 {sum(counts.values())} 个：' + '、'.join(
            f'{EVENT_NAMES.get(k, k)} {v}' for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    open_rows = [r for r in data.get('commissions') or [] if (r.get('delivered') or 0) < (r.get('quantity') or 0)]
    if open_rows:
        lines.append('收购委托：' + '、'.join(
            f'{_label(names, r["item"])} ×{r["quantity"]}，报酬 {_reward(names, r)}（第 {r["expiry"]} 天截止'
            + ('，系统发布' if r.get('system') else (f'，{r["requester"]} 发布' if r.get('requester') else '')) + '）'
            for r in open_rows[:6]))
    ledger = data.get('ledger') or []
    if ledger:
        minted = sum(int(r.get('minted') or 0) for r in ledger)
        burned = sum(int(r.get('burned') or 0) for r in ledger)
        lines.append(f'近 {len(ledger)} 天交易所新发行 {minted} 币、回收 {burned} 币')
    return '\n'.join(lines) + '\n' + note


# ===== Thaumcraft world =====

def aspect_names(mods_dir):
    """tc.aspect.<key> -> first Chinese name, from the mirrored Thaumcraft jar (cached by mtime)."""
    jar = next(iter(sorted(Path(mods_dir).glob('thaumcraft-*.jar'))), None) if mods_dir else None
    if not jar:
        return {}
    key = ('aspects', str(jar), jar.stat().st_mtime)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key][1]
    names = {}
    try:
        with zipfile.ZipFile(jar) as archive:
            lang = json.loads(archive.read('assets/thaumcraft/lang/zh_cn.json').decode('utf-8-sig'))
        names = {k[len('tc.aspect.'):]: re.split(r'[,，]', v)[0] for k, v in lang.items()
                 if k.startswith('tc.aspect.') and isinstance(v, str) and '.' not in k[len('tc.aspect.'):]}
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        pass
    with _LOCK:
        _CACHE[key] = (time.time(), names)
    return names


def _aspect(names, key):
    name = (names or {}).get(key)
    return f'{name}（{key}）' if name else str(key)


def _dim(dim):
    return DIM_NAMES.get(dim, dim)


def _node_type_key(value):
    value = str(value or '').strip()
    for key, label in NODE_TYPES.items():
        if value.upper() == key or value in (label, label + '节点'):
            return key
    return ''


def format_thaumcraft_world(prefix, data, topic='nodes', node_type='', aspect='', detailed=False, names=None):
    data = data or {}
    if topic == 'outer_lands':
        return _format_outer_lands(prefix, data, detailed)
    dims = data.get('dims') or {}
    if not dims:
        return f'{prefix} 灵气节点数据暂时取不到（存档里没有节点文件，或主机连不上）。'
    saved = max(float(row.get('saved_at') or 0) for row in dims.values())
    note = f'数据来自存档（{_stamp(saved)}），只含已经生成过的区块里的节点。' if saved else '数据来自存档。'
    order = sorted(dims, key=lambda d: -dims[d]['count'])
    if node_type:
        key = _node_type_key(node_type)
        if not key:
            return f'{prefix} 节点类型只有：' + '、'.join(NODE_TYPES.values()) + '。'
        lines = [f'{prefix} {NODE_TYPES[key]}节点：' + '、'.join(
            f'{_dim(d)} {dims[d]["types"].get(key, 0)} 个' for d in order)]
        if detailed and key in RARE_NODE_TYPES:
            for dim in order:
                rows = [r for r in dims[dim].get('rare') or [] if r.get('type') == key]
                if rows:
                    lines.append(f'{_dim(dim)}（前 {min(15, len(rows))} 个）：' + '；'.join(
                        f'({r["pos"][0]}, {r["pos"][1]}, {r["pos"][2]}) ' + '、'.join(
                            f'{_aspect(names, a)} {n}' for a, n in r['aspects']) for r in rows[:15]))
        elif detailed:
            lines.append('只有污染、饕餮和充能节点保存了坐标。')
        else:
            lines.append('（坐标只对管理员显示。）')
        return '\n'.join(lines) + '\n' + note
    if aspect:
        wanted = str(aspect).strip().casefold()
        keys = {k for row in dims.values() for k in row.get('aspects') or {}}
        hit = next((k for k in keys if wanted in (k.casefold(), str((names or {}).get(k, '')).casefold())), None)
        if not hit:
            return f'{prefix} 节点里没有「{aspect}」这种要素。'
        return (f'{prefix} 含{_aspect(names, hit)}的节点：' + '、'.join(
            f'{_dim(d)} {dims[d]["holders"].get(hit, 0)} 个（合计 {dims[d]["aspects"].get(hit, 0)} 点）' for d in order)
                + '\n' + note)
    lines = [f'{prefix} 神秘时代灵气节点']
    for dim in order:
        row = dims[dim]
        types = sorted(row.get('types', {}).items(), key=lambda kv: -kv[1])
        mods = '、'.join(f'{NODE_MODIFIERS[k]} {v}' for k, v in sorted(row.get('modifiers', {}).items())
                        if k in NODE_MODIFIERS)
        lines.append(f'- {_dim(dim)} {row["count"]} 个：' + '、'.join(f'{NODE_TYPES.get(k, k)} {v}' for k, v in types)
                     + f'；充能 {row.get("energized", 0)} 个' + (f'；亮度：{mods}' if mods else ''))
        top = sorted((row.get('aspects') or {}).items(), key=lambda kv: -kv[1])[:6]
        if top:
            lines.append('  要素储量最多：' + '、'.join(
                f'{_aspect(names, k)} {v}（{row["holders"].get(k, 0)} 个节点）' for k, v in top))
    if data.get('shock_fields'):
        lines.append('触电场：' + '、'.join(f'{_dim(d)} {n} 处' for d, n in data['shock_fields'].items()))
    return '\n'.join(lines) + '\n' + note


def _format_outer_lands(prefix, data, detailed):
    mazes = data.get('mazes') or {}
    bosses = data.get('bosses') or {}
    if not mazes and not bosses:
        return f'{prefix} 外域数据暂时取不到（还没人去过外域，或主机连不上）。'
    lines = [f'{prefix} 神秘时代·外域']
    if mazes:
        states = '、'.join(f'{MAZE_STATES.get(k, k)} {v}' for k, v in sorted(mazes.get('states', {}).items(),
                                                                              key=lambda kv: -kv[1]))
        lines.append(f'- 迷宫 {mazes["count"]} 个：{states}；已生成的房间格 {mazes.get("generated_cells", 0)} 个')
        sizes = sorted((mazes.get('sizes') or {}).items(), key=lambda kv: -kv[1])[:3]
        if sizes:
            lines.append('- 尺寸：' + '、'.join(f'{k.replace("x", "×")} {v} 个' for k, v in sizes))
        if detailed and mazes.get('portals'):
            lines.append('- 入口传送门（前 15 个）：' + '；'.join(
                f'{_dim(p["dim"])} ({p["pos"][0]}, {p["pos"][1]}, {p["pos"][2]}) {MAZE_STATES.get(p["state"], p["state"])}'
                for p in mazes['portals'][:15]))
        elif not detailed:
            lines.append('（入口坐标只对管理员显示。）')
    rooms = bosses.get('rooms') or []
    if rooms or bosses.get('count'):
        lines.append(f'- 首领房间 {bosses.get("count") or len(rooms)} 个，有记录的 {len(rooms)} 个：' + '、'.join(
            f'{BOSS_TYPES.get(r["type"], r["type"])} {BOSS_STATES.get(r["state"], r["state"])}'
            + (f'（击败 {r["defeated"]}/{r["spawned"]}）' if r.get('spawned', 0) > 1 else '') for r in rooms))
    return '\n'.join(lines) + '\n数据来自外域存档，玩家进入新迷宫后要等服务器存盘才会更新。'
