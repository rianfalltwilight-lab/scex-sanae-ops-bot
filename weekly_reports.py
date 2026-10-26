"""Deterministic seven-day reports and durable, at-most-once weekly delivery.

All times use UTC+8. Collection reads existing artifacts/logs only; it never
queries Minecraft, creates backups, or invokes a model. Delivery belongs to the
existing monitor, with no additional OS task or service.
"""
from __future__ import annotations

import gzip
import json
import logging
import math
import os
import re
import shutil
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import server_watch

TZ = timezone(timedelta(hours=8))
WEEK = 7 * 86400
MAX_STREAM = 64 * 1024 * 1024
MAX_LOG_BYTES = 128 * 1024 * 1024
_THREAD_LOCK = threading.RLock()
HEADER = re.compile(r'^\[([^\]]+)\] \[([^\]]+)/(INFO|WARN|ERROR|FATAL)\] \[([^\]]+)\]: (.*)$')
MC = {'net.minecraft.server.MinecraftServer/', 'net.minecraft.server.dedicated.DedicatedServer/'}
BACKUP = 'de.melanx.simplebackups.SimpleBackups/'
LOG_KEYS = ('start', 'stop', 'join', 'leave', 'backup_start', 'backup_complete',
            'backup_error', 'error_lines')
GAP_SECONDS = 1800


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))


