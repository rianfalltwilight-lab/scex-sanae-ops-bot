"""Live world status from read-only server commands plus the saved chunk-ticket snapshot.

Sources (all read-only):
  forceload query (per dimension)   vanilla /forceload chunks, live
  mio_icif chunkloaders             SI chunk loaders: owner, position, chunks held, live
  neoforge entity list ["<type>"]   entity counts by type, or by chunk for one type, live
  neoforge tps / mio_icif perf      tick time per dimension, SI energy network timing, live
  statistics block-entities|entities  ServerCore counts of ticking block entities / entities by type
                                    or by online player (each player's surrounding loaded chunks), live
  servercore status                 ServerCore's current simulation/view/chunk-tick distances, live
  tick query                        average tick time and P50/P95/P99 over the last 100 ticks, live
  chesirtab status                  the per-chunk entity limiter: entities tracked and removed, live
  neoforge track <kind>             per block entity / entity update time after an admin-started sample
  remote host (SSH, read-only)      process RSS/CPU, JVM heap and uptime (jcmd GC.heap_info, VM.uptime)
  <ops>/forced-chunks.json          every force-loading ticket from world/**/data/chunks.dat
                                    (vanilla + mods such as AE2 spatial anchors), as of the last save
Coordinates are shown only when `detailed` is set (admins): PvP is on and loader or
farm positions are player bases.
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
VANILLA_DIMENSIONS = ('minecraft:overworld', 'minecraft:the_nether', 'minecraft:the_end')
DIMENSION_NAMES = {'minecraft:overworld': '主世界', 'minecraft:the_nether': '下界', 'minecraft:the_end': '末地',
                   'twilightforest:twilight_forest': '暮色森林', 'thaumcraft:outer_lands': '神秘时代·外域',
                   'mythicbotany:alfheim': '亚尔夫海姆', 'ae2:spatial_storage': 'AE2 空间存储',
                   'thaumichorizons:pocket_plane': '口袋位面', 'thaumic_tinkerer:bedrock': '基岩维度',
                   'woot_revived:drop_simulator': 'Woot 掉落模拟'}
CONTROLLER_NAMES = {'ae2:default': 'AE2 空间锚', 'mio_icif:paid_chunk_loader': 'SI 区块加载器'}
ENTITY_NAMES = {'minecraft:item': '掉落物', 'minecraft:experience_orb': '经验球', 'minecraft:zombie': '僵尸',
                'minecraft:skeleton': '骷髅', 'minecraft:creeper': '苦力怕', 'minecraft:spider': '蜘蛛',
                'minecraft:item_frame': '物品展示框', 'minecraft:villager': '村民', 'minecraft:player': '玩家',
                'minecraft:arrow': '箭', 'minecraft:bat': '蝙蝠', 'minecraft:cow': '牛', 'minecraft:pig': '猪',
                'minecraft:sheep': '羊', 'minecraft:chicken': '鸡', 'minecraft:armor_stand': '盔甲架',
                'minecraft:hopper_minecart': '漏斗矿车', 'minecraft:minecart': '矿车'}
_ID = re.compile(r'^[a-z0-9_.-]+:[a-z0-9_./-]+$')


def _clock(value):
    return datetime.fromtimestamp(float(value), TZ).strftime('%m-%d %H:%M')


def dim_label(dim):
    return DIMENSION_NAMES.get(dim, dim)


def entity_label(entity_id):
    name = ENTITY_NAMES.get(entity_id)
    return f'{name}（{entity_id}）' if name else entity_id


# ===== parsers for command output =====

def parse_forceload(text):
    """Chunk list [[x, z], ...] from `forceload query`; None when the output is not understood."""
    text = str(text or '')
    if re.search(r'No force loaded chunks were found', text):
        return []
    match = re.search(r'force loaded chunks? (?:was|were) found in .+? at:\s*(.*)', text, re.S)
    if not match:
        return None
    return [[int(x), int(z)] for x, z in re.findall(r'\[(-?\d+),\s*(-?\d+)\]', match.group(1))]


def parse_chunkloaders(text):
    """{'loaded', 'working', 'held', 'loaders': [{dim, x, y, z, owner, held, wanted, state}]} or None."""
    text = str(text or '')
    head = re.search(r'SI chunk loaders:\s*(\d+) loaded,\s*(\d+) working,\s*(\d+) chunks held', text)
    if not head:
        return None
    loaders = []
    for m in re.finditer(r'^([a-z0-9_.-]+:[a-z0-9_./-]+) (-?\d+) (-?\d+) (-?\d+) - (.+?) - (\d+)/(\d+) chunks - (\S+)\s*$',
                         text, re.M):
        owner = m.group(5).strip()
        loaders.append({'dim': m.group(1), 'x': int(m.group(2)), 'y': int(m.group(3)), 'z': int(m.group(4)),
                        'owner': '' if owner == '(no owner)' else owner,
                        'held': int(m.group(6)), 'wanted': int(m.group(7)), 'state': m.group(8)})
    return {'loaded': int(head.group(1)), 'working': int(head.group(2)), 'held': int(head.group(3)),
            'loaders': loaders}


def parse_entity_list(text):
    """(total, [(count, label)]) from `neoforge entity list` or a filtered list; (None, []) if not understood."""
    text = str(text or '')
    total = re.search(r'Total:\s*(\d+)', text)
    if not total:
        return None, []
    rows = [(int(count), label.strip()) for count, label in re.findall(r'^\s*(\d+):\s*(.+?)\s*$', text, re.M)]
    return int(total.group(1)), rows


def parse_si_perf(text):
    """{'tick_ms', 'tps', 'dims': {dim: {'median', 'p95', 'max'}}} from `mio_icif perf`; None if absent."""
    text = str(text or '')
    head = re.search(r'server tick ([\d.]+) ms \(([\d.]+) TPS\)', text)
    dims = {m.group(1): {'median': float(m.group(2)), 'p95': float(m.group(3)), 'max': float(m.group(4))}
            for m in re.finditer(r'^([a-z0-9_.-]+:[a-z0-9_./-]+): energy network ([\d.]+) ms median, ([\d.]+) ms p95, '
                                 r'([\d.]+) ms max', text, re.M)}
    if not head and not dims:
        return None
    return {'tick_ms': float(head.group(1)) if head else None, 'tps': float(head.group(2)) if head else None,
            'dims': dims}


def parse_statistics(text):
    """[(label, count)] from ServerCore `statistics ...` (one page); None when the output is not understood."""
    text = str(text or '')
    if not re.search(r'(?:Block )?Entities by (?:Type|Player)', text):
        return None
    return [(label, int(count)) for label, count in re.findall(r'^\s*\d+\.\s+(\S+)\s+(\d+)\s*$', text, re.M)]


def parse_servercore(text):
    """{'simulation', 'view', 'chunk_tick', 'mobcap'} from `servercore status`; None if absent."""
    text = str(text or '')
    fields = {'simulation': r'Simulation distance', 'view': r'View distance',
              'chunk_tick': r'Chunk-tick distance', 'mobcap': r'Mobcap percentage'}
    out = {}
    for key, label in fields.items():
        match = re.search(label + r':\s*(\d+)', text)
        if match:
            out[key] = int(match.group(1))
    return out or None


def parse_tick_query(text):
    """{'average', 'target', 'p50', 'p95', 'p99', 'behind'} in ms from `tick query`; None if absent."""
    text = str(text or '')
    average = re.search(r'Average time per tick:\s*([\d.]+)\s*ms(?:\s*\(Target:\s*([\d.]+)\s*ms\))?', text)
    if not average:
        return None
    out = {'average': float(average.group(1)), 'target': float(average.group(2) or 50), 'behind': "can't keep up" in text}
    for key in ('P50', 'P95', 'P99'):
        match = re.search(key + r':\s*([\d.]+)\s*ms', text)
        out[key.lower()] = float(match.group(1)) if match else None
    return out


def tick_line(tick):
    parts = [f'{key.upper()} {tick[key]:.1f}' for key in ('p50', 'p95', 'p99') if tick.get(key) is not None]
    return (f'最近 100 刻：平均每刻 {tick["average"]:.1f} ms（目标 {tick["target"]:.0f} ms）'
            + ('，' + ' / '.join(parts) + ' ms' if parts else '')
            + ('；服务器报告跟不上目标刻率' if tick['behind'] else ''))


def parse_chesirtab(text):
    """{'tracked', 'chunks', 'removed'} from `chesirtab status` (per-chunk entity limiter); None if absent."""
    text = str(text or '')
    tracked = re.search(r'跟踪实体=(\d+)', text)
    if not tracked:
        return None
    chunks = re.search(r'跟踪区块=(\d+)', text)
    removed = re.search(r'累计清理实体=(\d+)', text)
    limit = re.search(r'区块实体上限=(\d+)', text)
    return {'tracked': int(tracked.group(1)), 'chunks': int(chunks.group(1)) if chunks else None,
            'removed': int(removed.group(1)) if removed else None, 'limit': int(limit.group(1)) if limit else None}


_TRACK_ROW = re.compile(r'^(\S+) - (\S+) \[(-?\d+), (-?\d+), (-?\d+)\]: ([\d.,]+)\s*(ms|μs|us)\s*$', re.M)


def parse_track(text):
    """[(type, dim, x, y, z, ms_per_update)] from `neoforge track blockentity|entity`.

    NeoForge records nanoseconds and prints the average divided by 1000 with an "ms" label,
    so a printed "688.52ms" is 688.52 µs; a printed "µs" value is really nanoseconds.
    """
    rows = []
    for m in _TRACK_ROW.finditer(str(text or '')):
        value = float(m.group(6).replace(',', ''))
        rows.append((m.group(1), m.group(2), int(m.group(3)), int(m.group(4)), int(m.group(5)),
                     value / 1000 if m.group(7) == 'ms' else value / 1_000_000))
    return rows


def dimension_ids(dimensions_text, snapshot=None):
    """Level ids to ask about: vanilla, those in the ticket snapshot, and id-shaped names in `neoforge dimensions`."""
    ids = list(VANILLA_DIMENSIONS)
    ids.extend((snapshot or {}).get('dims') or {})
    for line in str(dimensions_text or '').splitlines():
        if ':' not in line or line.strip().endswith(':'):
            continue
        left, _sep, right = line.strip().partition(': ')
        for value in [left] + [v.strip() for v in right.split(',')]:
            if _ID.match(value) and not value.endswith('_type'):
                ids.append(value)
    return list(dict.fromkeys(ids))


def player_positions(query, list_text):
    """[(name, dim, chunk_x, chunk_z)] for online players via `data get entity`."""
    match = re.search(r'players online:?\s*(.*)', str(list_text or ''))
    names = [n.strip() for n in (match.group(1) if match else '').split(',') if n.strip()]
    rows = []
    for name in names[:40]:
        if not re.fullmatch(r'[A-Za-z0-9_]{1,16}', name):
            continue
        try:
            dim = re.search(r'"([a-z0-9_.-]+:[a-z0-9_./-]+)"', query(f'data get entity {name} Dimension'))
            pos = re.search(r'\[(-?[\d.]+)d?,\s*(-?[\d.]+)d?,\s*(-?[\d.]+)d?\]', query(f'data get entity {name} Pos'))
        except Exception:
            continue
        if dim and pos:
            # floor, not int(): block -32.2 is in chunk -3, not -2.
            rows.append((name, dim.group(1), math.floor(float(pos.group(1))) >> 4,
                         math.floor(float(pos.group(3))) >> 4))
    return rows


def simulation_distance(properties_path, default=10):
    try:
        text = Path(properties_path).read_text(encoding='utf-8', errors='replace')
        match = re.search(r'^simulation-distance=(\d+)\s*$', text, re.M)
        return max(2, min(32, int(match.group(1)))) if match else default
    except OSError:
        return default


# ===== collection =====

def read_snapshot(ops_root):
    try:
        data = json.loads((Path(ops_root) / 'forced-chunks.json').read_text(encoding='utf-8'))
        return data if isinstance(data.get('dims'), dict) else None
    except (OSError, ValueError, AttributeError):
        return None


def collect_chunks(query, ops_root, properties_path):
    """Everything the chunk report needs; each source may be missing."""
    snapshot = read_snapshot(ops_root)
    forced = {}
    for dim in dimension_ids(_safe(query, 'neoforge dimensions'), snapshot):
        command = 'forceload query' if dim == 'minecraft:overworld' else f'execute in {dim} run forceload query'
        chunks = parse_forceload(_safe(query, command))
        if chunks is not None:
            forced[dim] = chunks
    # ServerCore may lower the simulation distance under load; its live value wins over server.properties.
    servercore = parse_servercore(_safe(query, 'servercore status'))
    return {'forced': forced, 'loaders': parse_chunkloaders(_safe(query, 'mio_icif chunkloaders')),
            'snapshot': snapshot, 'players': player_positions(query, _safe(query, 'list')),
            'simulation': (servercore or {}).get('simulation') or simulation_distance(properties_path),
            'servercore': servercore, 'collected_at': time.time()}


def _safe(query, command):
    try:
        return str(query(command) or '')
    except Exception:
        return ''


def ticking_estimate(data):
    """{dim: {'forced': n, 'players': n, 'estimate': n}}: forced chunks plus the players' simulation squares."""
    radius = data['simulation']
    area = defaultdict(set)
    forced = defaultdict(set)
    for dim, chunks in data['forced'].items():
        forced[dim].update(map(tuple, chunks))
    for dim, row in ((data['snapshot'] or {}).get('dims') or {}).items():
        for ticket in (row.get('tickets') or {}).values():
            forced[dim].update(map(tuple, ticket.get('chunks') or []))
    # Without the snapshot, SI loaders are known only by their live "chunks held" totals.
    snapshot_dims = (data['snapshot'] or {}).get('dims') or {}
    extra = Counter()
    for loader in (data['loaders'] or {}).get('loaders') or []:
        if 'mio_icif:paid_chunk_loader' not in ((snapshot_dims.get(loader['dim']) or {}).get('tickets') or {}):
            extra[loader['dim']] += loader['held']
    players = Counter()
    for _name, dim, cx, cz in data['players']:
        players[dim] += 1
        area[dim].update((x, z) for x in range(cx - radius, cx + radius + 1) for z in range(cz - radius, cz + radius + 1))
    out = {}
    for dim in set(forced) | set(area) | set(extra):
        out[dim] = {'forced': len(forced[dim]) + extra[dim], 'players': players[dim],
                    'estimate': len(forced[dim] | area[dim]) + extra[dim]}
    return out


