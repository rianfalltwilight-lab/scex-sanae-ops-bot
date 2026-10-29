"""Server watch logic without I/O side effects beyond the files it is given.

  lag_step / format_lag_alert   sustained low TPS from the per-minute RCON samples
  mod_changes / format_update   what changed between two mod inventories
  snapshot_player_stats         one compact copy of player-stats.json per day
  player_week / format_week     weekly deltas for the weekly report
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))


def _clock(value):
    return datetime.fromtimestamp(float(value), TZ).strftime('%m-%d %H:%M')


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ===== sustained lag =====

def lag_step(state, row, threshold=10.0, minutes=5, max_gap=150, repeat=3600):
    """Feed one runtime sample; returns (state, streak or None).

    A streak is consecutive valid samples with TPS below threshold, no gap longer
    than max_gap seconds between them. It is returned when it reaches `minutes`
    samples spanning at least (minutes - 1) minutes, and again every `repeat`
    seconds while it lasts. A failed or missing reading ends the streak: an
    unreachable server is not "lag". Keys other than the streak are kept.
    """
    state = dict(state or {})
    stamp, tps, mspt = _num(row.get('timestamp')), _num(row.get('tps')), _num(row.get('mspt'))
    if stamp is None or tps is None or row.get('tick_parser', 2) != 2 or tps >= threshold:
        state.update(start=None, last=None, samples=0, min_tps=None, mspt_sum=0.0, reported_at=None)
        return state, None
    if state.get('start') is None or stamp - float(state.get('last') or 0) > max_gap:
        state.update(start=stamp, samples=0, min_tps=tps, mspt_sum=0.0, reported_at=None)
    state['last'] = stamp
    state['samples'] = int(state.get('samples') or 0) + 1
    state['min_tps'] = min(float(state.get('min_tps') if state.get('min_tps') is not None else tps), tps)
    state['mspt_sum'] = float(state.get('mspt_sum') or 0) + (mspt or 0)
    reported = state.get('reported_at')
    if (state['samples'] >= minutes and stamp - state['start'] >= (minutes - 1) * 60
            and (reported is None or stamp - float(reported) >= repeat)):
        state['reported_at'] = stamp
        return state, {'start': state['start'], 'last': stamp, 'samples': state['samples'],
                       'min_tps': state['min_tps'], 'avg_mspt': state['mspt_sum'] / state['samples'],
                       'threshold': threshold, 'continuing': reported is not None}
    return state, None


def sustained_step(state, row, threshold=14.0, minutes=20, repeat=3600, max_gap=150):
    """Average TPS of the last `minutes` of samples below threshold; (state, alert or None).
    10-28 23:21-01:15 TPS stayed between 7.6 and 14.3 for 114 minutes but never under 10 for five
    samples in a row, so lag_step stayed quiet while players asked in the group why it was so slow.
    state['window'] holds [timestamp, tps, mspt]; a failed reading or a gap starts it over."""
    state = dict(state or {})
    stamp, tps, mspt = _num(row.get('timestamp')), _num(row.get('tps')), _num(row.get('mspt'))
    if stamp is None or tps is None or row.get('tick_parser', 2) != 2:
        state.update(window=[], reported_at=None)
        return state, None
    window = [item for item in state.get('window') or [] if isinstance(item, list) and len(item) == 3]
    if window and stamp - float(window[-1][0]) > max_gap:
        window = []
    window.append([stamp, tps, mspt or 0.0])
    window = [item for item in window if stamp - float(item[0]) <= minutes * 60]
    state['window'] = window
    if len(window) < minutes - 2 or stamp - float(window[0][0]) < (minutes - 1) * 60 - 30:
        return state, None
    average = sum(item[1] for item in window) / len(window)
    if average >= threshold:
        state['reported_at'] = None
        return state, None
    reported = state.get('reported_at')
    if reported is not None and stamp - float(reported) < repeat:
        return state, None
    state['reported_at'] = stamp
    return state, {'kind': 'sustained', 'start': window[0][0], 'last': stamp, 'samples': len(window),
                   'avg_tps': average, 'min_tps': min(item[1] for item in window),
                   'avg_mspt': sum(item[2] for item in window) / len(window), 'threshold': threshold,
                   'minutes': minutes, 'continuing': reported is not None}


def dimension_ticks(text):
    """[(dimension, tps, ms_per_tick)] from `neoforge tps`, slowest first, without Overall."""
    rows = []
    for line in re.sub(r'§.', '', str(text or '')).splitlines():
        match = re.match(r'\s*(.+?):\s*([\d.]+)\s*TPS\s*\(([\d.]+)\s*ms/tick\)', line, re.I)
        if match and not re.match(r'overall', match.group(1), re.I):
            rows.append((match.group(1).strip(), float(match.group(2)), float(match.group(3))))
    return sorted(rows, key=lambda row: -row[2])


def format_lag_alert(prefix, streak, ticks_text='', list_text='', extra_lines=()):
    minutes = max(1, round((streak['last'] - streak['start']) / 60) + 1)
    if streak.get('kind') == 'sustained':
        lines = [f'{prefix}【卡顿提醒】最近 {streak["minutes"]} 分钟平均 TPS {streak["avg_tps"]:.1f}，'
                 f'低于 {streak["threshold"]:g}' + ('，仍在持续' if streak.get('continuing') else '')
                 + f'（最低 {streak["min_tps"]:.1f}，平均 MSPT {streak["avg_mspt"]:.0f}）']
    else:
        lines = [f'{prefix}【卡顿提醒】TPS 已连续约 {minutes} 分钟低于 {streak["threshold"]:g}'
                 + ('，仍在持续' if streak.get('continuing') else '')
                 + f'（{_clock(streak["start"])} 起，最低 {streak["min_tps"]:.1f}，平均 MSPT {streak["avg_mspt"]:.0f}）']
    dims = [row for row in dimension_ticks(ticks_text) if row[2] >= 1][:3]
    if dims:
        lines.append('最耗时的维度：' + '、'.join(f'{name} {ms:.0f} ms/tick' for name, _tps, ms in dims))
    match = re.search(r'There are (\d+) of a max of (\d+) players online:?\s*(.*)', str(list_text or ''))
    if match:
        names = [n.strip() for n in match.group(3).split(',') if n.strip()]
        shown = '、'.join(names[:12]) + (f' 等 {len(names)} 人' if len(names) > 12 else '')
        lines.append(f'在线 {match.group(1)} 人' + (f'：{shown}' if names else ''))
    lines.extend(extra_lines)
    lines.append('一小时内不再重复提醒。')
    return '\n'.join(lines)


# ===== mod updates =====

_FILE_VERSION = re.compile(r'[-_ ]v?\d[\w.+-]*$')


def _mod_rows(inventory):
    """{key: (label, version, file)}; filename-only entries are keyed without their version."""
    rows = {}
    for mod in (inventory or {}).get('mods') or []:
        if not isinstance(mod, dict):
            continue
        file = str(mod.get('file') or '')
        if mod.get('metadata') == 'filename' or not mod.get('id'):
            stem = re.sub(r'\.jar$', '', file or str(mod.get('name') or ''), flags=re.I)
            base = _FILE_VERSION.sub('', stem) or stem
            key, label, version = 'file:' + base.casefold(), base, stem[len(base):].lstrip('-_ v')
        else:
            key = str(mod['id']).casefold()
            label, version = str(mod.get('name') or mod['id']), str(mod.get('version') or '')
        if key in rows:  # two jars for one mod: keep both versions visible
            old = rows[key]
            rows[key] = (old[0], old[1] + '、' + version, old[2] + '、' + file)
        else:
            rows[key] = (label, version, file)
    return rows


def mod_changes(old_inventory, new_inventory, fetched=()):
    """{'updated': [(label, old, new)], 'added': [(label, version)], 'removed': [...]}.

    `fetched` lists jar names the mirror just downloaded: a jar replaced under the
    same version counts as updated with old == new.
    """
    old, new = _mod_rows(old_inventory), _mod_rows(new_inventory)
    fetched = {str(name) for name in fetched or ()}
    updated, added, removed = [], [], []
    for key, (label, version, file) in sorted(new.items(), key=lambda item: item[1][0].casefold()):
        if key not in old:
            added.append((label, version))
        elif old[key][1] != version:
            updated.append((label, old[key][1], version))
        elif any(part in fetched for part in file.split('、')) and old[key][2] == file:
            updated.append((label, version, version))
    for key, (label, version, _file) in sorted(old.items(), key=lambda item: item[1][0].casefold()):
        if key not in new:
            removed.append((label, version))
    return {'updated': updated, 'added': added, 'removed': removed}


def format_update(prefix, changes, found_at, summary='', limit=40):
    counts = {k: len(v) for k, v in changes.items()}
    lines = [f'{prefix}【模组更新】{_clock(found_at)} 发现服务器模组有变动：'
             f'更新 {counts["updated"]} 个，新增 {counts["added"]} 个，移除 {counts["removed"]} 个']
    if summary:
        lines.append(summary)
    shown = 0

    def section(title, rows, render):
        nonlocal shown
        if not rows:
            return
        lines.append(title)
        for row in rows:
            if shown >= limit:
                break
            lines.append('- ' + render(row))
            shown += 1

    section('更新：', changes['updated'],
            lambda r: f'{r[0]}：{r[1] or "?"} → {r[2] or "?"}' if r[1] != r[2] else f'{r[0]} {r[1]}（版本号未变，文件已替换）')
    section('新增：', changes['added'], lambda r: f'{r[0]} {r[1]}'.rstrip())
    section('移除：', changes['removed'], lambda r: f'{r[0]} {r[1]}'.rstrip())
    total = sum(counts.values())
    if total > shown:
        lines.append(f'……另有 {total - shown} 项，!mods 可看完整清单')
    return '\n'.join(lines)


# ===== player statistics: daily snapshots and weekly deltas =====

WEEK_FIELDS = ('play_hours', 'deaths', 'mob_kills', 'player_kills', 'mined', 'crafted',
               'distance_km', 'jumps', 'sessions')
_UNNAMED = re.compile(r'^[0-9a-f]{8}$')   # uuid prefix: the player is not in usercache


def snapshot_player_stats(stats, history_dir, now=None, keep_days=60, hour=20):
    """Keep one compact copy per day, taken at the first sync after `hour`:00 Beijing time.

    The weekly report goes out at 20:00, so a snapshot from the same hour a week
    earlier is the natural baseline. A file is named after the day its slot began
    (a 00:30 sync belongs to the previous day's slot). Returns the path written or None.
    """
    now = time.time() if now is None else now
    folder = Path(history_dir)
    folder.mkdir(parents=True, exist_ok=True)
    day = (datetime.fromtimestamp(now, TZ) - timedelta(hours=hour)).strftime('%Y-%m-%d')
    path = folder / f'{day}.json'
    written = None
    if not path.exists():
        compact = {str(row['name']): {field: row[field] for field in WEEK_FIELDS if _num(row.get(field)) is not None}
                   for row in (stats or {}).get('players') or [] if isinstance(row, dict) and row.get('name')}
        temp = path.with_name(path.name + '.tmp')
        temp.write_text(json.dumps({'taken_at': now, 'players': compact}, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, path)
        written = path
    cutoff = (datetime.fromtimestamp(now, TZ) - timedelta(days=keep_days)).strftime('%Y-%m-%d')
    for old in folder.glob('????-??-??.json'):
        if old.stem < cutoff:
            old.unlink(missing_ok=True)
    return written


def _read(path):
    try:
        with open(path, encoding='utf-8') as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def player_week(ops_root, since, now, min_hours=12):
    """Deltas from the last snapshot taken by the window start (or the oldest one) to now.

    None when there are no current stats, no snapshot, or the baseline is younger
    than min_hours (too short to say anything about a week).
    """
    root = Path(ops_root)
    current = _read(root / 'player-stats.json') or {}
    rows = {str(r['name']): r for r in current.get('players') or [] if isinstance(r, dict) and r.get('name')}
    snapshots = []
    for path in sorted((root / 'player-stats-history').glob('????-??-??.json')):
        data = _read(path) or {}
        if _num(data.get('taken_at')) is not None and isinstance(data.get('players'), dict):
            snapshots.append(data)
    if not rows or not snapshots:
        return None
    snapshots.sort(key=lambda s: s['taken_at'])
    # An hour of slack: the 20:00 report and the 20:00 snapshot race each other.
    before = [s for s in snapshots if s['taken_at'] <= since + 3600]
    chosen = before[-1] if before else snapshots[0]
    if now - chosen['taken_at'] < min_hours * 3600:
        return None
    base = chosen['players']
    deltas, new = {}, []
    for name, row in rows.items():
        if _UNNAMED.match(name):
            continue
        old = base.get(name)
        delta = {f: (_num(row.get(f)) or 0) - (_num((old or {}).get(f)) or 0) for f in WEEK_FIELDS}
        if delta['play_hours'] > 0.05:
            deltas[name] = delta
            if old is None:
                new.append(name)

    def top(field, count=1):
        ranked = sorted(((n, d[field]) for n, d in deltas.items() if d[field] > 0), key=lambda r: -r[1])
        return ranked[:count]

    return {'from': _clock(chosen['taken_at']), 'partial': chosen['taken_at'] > since + 3600,
            'active': len(deltas),
            'new': sorted(new), 'hours': top('play_hours', 5), 'deaths': top('deaths'),
            'mined': top('mined'), 'distance': top('distance_km'), 'kills': top('mob_kills')}


def _big(value):
    return f'{value / 10000:.1f} 万' if value >= 10000 else f'{value:g}'


def format_week(week):
    """Lines for the weekly report; [] when there is nothing to say."""
    if not week or not week['active']:
        return []
    head = f'玩家 本周上线 {week["active"]} 人'
    if week['new']:
        head += '｜新面孔 ' + '、'.join(week['new'][:6]) + (f' 等 {len(week["new"])} 人' if len(week['new']) > 6 else '')
    lines = [head]
    if week['hours']:
        lines.append('时长榜 ' + '、'.join(f'{n} {h:.1f}h' for n, h in week['hours']))
    extras = []
    if week['deaths']:
        extras.append(f'死得最多 {week["deaths"][0][0]} {week["deaths"][0][1]:g} 次')
    if week['kills']:
        extras.append(f'打怪最多 {week["kills"][0][0]} {_big(week["kills"][0][1])}只')
    if week['mined']:
        extras.append(f'挖得最多 {week["mined"][0][0]} {_big(week["mined"][0][1])}块')
    if week['distance']:
        extras.append(f'跑得最远 {week["distance"][0][0]} {week["distance"][0][1]:.0f} km')
    if extras:
        lines.append('｜'.join(extras))
    if week['partial']:
        lines.append(f'（玩家数据从 {week["from"]} 开始记录，不满 7 天）')
    return lines
