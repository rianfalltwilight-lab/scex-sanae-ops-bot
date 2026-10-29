"""Read-only status for an explicitly configured external world-backup pipeline.

The adapter reads <tool>/config.json, last-task.json and last-task.stdout.json or
last-task.stderr.json, then the configured output directory's state.json and
world_*.tar.gz metadata. The pipeline format includes interval, retention and disk
budget fields. Nothing here starts, deletes or changes a backup.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
TOOL_DIR = Path(os.environ.get('LEGACY_BACKUP_TOOL_DIR') or Path(__file__).parent / 'backup-tool')
GIB = 2 ** 30
STATUS_TEXT = {'NOT_DUE': '还没到下一次', 'NO_PLAYERS': '没人在线，按规则跳过', 'BUSY': '正在备份',
               'DISABLED': '自动备份已停用', 'COMPLETE': '成功', 'FAILED': '失败'}


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def read(tool_dir=None, now=None):
    """Facts only; {'available': False, 'reason': ...} when the tool is not there."""
    tool_dir = Path(tool_dir or TOOL_DIR)
    config = _json(tool_dir / 'config.json')
    if not isinstance(config, dict) or not config.get('output_directory'):
        return {'available': False, 'reason': f'没找到备份工具的配置（{tool_dir}）'}
    output = Path(config['output_directory'])
    state = _json(output / 'state.json') or {}
    task = _json(tool_dir / 'last-task.json') or {}
    result = None
    for name in ('last-task.stderr.json', 'last-task.stdout.json'):
        data = _json(tool_dir / name)
        if isinstance(data, dict) and data.get('status'):
            result = data
            break
    archives = []
    for path in output.glob('world_*.tar.gz'):
        try:
            stat = path.stat()
        except OSError:
            continue
        archives.append({'name': path.name, 'bytes': stat.st_size, 'mtime': stat.st_mtime})
    archives.sort(key=lambda row: row['mtime'], reverse=True)
    try:
        free = shutil.disk_usage(output).free
    except OSError:
        free = None
    last_bytes = int(state.get('last_archive_bytes') or 0)
    # The tool's own space rule (backup.py require_space / archive_estimate_bytes).
    expected = max(max(32, int(config.get('expected_archive_GiB') or 32)), math.ceil(last_bytes * 1.2 / GIB)) * GIB
    return {
        'available': True, 'now': time.time() if now is None else now,
        'enabled': bool(config.get('enabled')), 'interval_minutes': int(config.get('interval_minutes') or 120),
        'keep': config.get('keep_complete_backups'), 'output': str(output), 'drive': output.drive or str(output),
        'last_success': _epoch(state.get('last_success_epoch')),
        'last_complete': Path(str(state.get('last_complete') or '')).name, 'last_bytes': last_bytes,
        'task_finished': _epoch(task.get('finishedAt')), 'task_exit': task.get('exitCode'),
        'task_status': str((result or {}).get('status') or ''),
        'task_message': str((result or {}).get('message') or (result or {}).get('error') or ''),
        'archives': archives, 'free': free, 'required': 20 * GIB + 2 * expected,
    }


def failing(data):
    """The latest scheduled run failed (NOT_DUE / NO_PLAYERS are not failures)."""
    return bool(data.get('available') and data.get('enabled')
                and (data.get('task_status') == 'FAILED' or (data.get('task_exit') not in (None, 0, 3)
                                                              and data.get('task_status') not in ('NOT_DUE', 'NO_PLAYERS', 'BUSY'))))


def stale_hours(data):
    last = data.get('last_success')
    return None if last is None else max(0.0, (data['now'] - last) / 3600)


def reason(data):
    """The failure in plain words."""
    message = data.get('task_message') or ''
    match = re.search(r'Insufficient target disk space:\s*(\d+)\s*free,\s*(\d+)\s*required', message)
    if match:
        return (f'备份盘（{data.get("drive")}）空间不够：剩 {int(match.group(1)) / GIB:.1f} GiB，'
                f'备份工具要求至少 {int(match.group(2)) / GIB:.1f} GiB（20 GiB 余量 + 2 份归档）')
    if re.search(r'Another Legacy backup is running', message):
        return '上一次备份还在进行'
    if re.search(r'RCON|Connection|timed out|SSH', message, re.I):
        return '连不上服务器（' + message[:80] + '）'
    return message[:160] or '原因没记录'


def _clock(stamp, now):
    moment = datetime.fromtimestamp(stamp, TZ)
    today = datetime.fromtimestamp(now, TZ).date()
    return moment.strftime('%H:%M') if moment.date() == today else moment.strftime('%m-%d %H:%M')


def _age(hours):
    if hours < 1:
        return f'{max(1, round(hours * 60))} 分钟'
    if hours < 48:
        return f'{hours:.0f} 小时'
    return f'{int(hours // 24)} 天 {round(hours % 24)} 小时'


def format_status(prefix, data, detailed=False):
    if not data.get('available'):
        return f'{prefix} 备份情况暂时查不到：{data.get("reason")}'
    now = data['now']
    if not data.get('enabled'):
        return f'{prefix} 世界自动备份现在是停用的。'
    lines = []
    hours = stale_hours(data)
    if hours is None:
        lines.append(f'{prefix} 世界备份：还没有成功过的记录。')
    else:
        lines.append(f'{prefix} 世界备份：上次成功是 {_clock(data["last_success"], now)}（{_age(hours)}前）'
                     + (f'，{data["last_bytes"] / GIB:.1f} GiB，完整校验通过' if data.get('last_bytes') else '')
                     + f'；正常每 {data["interval_minutes"]} 分钟一份（没人在线时跳过）。')
    if failing(data):
        when = f'（{_clock(data["task_finished"], now)}）' if data.get('task_finished') else ''
        lines.append(f'之后的备份没做成：最近一次{when}失败。' + (f'原因：{reason(data)}。' if detailed else ''))
    elif data.get('task_status'):
        when = f'（{_clock(data["task_finished"], now)}）' if data.get('task_finished') else ''
        lines.append(f'最近一次检查{when}：{STATUS_TEXT.get(data["task_status"], data["task_status"])}。')
    if detailed:
        kept = data.get('archives') or []
        if kept:
            lines.append(f'{data["output"]} 里保留 {len(kept)} 份：' + '、'.join(
                f'{_clock(row["mtime"], now)} {row["bytes"] / GIB:.1f} GiB' for row in kept[:4])
                + (f'（设定保留 {data["keep"]} 份）' if data.get('keep') else ''))
        if data.get('free') is not None:
            lines.append(f'{data["drive"]} 现在剩 {data["free"] / GIB:.1f} GiB，下一份需要至少 {data["required"] / GIB:.1f} GiB。')
    return '\n'.join(lines)


def format_alert(prefix, data):
    hours = stale_hours(data)
    head = (f'{prefix}【备份提醒】世界已经 {_age(hours)}没有成功备份了' if hours is not None
            else f'{prefix}【备份提醒】世界还没有成功备份过')
    lines = [head + f'（上次成功 {_clock(data["last_success"], data["now"])}）。' if hours is not None else head + '。',
             f'最近一次自动备份失败：{reason(data)}。']
    # The space line repeats the reason when space is the reason (first alert, 10-29 11:33).
    if data.get('free') is not None and 'Insufficient target disk space' not in (data.get('task_message') or ''):
        lines.append(f'{data["drive"]} 剩 {data["free"] / GIB:.1f} GiB，要求 {data["required"] / GIB:.1f} GiB。')
    lines.append('12 小时内不再重复提醒；恢复后会说一声。')
    return '\n'.join(lines)


def format_recovered(prefix, data):
    return (f'{prefix}【备份恢复】世界备份又成功了：{_clock(data["last_success"], data["now"])}，'
            f'{data.get("last_bytes", 0) / GIB:.1f} GiB。')