# ===== reports =====

def format_chunks(prefix, data, detailed=False):
    snapshot = data['snapshot']
    estimate = ticking_estimate(data)
    lines = [f'{prefix} 区块与强加载（实时查询；模组票据来自存档快照'
             + (f'，存档于 {_clock(snapshot["saved_at"])}' if snapshot and snapshot.get('saved_at') else '，暂无快照')
             + '）']
    order = sorted(estimate, key=lambda d: (d not in VANILLA_DIMENSIONS, -estimate[d]['estimate']))
    idle = [dim for dim in order if not estimate[dim]['forced'] and not estimate[dim]['players']]
    for dim in order:
        row = estimate[dim]
        if dim in idle:
            continue
        parts = [f'强加载 {row["forced"]} 个区块']
        vanilla = len(data['forced'].get(dim) or [])
        if vanilla:
            parts.append(f'其中 /forceload {vanilla} 个')
        for controller, ticket in sorted((((snapshot or {}).get('dims') or {}).get(dim, {}).get('tickets') or {}).items()):
            parts.append(f'{CONTROLLER_NAMES.get(controller, controller)} {len(ticket.get("chunks") or [])} 个')
        line = f'- {dim_label(dim)}：' + '，'.join(parts)
        if row['players']:
            line += (f'；玩家 {row["players"]} 人，估计在运算的区块约 {row["estimate"]} 个'
                     f'（玩家周围按模拟距离 {data["simulation"]} 估算）')
        lines.append(line)
    if idle:
        lines.append(f'- 其他 {len(idle)} 个维度（{"、".join(dim_label(d) for d in idle[:8])}）没有强加载，也没有玩家')
    loaders = data['loaders']
    if loaders:
        owners = Counter()
        chunks = Counter()
        for loader in loaders['loaders']:
            owners[loader['owner'] or '无主'] += 1
            chunks[loader['owner'] or '无主'] += loader['held']
        lines.append(f'SI 区块加载器 {loaders["loaded"]} 个（工作中 {loaders["working"]} 个），共占用 {loaders["held"]} 个区块；按放置人：'
                     + '、'.join(f'{name} {count} 个/{chunks[name]} 区块' for name, count in owners.most_common(12)))
        if detailed:
            lines.extend(f'  · {dim_label(l["dim"])} ({l["x"]}, {l["y"]}, {l["z"]}) {l["owner"] or "无主"} '
                         f'{l["held"]}/{l["wanted"]} 区块 {l["state"]}' for l in loaders['loaders'][:30])
    if detailed:
        for dim, chunks in data['forced'].items():
            if chunks:
                lines.append(f'/forceload {dim_label(dim)}：' + '、'.join(f'[{x}, {z}]' for x, z in chunks[:40])
                             + (f' 等 {len(chunks)} 个' if len(chunks) > 40 else ''))
        for dim, row in ((snapshot or {}).get('dims') or {}).items():
            for controller, ticket in (row.get('tickets') or {}).items():
                if controller == 'mio_icif:paid_chunk_loader':
                    continue          # listed above with owners
                owners = ticket.get('owners') or []
                lines.append(f'{CONTROLLER_NAMES.get(controller, controller)}（{dim_label(dim)}）票据来源方块：'
                             + '、'.join(f'({x}, {y}, {z})' for x, y, z in owners[:12])
                             + (f' 等 {len(owners)} 处' if len(owners) > 12 else ''))
    else:
        lines.append('（坐标只对管理员显示。）')
    if data.get('servercore'):
        lines.append(servercore_line(data['servercore']))
    lines.append('说明：“在运算”只按强加载和玩家位置估算；玩家视距内只加载不运算的区块不计入。')
    return '\n'.join(lines)


