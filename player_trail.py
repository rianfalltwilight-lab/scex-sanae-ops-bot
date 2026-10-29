"""Where online players were over the last day, for admins only (round 30, 10-27).

Owner decision 10-27: sample every 2 minutes, keep 24 hours, answer admins only. PvP is on, so a
position trail would help raiders; the tool that reads it refuses everyone else, and the samples
stay on the bot host (<ops root>/player-trail.jsonl, one JSON line per sample).

A sample is the online list plus `data get entity <name> Dimension` / `Pos` for each player
(world_status.player_locations). Two minutes apart, the trail shows where someone was, not the
path between: a jump of thousands of blocks is a teleport, a nether trip or an elytra flight.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
SAMPLE_SECONDS = 120
KEEP_SECONDS = 24 * 3600
PRUNE_EVERY = 1800
FILE_NAME = 'player-trail.jsonl'
DEFAULT_MINUTES = 60
MAX_MINUTES = 24 * 60
DEFAULT_JUMP = 48
# Two samples further apart than this are separate sessions: no distance is counted between them.
GAP_SECONDS = SAMPLE_SECONDS * 2.5
TELEPORT_SPEED = 60            # blocks per second; faster than an elytra with rockets
MAX_POINTS = 30


def _stamp(value, with_date=False):
    return datetime.fromtimestamp(value, TZ).strftime('%m-%d %H:%M' if with_date else '%H:%M')


def _dim(dim):
    try:
        import world_status
        return world_status.dim_label(dim)
    except Exception:
        return str(dim)


class TrailRecorder:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        self._pruned = 0.0

    def record(self, located):
        """located: (online names, [(name, dim, x, y, z)]) from world_status.player_locations."""
        names, rows = located
        now = self.clock()
        line = {'t': round(now), 'online': [str(name) for name in names],
                'pos': [[str(name), str(dim), int(x), int(y), int(z)] for name, dim, x, y, z in rows]}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(line, ensure_ascii=False, separators=(',', ':')) + '\n')
            if now - self._pruned >= PRUNE_EVERY:
                self._prune(now)
        return line

    def _prune(self, now):
        self._pruned = now
        keep = [line for line in _lines(self.path) if line.get('t', 0) >= now - KEEP_SECONDS]
        temp = self.path.with_name(self.path.name + '.tmp')
        with temp.open('w', encoding='utf-8') as stream:
            for line in keep:
                stream.write(json.dumps(line, ensure_ascii=False, separators=(',', ':')) + '\n')
        os.replace(temp, self.path)


def _lines(path):
    try:
        with Path(path).open(encoding='utf-8') as stream:
            for raw in stream:
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(line, dict) and isinstance(line.get('t'), (int, float)):
                    yield line
    except OSError:
        return


def sampler_loop(server, query, path, *, sleep=time.sleep, log=print, stop=None):
    """Background thread body: one sample every SAMPLE_SECONDS; failures are logged at most every 30 min."""
    import world_status
    recorder = TrailRecorder(path)
    last_error = 0.0
    log(f'[trail] sampling {server.get("id")} every {SAMPLE_SECONDS}s, keep {KEEP_SECONDS // 3600}h -> {path}', flush=True)
    while not (stop and stop.is_set()):
        try:
            recorder.record(world_status.player_locations(query, query('list')))
        except Exception as exc:
            if time.time() - last_error >= 1800:
                last_error = time.time()
                log(f'[trail] sample failed: {type(exc).__name__}', flush=True)
        sleep(SAMPLE_SECONDS)


# ===== report =====

def _tracks(path, since):
    tracks, samples = {}, 0
    for line in _lines(path):
        if line['t'] < since:
            continue
        samples += 1
        for row in line.get('pos') or []:
            if isinstance(row, list) and len(row) == 5:
                name, dim, x, y, z = row
                tracks.setdefault(str(name), []).append((line['t'], str(dim), int(x), int(y), int(z)))
    return tracks, samples


def _distance(a, b):
    return math.sqrt((a[2] - b[2]) ** 2 + (a[3] - b[3]) ** 2 + (a[4] - b[4]) ** 2)


def _summary(points):
    """Path length, the biggest jump between neighbouring samples, dimension changes."""
    walked, jump, changes = 0.0, None, []
    for before, after in zip(points, points[1:]):
        if after[0] - before[0] > GAP_SECONDS:
            continue
        if after[1] != before[1]:
            changes.append((after[0], before[1], after[1]))
            continue
        step = _distance(before, after)
        walked += step
        if not jump or step > jump[0]:
            jump = (step, before, after)
    return walked, jump, changes


def _jump_text(jump):
    if not jump or jump[0] < 1:
        return '没怎么动'
    step, before, after = jump
    speed = step / max(1, after[0] - before[0])
    note = '，像是传送' if speed >= TELEPORT_SPEED else ''
    return (f'最大一跳 {round(step)} 格（{_stamp(before[0])}→{_stamp(after[0])}，'
            f'({before[2]}, {before[3]}, {before[4]})→({after[2]}, {after[3]}, {after[4]})，约 {speed:.0f} 格/秒{note}）')


def _player_lines(name, points, min_jump):
    walked, jump, changes = _summary(points)
    dims = list(dict.fromkeys(_dim(point[1]) for point in points))
    lines = [f'{name}：{_stamp(points[0][0])}–{_stamp(points[-1][0])} 记到 {len(points)} 次，'
             f'去过 {"、".join(dims)}，相邻两次之间累计约 {round(walked)} 格；{_jump_text(jump)}。']
    if changes:
        lines.append('换维度：' + '；'.join(f'{_stamp(t)} {_dim(a)}→{_dim(b)}' for t, a, b in changes[:10]))
    kept = [points[0]]
    for point in points[1:]:
        if point[1] != kept[-1][1] or _distance(point, kept[-1]) >= min_jump:
            kept.append(point)
    if kept[-1] is not points[-1]:
        kept.append(points[-1])
    if len(kept) > MAX_POINTS:
        step = len(kept) / MAX_POINTS
        kept = [kept[int(i * step)] for i in range(MAX_POINTS - 1)] + [kept[-1]]
    lines.append(f'位置（移动超过 {min_jump} 格或换维度才列出）：')
    lines += [f'  {_stamp(t)} {_dim(dim)} {x} {y} {z}' for t, dim, x, y, z in kept]
    return lines


def _near(tracks, x, z, radius, dim=''):
    rows = []
    for name, points in tracks.items():
        close = [point for point in points if (not dim or point[1] == dim)
                 and math.hypot(point[2] - x, point[4] - z) <= radius]
        if close:
            nearest = min(close, key=lambda point: math.hypot(point[2] - x, point[4] - z))
            rows.append((close[0][0], name, close, nearest))
    rows.sort()
    return rows


def report(prefix, path, player='', minutes=None, min_jump=None, near=None, radius=None, now=None):
    now = time.time() if now is None else now
    try:
        minutes = max(10, min(MAX_MINUTES, int(minutes or DEFAULT_MINUTES)))
    except (TypeError, ValueError):
        minutes = DEFAULT_MINUTES
    try:
        min_jump = max(8, min(2000, int(min_jump or DEFAULT_JUMP)))
    except (TypeError, ValueError):
        min_jump = DEFAULT_JUMP
    since = now - minutes * 60
    tracks, samples = _tracks(path, since)
    span = f'最近 {minutes // 60} 小时' if minutes % 60 == 0 else f'最近 {minutes} 分钟'
    head = (f'{prefix} 玩家轨迹（只给管理员；{span}，每 2 分钟记一次，最多留 24 小时；'
            '两次之间怎么走的看不到）')
    if not samples:
        return head + '\n这段时间还没有记录（机器人刚重启，或者采样连不上服务器）。'
    if near:
        try:
            parts = [float(value) for value in str(near).replace('，', ',').replace(',', ' ').split()]
            x, z = (parts[0], parts[-1]) if len(parts) in (2, 3) else (None, None)
        except ValueError:
            x = z = None
        if x is None:
            return head + '\nnear 要写坐标“x z”或“x y z”，例如 1386 267829。'
        try:
            radius = max(8, min(1000, int(radius or 64)))
        except (TypeError, ValueError):
            radius = 64
        rows = _near(tracks, x, z, radius)
        if not rows:
            return head + f'\n这段时间没人到过 ({x:.0f}, {z:.0f}) 附近 {radius} 格内（共 {samples} 次采样）。'
        lines = [head, f'到过 ({x:.0f}, {z:.0f}) 附近 {radius} 格内的人（按最早到达排序）：']
        for _first, name, close, nearest in rows[:15]:
            distance = math.hypot(nearest[2] - x, nearest[4] - z)
            lines.append(f'- {name}：{_stamp(close[0][0])}–{_stamp(close[-1][0])} 记到 {len(close)} 次，'
                         f'最近 {distance:.0f} 格（{_stamp(nearest[0])} {_dim(nearest[1])} {nearest[2]} {nearest[3]} {nearest[4]}）')
        return '\n'.join(lines)
    if player:
        wanted = str(player).strip().casefold()
        name = next((key for key in tracks if key.casefold() == wanted), None)
        if not name:
            similar = [key for key in tracks if wanted in key.casefold()]
            if len(similar) == 1:
                name = similar[0]
            else:
                return head + (f'\n这段时间没记到 {player}（不在线，或者名字不对）。'
                               + (f'名字相近的：{"、".join(similar[:6])}。' if similar else ''))
        return '\n'.join([head] + _player_lines(name, tracks[name], min_jump))
    lines = [head, f'共 {samples} 次采样，记到 {len(tracks)} 个玩家（走得多的在前）：']
    ordered = sorted(tracks.items(), key=lambda item: -_summary(item[1])[0])
    for name, points in ordered[:20]:
        walked, jump, changes = _summary(points)
        last = points[-1]
        lines.append(f'- {name}：{_stamp(points[0][0])}–{_stamp(last[0])} 记到 {len(points)} 次，约 {round(walked)} 格，'
                     f'{_jump_text(jump)}' + (f'，换了 {len(changes)} 次维度' if changes else '')
                     + f'；最后在 {_dim(last[1])} {last[2]} {last[3]} {last[4]}')
    lines.append('要看某个人的详细位置，填 player；要查谁到过某个地方，填 near。')
    return '\n'.join(lines)