@contextmanager
def locked(path):
    """Cross-process byte lock; POSIX lockf avoids the local fcntl shim."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK, path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            os.lockf(handle.fileno(), os.F_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                os.lockf(handle.fileno(), os.F_ULOCK, 1)


def number(value, low=0, high=float('inf')):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and low <= value <= high else None
    except (TypeError, ValueError):
        return None


def stream_rows(path, since, now):
    """No row-count cap. Explicitly flag byte truncation and malformed rows."""
    rows, meta = [], {'available': False, 'truncated': False, 'malformed': 0}
    try:
        size = path.stat().st_size
        start = max(0, size - MAX_STREAM)
        meta.update(available=True, truncated=bool(start), bytes=size - start)
        with path.open('rb') as stream:
            stream.seek(start)
            if start:
                stream.readline()
            end = size
            while stream.tell() < end:
                line = stream.readline(end - stream.tell())
                try:
                    row = json.loads(line.decode('utf-8-sig'))
                    stamp = number(row.get('timestamp', row.get('occurred_at', row.get('time'))))
                    if stamp is None:
                        raise ValueError('timestamp')
                    if since <= stamp <= now:
                        rows.append(dict(row, timestamp=stamp))
                except (ValueError, TypeError, AttributeError, UnicodeError):
                    meta['malformed'] += 1
    except OSError:
        meta['available'] = False
    meta['rows'] = len(rows)
    return rows, meta


def log_stamp(value, fallback_date):
    cn = re.fullmatch(r'(\d{2})(\d{1,2})月(\d{4}) (\d{2}:\d{2}:\d{2}(?:\.\d+)?)', value)
    try:
        if cn:
            value = f'{cn[3]}-{int(cn[2]):02d}-{cn[1]} {cn[4]}'
        elif re.fullmatch(r'\d{2}:\d{2}:\d{2}(?:\.\d+)?', value):
            value = fallback_date + ' ' + value
        return datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp()
    except ValueError:
        return None


def log_kind(thread, level, logger, body):
    # Anchor to real log headers and exact bodies; player chat cannot spoof events.
    if thread == 'Server thread' and logger in MC:
        if re.fullmatch(r'Done \([0-9.]+s\)! For help, type "help"', body):
            return 'start'
        if body == 'Stopping server':
            return 'stop'
        if re.fullmatch(r'\S+ joined the game', body):
            return 'join'
        if re.fullmatch(r'\S+ left the game', body):
            return 'leave'
    if thread == 'SimpleBackups' and logger == BACKUP:
        if body == 'Backup started...':
            return 'backup_start'
        if body.startswith('Backup completed in '):
            return 'backup_complete'
        if level in ('ERROR', 'FATAL'):
            return 'backup_error'
    return None


def scan_logs(server_dir, root, since, now):
    directory = server_dir / 'logs'
    first_date = (datetime.fromtimestamp(since, TZ) - timedelta(days=1)).date()
    last_date = datetime.fromtimestamp(now, TZ).date()
    cache = read_json(root / 'weekly-log-cache.json', {}) or {}
    if cache.get('schema') != 1:
        cache = {}
    previous = cache.get('files', {})
    current, seen, counts, days = {}, set(), Counter(), set()
    meta = {'available': directory.is_dir(), 'files': 0, 'cached_files': 0,
            'issues': [], 'first': None, 'last': None, 'scanned_bytes': 0}
    paths = []
    if directory.is_dir():
        for path in directory.iterdir():
            if path.name == 'latest.log':
                paths.append(path)
            elif re.fullmatch(r'\d{4}-\d{2}-\d{2}-\d+\.log(?:\.gz)?', path.name):
                if first_date.isoformat() <= path.name[:10] <= last_date.isoformat():
                    paths.append(path)
    if len(paths) > 256:
        meta['issues'].append('日志文件超过 256 个，本次只读取最近文件')
        paths = sorted(paths, key=lambda p: p.name, reverse=True)[:256]
    for path in sorted(paths):
        try:
            stat = path.stat()
            signature = [stat.st_size, stat.st_mtime_ns]
            entry = previous.get(path.name, {})
            if path.name != 'latest.log' and entry.get('signature') == signature and not entry.get('partial'):
                meta['cached_files'] += 1
            else:
                entry = {'signature': signature, 'events': [], 'first': None, 'last': None,
                         'days': [], 'errors': [], 'partial': False}
                opener = gzip.open if path.suffix == '.gz' else open
                file_days = set()
                fallback = path.name[:10] if path.name != 'latest.log' else datetime.fromtimestamp(stat.st_mtime, TZ).date().isoformat()
                with opener(path, 'rb') as stream:
                    while True:
                        raw = stream.readline(1024 * 1024)
                        if not raw:
                            break
                        meta['scanned_bytes'] += len(raw)
                        if meta['scanned_bytes'] > MAX_LOG_BYTES or len(raw) == 1024 * 1024:
                            entry['partial'] = True
                            break
                        line = raw.decode('utf-8', 'replace').rstrip('\r\n')
                        match = HEADER.fullmatch(line)
                        if not match:
                            continue
                        stamp = log_stamp(match[1], fallback)
                        if stamp is None:
                            continue
                        entry['first'] = min(entry['first'] or stamp, stamp)
                        entry['last'] = max(entry['last'] or stamp, stamp)
                        file_days.add(datetime.fromtimestamp(stamp, TZ).date().isoformat())
                        kind = log_kind(*match.groups()[1:])
                        if kind or match[3] in ('ERROR', 'FATAL'):
                            # Store only event hashes and times, never raw chat or stack traces.
                            import hashlib
                            entry['events'].append([stamp, kind, match[3], hashlib.sha256(raw.rstrip()).hexdigest()])
                entry['days'] = sorted(file_days)
            if entry.get('partial'):
                meta['issues'].append(path.name + ': 读取达到大小上限')
            if entry.get('first') is None:
                meta['issues'].append(path.name + ': 没有可识别的带日期记录')
            current[path.name] = entry
            meta['files'] += 1
            occurrences = Counter()
            for stamp, kind, level, digest in entry['events']:
                occurrences[(stamp, digest)] += 1
                identity = (stamp, digest, occurrences[(stamp, digest)])
                if since <= stamp <= now and identity not in seen:
                    seen.add(identity)
                    if kind:
                        counts[kind] += 1
                    if level in ('ERROR', 'FATAL'):
                        counts['error_lines'] += 1
            days.update(day for day in entry['days'] if datetime.fromtimestamp(since, TZ).date().isoformat() <= day <= last_date.isoformat())
            if entry['first'] is not None and entry['last'] >= since and entry['first'] <= now:
                a, b = max(since, entry['first']), min(now, entry['last'])
                meta['first'] = min(meta['first'] or a, a)
                meta['last'] = max(meta['last'] or b, b)
        except (OSError, EOFError, ValueError) as exc:
            meta['issues'].append(path.name + ': ' + type(exc).__name__)
    expected = {(datetime.fromtimestamp(since, TZ).date() + timedelta(days=i)).isoformat()
                for i in range((last_date - datetime.fromtimestamp(since, TZ).date()).days + 1)}
    meta['missing_days'] = sorted(expected - days)
    meta['available'] = meta['available'] and meta['first'] is not None
    write_json(root / 'weekly-log-cache.json', {'schema': 1, 'files': current})
    return {key: counts[key] if meta['available'] else None for key in (
        'start', 'stop', 'join', 'leave', 'backup_start', 'backup_complete', 'backup_error', 'error_lines')}, meta


def remote_activity(events, available):
    """Join counts from recorded events; a remote server has no retained logs here."""
    logs = dict.fromkeys(LOG_KEYS)
    if available:
        joins = [row for row in events if row.get('type') == 'join']
        logs['join'] = len(joins)
        logs['join_players'] = len({str((row.get('details') or {}).get('player') or '').casefold()
                                    for row in joins} - {''})
    return logs


def sample_gaps(rows, since, now, threshold=GAP_SECONDS):
    """Spans longer than threshold without any sample, i.e. the collector was not running."""
    points = [since] + sorted(row['timestamp'] for row in rows) + [now]
    return [[a, b] for a, b in zip(points, points[1:]) if b - a > threshold]


def _clock(value):
    # build_report binds a local named stamp, so it cannot call stamp() itself.
    return datetime.fromtimestamp(value, TZ).strftime('%m-%d %H:%M')


def build_report(telemetry, now=None, output_root=None):
    now = float(time.time() if now is None else now)
    since, source = now - WEEK, telemetry.root
    root = Path(output_root or source)
    remote = bool(getattr(telemetry, 'remote', False))
    with locked(root / 'weekly-generation.lock'):
        perf, perf_meta = stream_rows(source / 'perf-samples.jsonl', since, now)
        events, event_meta = stream_rows(source / 'events.jsonl', since, now)
        fingerprints, fp_meta = stream_rows(source / 'error-fingerprints.jsonl', since, now)
        # Deduplicate identical samples, excluding unsupported parser history.
        perf = list({r['timestamp']: r for r in perf}.values())
        valid = [r for r in perf if r.get('tick_parser') == 2
                 and number(r.get('tps'), 0, 20.01) is not None
                 and number(r.get('mspt')) is not None]
        tps, mspt = [float(r['tps']) for r in valid], [float(r['mspt']) for r in valid]
        players = [number(r.get('players')) for r in perf if number(r.get('players')) is not None]
        avg = lambda values: sum(values) / len(values) if values else None
        counts = Counter(r.get('type') for r in events if isinstance(r.get('type'), str))
        if remote:
            logs = remote_activity(events, event_meta['available'])
            log_meta = {'available': False, 'remote': True, 'files': 0, 'cached_files': 0,
                        'issues': [], 'first': None, 'last': None, 'scanned_bytes': 0,
                        'missing_days': []}
        else:
            logs, log_meta = scan_logs(Path(telemetry.server_dir), root, since, now)
        crash_dir = Path(telemetry.server_dir) / 'crash-reports'
        crash_names = set()
        for row in events:
            if row.get('type') == 'crash':
                name = (row.get('details') or {}).get('report')
                if name:
                    crash_names.add(Path(name).name)
        if crash_dir.is_dir():
            for path in crash_dir.glob('*.txt'):
                match = re.search(r'crash-(\d{4}-\d{2}-\d{2})_(\d{2})\.(\d{2})\.(\d{2})', path.name)
                stamp = log_stamp(f'{match[1]} {match[2]}:{match[3]}:{match[4]}', '') if match else path.stat().st_mtime
                if stamp is not None and since <= stamp <= now:
                    crash_names.add(path.name)
        verifies = [r for r in events if r.get('type') == 'backup_verify'
                    and isinstance((r.get('details') or {}).get('ok'), bool)]
        backup = read_json(source / 'backup-verify.json', {}) or {}
        runtime = read_json(source / 'runtime.json', {}) or {}
        age = now - (number(runtime.get('timestamp')) or 0)
        runtime_fresh = 0 <= age <= 180 and runtime.get('tick_parser') == 2
        try:
            free = None if remote else shutil.disk_usage(telemetry.server_dir).free / 1024 ** 3
        except OSError:
            free = None
        gaps = sample_gaps(perf, since, now) if remote else []
        issues = []
        for label, meta in [('性能采样', perf_meta), ('事件记录', event_meta), ('指纹记录', fp_meta)]:
            if not meta['available']:
                issues.append(label + '缺失')
            if meta['truncated']:
                issues.append(label + '超过读取上限，统计不完整')
            if meta['malformed']:
                issues.append(label + f'有 {meta["malformed"]} 条损坏记录')
        issues.extend(log_meta['issues'])
        if log_meta['missing_days']:
            issues.append('部分日期没有保留日志：' + '、'.join(log_meta['missing_days']))
        if gaps:
            issues.append('采样中断（期间没有数据）：' + '、'.join(
                f'{_clock(a)}～{_clock(b)}' for a, b in gaps[:3]) +
                (f' 等 {len(gaps)} 段' if len(gaps) > 3 else ''))
        data = {'schema': 2, 'server': telemetry.server_id, 'prefix': telemetry.prefix,
                'generated_at': now, 'since': since, 'samples': len(valid),
                'total_samples': len(perf), 'excluded_samples': len(perf) - len(valid),
                'performance_first': min((r['timestamp'] for r in valid), default=None),
                'performance_last': max((r['timestamp'] for r in valid), default=None),
                'avg_tps': avg(tps), 'min_tps': min(tps) if tps else None,
                'avg_mspt': avg(mspt), 'max_mspt': max(mspt) if mspt else None,
                'lag_samples': sum(float(r['tps']) < 18 or float(r['mspt']) >= 50 for r in valid) if valid else None,
                'player_samples': len(players), 'avg_players': avg(players),
                'peak_players': max(players) if players else None,
                'events': dict(counts), 'logs': logs,
                # Remote crash reports arrive as events from the SSH crash watcher.
                'crash_reports': ((len(crash_names) if event_meta['available'] else None) if remote else
                                  (len(crash_names) if crash_dir.is_dir() or event_meta['available'] else None)),
                'backup_ok': None if remote else backup.get('ok'),
                'backup_checked_at': None if remote else backup.get('generated_at'),
                'backup_verify_pass': 0 if remote else sum(r['details']['ok'] for r in verifies),
                'backup_verify_batches': None if remote or not event_meta['available'] else len(verifies),
                'fingerprint_alert_records': sum(r.get('action') in ('first', 'alert', 'critical') for r in fingerprints) if fp_meta['available'] else None,
                'runtime': runtime if runtime_fresh else {}, 'runtime_age_seconds': age,
                'free_gib': free, 'sources': {'performance': perf_meta, 'events': event_meta, 'fingerprints': fp_meta, 'logs': log_meta},
                'issues': issues, 'remote': remote, 'sample_gaps': gaps,
                'definitions': REMOTE_DEFINITIONS if remote else ['TPS/MSPT 仅计 tick_parser=2 的有限有效值；排除旧解析数据。',
                    '平均值为采样平均；卡顿样本为 TPS<18 或 MSPT>=50，不等于独立卡顿事故。',
                    '启停、进退服、备份创建、ERROR/FATAL 从保留日志计数；启动为 Done，停止为 Stopping server。',
                    '备份完成与启动为窗口内各自计数，跨窗口任务不组成成功率；核验按已记录的批次计。',
                    '指纹告警记录只含 first/alert/critical；不含聚合摘要，不代表群消息投递数。',
                    '崩溃按报告文件名去重，文件与事件合并；不是进程异常退出次数。',
                    '所有时间为北京时间；缺失数据不填零，保留日志不保证覆盖服务离线时段。']}
        try:
            data['players_week'] = server_watch.player_week(source, since, now)
        except Exception:
            data['players_week'] = None
        day = datetime.fromtimestamp(now, TZ).strftime('%Y-%m-%d')
        folder = root / 'weekly' / day
        data['report_path'] = str(folder / 'report.txt')
        write_json(folder / 'report.json', data)
        atomic_text(folder / 'report.txt', format_report(data, details=True))
        write_json(root / 'weekly-latest.json', data)
        return data


REMOTE_DEFINITIONS = [
    'TPS/MSPT 仅计 tick_parser=2 的有限有效值；排除旧解析数据。',
    '平均值为采样平均；卡顿样本为 TPS<18 或 MSPT>=50，不等于独立卡顿事故。',
    '服务器运行在远端主机：在线与性能来自约每分钟一次的 RCON 采样，进服次数来自实时日志记录。',
    '崩溃按报告文件名去重（含 JVM 致命错误日志）；一次崩溃常产生两份报告，不等于异常退出次数。',
    '远端的备份和磁盘不在本机采集范围内，周报不统计。',
    '指纹告警记录只含 first/alert/critical；不含聚合摘要，不代表群消息投递数。',
    '所有时间为北京时间；缺失数据不填零，采样中断的时段不计入平均。']


def fmt(value):
    return '未记录' if value is None else f'{value:.2f}'.rstrip('0').rstrip('.')


def stamp(value):
    return datetime.fromtimestamp(value, TZ).strftime('%m-%d %H:%M') if value is not None else '未记录'


def format_remote_report(data, fresh, details=False):
    logs = data['logs']
    joins = ('未记录' if logs.get('join') is None else
             f'{logs["join"]}次（{logs.get("join_players") or 0}人）')
    lines = [f'{data["prefix"]}【周报】最近 7 天', fresh,
             f'性能 有效样本{data["samples"]} TPS均{fmt(data["avg_tps"])} / 低{fmt(data["min_tps"])}',
             f'MSPT均{fmt(data["avg_mspt"])} / 峰{fmt(data["max_mspt"])}｜卡顿样本{fmt(data["lag_samples"])}',
             f'在线 均{fmt(data["avg_players"])} / 峰{fmt(data["peak_players"])}｜进服{joins}',
             *server_watch.format_week(data.get('players_week')),
             f'可靠 崩溃报告{fmt(data["crash_reports"])}份｜错误 指纹告警记录{fmt(data["fingerprint_alert_records"])}（不含聚合摘要）',
             f'性能覆盖 {stamp(data["performance_first"])}～{stamp(data["performance_last"])}；排除旧版/无效采样{data["excluded_samples"]}条',
             '口径：卡顿按采样计；服务器在远端主机，备份和磁盘不在统计内。']
    if data['issues']:
        lines.append('部分数据缺失或读取不完整，详见 !周报 详情。')
    if details:
        lines.append(f'统计窗口 {stamp(data["since"])}～{stamp(data["generated_at"])}（北京时间）')
        lines.extend(data['definitions'])
        lines.extend('数据说明：' + issue for issue in data['issues'][:12])
    else:
        lines.append('详情 !周报 详情 / !体检；每周一 20:00 发玩家群。')
    return '\n'.join(lines)


def format_report(data, details=False):
    runtime, logs = data.get('runtime', {}), data['logs']
    fresh = (f'实时 在线{fmt(runtime.get("players"))} TPS{fmt(runtime.get("tps"))} MSPT{fmt(runtime.get("mspt"))}'
             f'（{max(0, int(data["runtime_age_seconds"]))}秒前）') if runtime else '实时 暂无新鲜采样'
    if data.get('remote'):
        return format_remote_report(data, fresh, details)
    lines = [f'{data["prefix"]}【周报】最近 7 天', fresh,
             f'性能 有效样本{data["samples"]} TPS均{fmt(data["avg_tps"])} / 低{fmt(data["min_tps"])}',
             f'MSPT均{fmt(data["avg_mspt"])} / 峰{fmt(data["max_mspt"])}｜卡顿样本{fmt(data["lag_samples"])}',
             f'在线 均{fmt(data["avg_players"])} / 峰{fmt(data["peak_players"])}',
             f'启停 启动成功{fmt(logs["start"])} / 停止记录{fmt(logs["stop"])}｜进{fmt(logs["join"])}退{fmt(logs["leave"])}',
             f'可靠 崩溃报告{fmt(data["crash_reports"])}｜备份完成{fmt(logs["backup_complete"])} / 启动{fmt(logs["backup_start"])}',
             f'备份核验 通过{fmt(data["backup_verify_pass"]) if data["backup_verify_batches"] else "未记录"} / {fmt(data["backup_verify_batches"])}批次',
             f'日志 ERROR/FATAL {fmt(logs["error_lines"])}｜指纹告警记录{fmt(data["fingerprint_alert_records"])}（不含聚合摘要）',
             f'磁盘 剩{fmt(data["free_gib"])} GiB',
             f'性能覆盖 {stamp(data["performance_first"])}～{stamp(data["performance_last"])}；排除旧版/无效采样{data["excluded_samples"]}条',
             '口径：卡顿按采样计，备份创建与核验分开统计。']
    if data['issues']:
        lines.append('部分数据缺失或读取不完整，详见 !周报 详情。')
    if details:
        lines.extend([f'统计窗口 {stamp(data["since"])}～{stamp(data["generated_at"])}（北京时间）',
                      f'保留日志 {stamp(data["sources"]["logs"]["first"])}～{stamp(data["sources"]["logs"]["last"])}',
                      f'最近备份核验 {stamp(data["backup_checked_at"])}：' + {True: '通过', False: '失败', None: '未验证'}.get(data['backup_ok'], '未验证')])
        lines.extend(data['definitions'])
        lines.extend('数据说明：' + issue for issue in data['issues'][:12])
    else:
        lines.append('详情 !周报 详情 / !体检；每周一 20:00 发玩家群。')
    return '\n'.join(lines)


def _combined(statuses):
    values = list(statuses)
    return values[0] if len(set(values)) == 1 else ','.join(values)


def publish_due(telemetry, push_fn, group_id, now=None, config=None):
    """One claimed attempt per scheduled week and group, including ambiguous receipts.

    group_id may be one group or several; the report is generated once and each
    group has its own ledger entry ("<due>#<group>"). A durable 'sending' claim
    survives crashes and is never replayed, because OneBot has no idempotency
    key. Failures are retained for operator inspection.
    """
    now = float(time.time() if now is None else now)
    groups = list(dict.fromkeys(int(g) for g in (
        group_id if isinstance(group_id, (list, tuple, set, frozenset)) else [group_id])))
    if config is None:
        config = read_json(Path(__file__).with_name('weekly-report-config.json'), {})
    if not config or not config.get('enabled') or config.get('server') != telemetry.server_id:
        return 'disabled'
    if config.get('timezone') != 'Asia/Shanghai' or config.get('destination') != 'players':
        raise ValueError('unsupported weekly destination/timezone')
    local = datetime.fromtimestamp(now, TZ)
    due = (local - timedelta(days=(local.weekday() - int(config['weekday'])) % 7)).replace(
        hour=int(config['hour']), minute=int(config['minute']), second=0, microsecond=0)
    if not 0 <= now - due.timestamp() < min(48, max(1, int(config.get('catch_up_hours', 24)))) * 3600:
        return 'not-due'
    key = due.isoformat()
    root = telemetry.root
    with locked(root / 'weekly-delivery.lock'):
        state_path = root / 'weekly-delivery.json'
        # Fail closed if an existing ledger cannot be read; never resend blindly.
        state = read_json(state_path, None) if state_path.exists() else {'deliveries': {}}
        if not isinstance(state, dict) or not isinstance(state.get('deliveries'), dict):
            raise ValueError('weekly delivery ledger unreadable')
        deliveries = state['deliveries']

        def previous(group):
            entry = deliveries.get(f'{key}#{group}')
            legacy = deliveries.get(key)  # single-group ledger before 2026-10-25
            if entry is None and isinstance(legacy, dict) and legacy.get('group_id') == group:
                entry = legacy
            return entry or {}

        statuses, todo = {}, []
        for group in groups:
            old = previous(group)
            if old.get('status') in ('sending', 'sent', 'uncertain'):
                statuses[group] = old['status']
            elif old.get('retry_after', 0) > now:
                statuses[group] = 'generation-backoff'
            else:
                todo.append(group)
        if not todo:
            return _combined(statuses[g] for g in groups)
        try:
            report = telemetry.weekly_report(now=now)
            message = telemetry.format_weekly(report)
            archive = root / 'weekly' / ('scheduled-' + due.strftime('%Y-%m-%d'))
            write_json(archive / 'report.json', report)
            atomic_text(archive / 'report.txt', format_report(report, details=True))
        except Exception as exc:
            for group in todo:
                deliveries[f'{key}#{group}'] = {'status': 'generation-failed', 'retry_after': now + 1800,
                                                'error_type': type(exc).__name__, 'group_id': group}
                statuses[group] = 'generation-failed'
            write_json(state_path, state)
            return _combined(statuses[g] for g in groups)
        for group in todo:
            entry = {'status': 'sending', 'scheduled_at': due.timestamp(), 'attempted_at': time.time(),
                     'group_id': group, 'report_path': str(archive / 'report.json')}
            deliveries[f'{key}#{group}'] = entry
            state['deliveries'] = deliveries = dict(sorted(deliveries.items())[-120:])
            write_json(state_path, state)
            try:
                accepted = push_fn(message, group) is True
                entry['status'] = 'sent' if accepted else 'uncertain'
            except Exception as exc:
                entry.update(status='uncertain', error_type=type(exc).__name__)
            entry['finished_at'] = time.time()
            write_json(state_path, state)
            if entry['status'] == 'uncertain':
                logging.getLogger(__name__).warning('Weekly delivery receipt uncertain; automatic replay suppressed. See weekly-delivery.json.')
            statuses[group] = entry['status']
        return _combined(statuses[g] for g in groups)