def servercore_line(core):
    parts = [f'{label} {core[key]}' for key, label in (('simulation', '模拟距离'), ('view', '视距'),
                                                       ('chunk_tick', '区块刻距离')) if key in core]
    if 'mobcap' in core:
        parts.append(f'刷怪上限 {core["mobcap"]}%')
    return 'ServerCore 当前设置：' + '、'.join(parts)


def format_entities(prefix, total_text, filtered_text='', entity='', detailed=False, top=15, limiter_text=''):
    total, rows = parse_entity_list(total_text)
    if total is None:
        return f'{prefix} 实体统计暂时取不到（neoforge entity list 没有返回）。'
    lines = [f'{prefix} 全服实体（实时）共 {total} 个，最多的：'
             + '、'.join(f'{entity_label(label)} {count}' for count, label in rows[:top])]
    if entity:
        sub_total, chunks = parse_entity_list(filtered_text)
        if sub_total is None:
            lines.append(f'{entity_label(entity)}：查不到这个实体类型，名字要写完整 ID。')
        else:
            if detailed:
                spots = '、'.join(f'区块 ({label}) {count} 个' for count, label in chunks[:10])
            else:
                spots = '、'.join(str(count) for count, _label in chunks[:10]) + '（坐标只对管理员显示）'
            lines.append(f'{entity_label(entity)} 共 {sub_total} 个，最集中的区块：{spots}')
    limiter = parse_chesirtab(limiter_text)
    if limiter:
        lines.append(limiter_line(limiter))
    return '\n'.join(lines)


def limiter_line(limiter):
    return (f'超量区块保护（chesirtab）：正在跟踪 {limiter["tracked"]} 个实体'
            + (f'、{limiter["chunks"]} 个区块' if limiter.get('chunks') is not None else '')
            + (f'，每区块上限 {limiter["limit"]}' if limiter.get('limit') else '')
            + (f'，累计清理 {limiter["removed"]} 个' if limiter.get('removed') is not None else ''))


def format_lag(prefix, tps_text, perf_text, entities_text, block_text='', servercore_text='', host=None,
               names=None, tick_text=''):
    lines = [f'{prefix} 卡顿来源排查（实时）']
    dims = []
    for line in re.sub(r'§.', '', str(tps_text or '')).splitlines():
        match = re.match(r'\s*(.+?):\s*([\d.]+)\s*TPS\s*\(([\d.]+)\s*ms/tick\)', line)
        if match:
            dims.append((match.group(1).strip(), float(match.group(2)), float(match.group(3))))
    overall = next((d for d in dims if d[0].lower() == 'overall'), None)
    if overall:
        lines.append(f'整体 TPS {overall[1]:.1f}，每刻 {overall[2]:.1f} ms（50 ms 以内不掉 TPS）')
    tick = parse_tick_query(tick_text)
    if tick:
        lines.append(tick_line(tick) + '（P99 高说明偶尔有一刻特别慢，玩家会感到顿一下）')
    busy = sorted((d for d in dims if d[0].lower() != 'overall' and d[2] >= 1), key=lambda d: -d[2])[:5]
    if busy:
        lines.append('各维度耗时：' + '、'.join(f'{name} {ms:.1f} ms' for name, _tps, ms in busy))
    perf = parse_si_perf(perf_text)
    if perf and perf['dims']:
        worst = sorted(perf['dims'].items(), key=lambda item: -item[1]['median'])[:3]
        lines.append('SI 能量网络每刻耗时：' + '、'.join(
            f'{dim_label(dim)} 中位 {row["median"]:.1f} ms / p95 {row["p95"]:.1f} / 最高 {row["max"]:.1f}'
            for dim, row in worst if row['max'] >= 0.5) or 'SI 能量网络耗时很低')
    total, rows = parse_entity_list(entities_text)
    if total is not None:
        heavy = [(count, label) for count, label in rows if label in ('minecraft:item', 'minecraft:experience_orb')]
        lines.append(f'实体共 {total} 个' + ('，其中 ' + '、'.join(f'{entity_label(label)} {count}' for count, label in heavy)
                                         if heavy else ''))
    blocks = parse_statistics(block_text)
    if blocks:
        lines.append('正在运算的方块实体最多的：' + '、'.join(
            f'{block_label(label, names)} {count}' for label, count in blocks[:5]))
    core = parse_servercore(servercore_text)
    if core:
        lines.append(servercore_line(core))
    heap = parse_heap((host or {}).get('heap'))
    if heap:
        lines.append(f'JVM 堆内存已用 {_gb(heap["used"])} / {_gb(heap["total"])}'
                     f'（{heap["used"] * 100 // max(1, heap["total"])}%，瞬时值，含还没回收的垃圾）')
    lines.append('数字是此刻的瞬时值；要定位具体机器，管理员可以查掉落物/经验球最集中的区块。')
    return '\n'.join(lines)


def block_label(block_id, names=None):
    """Block entity type with its block name when the item index knows it."""
    if block_id == '<lithium_sleeping>':
        return '休眠中的方块实体（Lithium 已暂停，几乎不耗算力）'
    entry = (names or {}).get(block_id) or {}
    label = (entry.get('zh') or entry.get('en')) if isinstance(entry, dict) else ''
    label = str(label or '').replace('<MISSING>', '').strip()      # untranslated part of a mod's name
    return f'{label}（{block_id}）' if label else block_id


def format_machines(prefix, by_type, by_type_more='', by_player='', entities_by_player='', names=None, top=16):
    """Ticking block entities by type and per online player, from ServerCore statistics."""
    rows = (parse_statistics(by_type) or []) + (parse_statistics(by_type_more) or [])
    if not rows:
        return f'{prefix} 方块实体统计暂时取不到（ServerCore statistics 没有返回）。'
    lines = [f'{prefix} 正在运算的方块实体（实时；只统计已加载区块里每刻都要处理的方块，例如机器、漏斗、刷怪笼）',
             '最多的种类：' + '、'.join(f'{block_label(label, names)} {count}' for label, count in rows[:top])]
    players = parse_statistics(by_player)
    if players:
        lines.append('按在线玩家（统计各玩家周围已加载的区块）：'
                     + '、'.join(f'{name} {count}' for name, count in players[:10]))
    entities = parse_statistics(entities_by_player)
    if entities:
        lines.append('实体按在线玩家：' + '、'.join(f'{name} {count}' for name, count in entities[:10]))
    lines.append('数字是此刻的瞬时值，只含加载中的区块；不在线玩家的基地如果没被强加载，不计入。')
    return '\n'.join(lines)


# ===== remote host =====

def _gb(kb):
    return f'{kb / 1024 / 1024:.1f} GB'


def parse_heap(text):
    """{'total', 'used', 'metaspace'} in KiB from `jcmd <pid> GC.heap_info`; None if absent."""
    text = str(text or '')
    match = re.search(r'heap\s+total\s+(\d+)K,\s*used\s+(\d+)K', text)
    if not match:
        return None
    meta = re.search(r'Metaspace\s+used\s+(\d+)K', text)
    return {'total': int(match.group(1)), 'used': int(match.group(2)),
            'metaspace': int(meta.group(1)) if meta else None}


def _elapsed_seconds(etime):
    """ps etime `[[dd-]hh:]mm:ss` in seconds; None if not understood."""
    match = re.fullmatch(r'(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)', str(etime or '').strip())
    if not match:
        return None
    days, hours, minutes, seconds = (int(v or 0) for v in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _duration(seconds):
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f'{days} 天 {hours} 小时'
    if hours:
        return f'{hours} 小时 {minutes} 分钟'
    return f'{minutes} 分钟'


def format_host(prefix, host, now=None):
    """Process memory, CPU and uptime of the server JVM; disk and backups are left out on purpose."""
    process = (host or {}).get('process')
    if not process:
        return f'{prefix} 主机上没有找到服务端进程（可能正在重启），或主机暂时连不上。'
    lines = [f'{prefix} 服务端进程状态（实时，来自服务器主机）']
    uptime = re.search(r'([\d.]+)\s*s\b', str(host.get('uptime') or ''))
    seconds = float(uptime.group(1)) if uptime else _elapsed_seconds(process.get('etime'))
    if seconds is not None:
        started = (now if now is not None else float(host.get('collected_at') or time.time())) - seconds
        lines.append(f'- 已连续运行 {_duration(seconds)}（上次启动于 {_clock(started)}）')
    heap = parse_heap(host.get('heap'))
    if heap:
        line = (f'- JVM 堆内存：已用 {_gb(heap["used"])} / 当前分配 {_gb(heap["total"])}'
                f'（{heap["used"] * 100 // max(1, heap["total"])}%）')
        if process.get('xmx'):
            line += f'，上限 -Xmx{process["xmx"]}'
        if heap['metaspace']:
            line += f'；类元数据 {heap["metaspace"] / 1024:.0f} MB'
        lines.append(line)
    memory = int(host.get('memory') or 0)
    lines.append(f'- 进程实际占用内存 {_gb(int(process["rss_kb"]))}'
                 + (f'（主机共 {memory / 1024 ** 3:.0f} GB）' if memory else ''))
    cores = int(host.get('cores') or 0)
    cpu = float(process.get('cpu') or 0)
    lines.append(f'- CPU：{cpu:.0f}%' + (f'（按单核计，主机 {cores} 核，约占整机 {cpu / cores:.0f}%）' if cores else '（按单核计）'))
    lines.append('说明：“已用”含还没回收的垃圾，会随垃圾回收锯齿式涨落，单次读数高不代表内存不够；'
                 '要连续几次都贴近上限、同时 TPS 下降才需要关注。CPU 是进程最近的平均占用。')
    return '\n'.join(lines)


def lag_alert_lines(entities_text, perf_text, item_text='', orb_text='', block_text='', servercore_text='',
                    host=None, tick_text=''):
    """Extra lines for the admin-group lag alert (coordinates allowed there)."""
    lines = []
    tick = parse_tick_query(tick_text)
    if tick:
        lines.append(tick_line(tick))
    total, rows = parse_entity_list(entities_text)
    if total is not None:
        lines.append(f'实体共 {total} 个：' + '、'.join(f'{entity_label(label).split("（")[0]} {count}'
                                                    for count, label in rows[:4]))
    for text in (item_text, orb_text):
        sub_total, chunks = parse_entity_list(text)
        match = re.search(r'Entity:\s*(\S+)', str(text or ''))
        if sub_total and chunks and match:
            lines.append(f'{entity_label(match.group(1)).split("（")[0]}最集中：'
                         + '、'.join(f'区块 ({label}) {count} 个' for count, label in chunks[:3]))
    perf = parse_si_perf(perf_text)
    if perf and perf['dims']:
        dim, row = max(perf['dims'].items(), key=lambda item: item[1]['median'])
        if row['median'] >= 1:
            lines.append(f'SI 能量网络（{dim_label(dim)}）中位 {row["median"]:.1f} ms / 最高 {row["max"]:.1f} ms')
    blocks = parse_statistics(block_text)
    if blocks:
        lines.append('运算中的方块实体：' + '、'.join(f'{label} {count}' for label, count in blocks[:4]))
    core = parse_servercore(servercore_text)
    if core:
        lines.append(servercore_line(core))
    heap = parse_heap((host or {}).get('heap'))
    if heap:
        lines.append(f'JVM 堆 {_gb(heap["used"])} / {_gb(heap["total"])}（{heap["used"] * 100 // max(1, heap["total"])}%）')
    return lines


# ===== admin-started timing sample =====

PROFILE_KINDS = {'blockentity': '方块实体', 'entity': '实体'}


def format_profile(prefix, kind, track_text, seconds, tick_text='', names=None):
    """The ten slowest block entities or entities of a NeoForge timing sample, with coordinates (admins)."""
    rows = parse_track(track_text)
    label = PROFILE_KINDS.get(kind, kind)
    if not rows:
        return f'{prefix} {label}耗时采样没有拿到数据（{str(track_text or "").strip()[:80] or "无输出"}）。'
    lines = [f'{prefix} {label}耗时采样（{seconds} 秒；每项是最近约 100 次更新的平均耗时，取最慢的 {len(rows)} 个）']
    for i, (ident, dim, x, y, z, ms) in enumerate(rows, 1):
        name = block_label(ident, names) if kind == 'blockentity' else entity_label(ident)
        lines.append(f'{i}. {name} {dim_label(dim)} ({x}, {y}, {z})：每次约 {ms:.2f} ms')
    total = sum(row[5] for row in rows)
    tick = parse_tick_query(tick_text)
    line = f'这 {len(rows)} 个合计每刻约 {total:.1f} ms'
    if tick:
        line += f'，占现在平均每刻 {tick["average"]:.1f} ms 的 {total * 100 / max(0.1, tick["average"]):.0f}%'
    lines.append(line + '。')
    lines.append('说明：NeoForge 原始输出把微秒标成了 ms，这里已换算；采样已自动结束。')
    return '\n'.join(lines)
