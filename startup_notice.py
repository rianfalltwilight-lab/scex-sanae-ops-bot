"""Announce a new, verified Minecraft boot through the existing player channel.

Independent durable log cursor: first install is quiet, bot restarts resume it.
Only a recent real Done line plus the matching Java listener identity can send.
There are no Minecraft commands, extra services, or per-poll process probes.
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
from datetime import datetime
from pathlib import Path

from ops_contract import read_incremental_lines
from weekly_reports import HEADER, TZ, locked, log_stamp, read_json, write_json

import re
DONE = re.compile(r'Done \(([0-9]+(?:\.[0-9]+)?)s\)! For help, type "help"')
MAX_AGE = 600
MAX_BOOT = 6 * 3600
LOGGER = logging.getLogger(__name__)


def parse_lifecycle(line):
    match = HEADER.fullmatch(line)
    if not match or (match[2], match[3]) != ('Server thread', 'INFO'):
        return None
    if match[4] not in ('net.minecraft.server.dedicated.DedicatedServer/',
                        'net.minecraft.server.MinecraftServer/'):
        return None
    # Require a full date; never assign today's date to an old time-only line.
    stamp = log_stamp(match[1], '')
    if stamp is None:
        return None
    if match[5] == 'Stopping server':
        return {'type': 'stop', 'timestamp': stamp}
    done = DONE.fullmatch(match[5])
    if not done or not math.isfinite(float(done[1])) or float(done[1]) > MAX_BOOT:
        return None
    return {'type': 'ready', 'timestamp': stamp, 'internal_seconds': float(done[1]),
            'internal_text': done[1]}


def duration(seconds):
    seconds = max(0, int(seconds + .5))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (f'{hours}小时' if hours else '') + (f'{minutes}分' if minutes or hours else '') + f'{seconds}秒'


def plain(value):
    return re.sub(r'\[CQ:', '［CQ:', re.sub(r'[\r\n\t]+', ' ', str(value)), flags=re.I).strip()


def render_notice(server, ready, total_seconds):
    name = ('SCEX Legacy Genesis（怀旧服）' if server['id'] == 'legacy'
            else plain(server.get('name') or 'Minecraft 服务器'))
    address = plain(server.get('address') or '')
    message = (f'[上线] {name} 已启动完成，用时 {duration(total_seconds)}'
               f'（Minecraft 内部阶段：{ready["internal_text"]}s）。')
    if address:
        message += '地址：' + address
    return message


def process_identity(server):
    from lag_forensics import NativeProbe
    return NativeProbe(server, seconds=12).process()


class StartupNotifier:
    def __init__(self, server, root, identity_fn=None):
        self.server = server
        self.root = Path(root)
        self.path = self.root / 'startup-notice.json'
        self.log = Path(server['server_dir']) / 'logs' / 'latest.log'
        self.identity_fn = identity_fn or (lambda: process_identity(server))
        self.next_check = 0
        self.last_error_log = float('-inf')

    def _baseline(self, now):
        with self.log.open('rb') as stream:
            stat = self.log.stat()
            head = hashlib.sha256(stream.read(min(128, stat.st_size))).hexdigest()
        return {'schema': 1, 'initialized_at': now, 'deliveries': {}, 'pending': None,
                'cursor': {'inode': stat.st_ino, 'offset': stat.st_size, 'partial': '', 'head': head}}

    def poll(self, push_fn, group_id, now=None):
        now = float(time.time() if now is None else now)
        if now < self.next_check:
            return 'backoff'
        try:
            return self._poll(push_fn, group_id, now)
        except Exception as exc:
            # Preserve an unreadable ledger. Never blindly replay after corruption.
            self.next_check = now + 30
            if now - self.last_error_log >= 1800:
                LOGGER.error('Startup notice paused (%s); inspect startup-notice.json and current log.', type(exc).__name__)
                self.last_error_log = now
            return 'error'

    def _poll(self, push_fn, group_id, now):
        if not self.log.is_file():
            return 'log-missing'
        with locked(self.root / 'startup-notice.lock'):
            if not self.path.exists():
                write_json(self.path, self._baseline(now))
                return 'initialized-quietly'
            state = read_json(self.path)
            if not isinstance(state, dict) or state.get('schema') != 1 or not isinstance(state.get('deliveries'), dict) or not isinstance(state.get('cursor'), dict):
                raise ValueError('startup ledger unreadable')
            lines, cursor, ok = read_incremental_lines(self.log, state['cursor'])
            if not ok:
                return 'log-unavailable'
            changed = cursor != state['cursor']
            state['cursor'] = cursor
            for line in lines:
                event = parse_lifecycle(line)
                if not event:
                    continue
                if event['type'] == 'stop':
                    if state.get('pending') and event['timestamp'] >= state['pending']['timestamp']:
                        state['pending'] = None
                elif 0 <= now - event['timestamp'] <= MAX_AGE:
                    pending = state.get('pending')
                    if not pending or event['timestamp'] >= pending['timestamp']:
                        state['pending'] = event
                changed = True
            pending = state.get('pending')
            if pending and not 0 <= now - pending['timestamp'] <= MAX_AGE:
                state['last_skipped'] = {'reason': 'expired', 'timestamp': pending['timestamp']}
                state['pending'] = pending = None
                changed = True
            if not pending:
                if changed:
                    write_json(self.path, state)
                return 'idle'
            if pending.get('retry_after', 0) > now:
                if changed:
                    write_json(self.path, state)
                return 'identity-backoff'
            # Save the pending event before the slow read-only process query.
            write_json(self.path, state)
            try:
                identity = self.identity_fn()
                created = datetime.fromisoformat(identity['created'].replace('Z', '+00:00'))
                if created.tzinfo is None or int(identity['pid']) <= 0:
                    raise ValueError('identity incomplete')
                total = pending['timestamp'] - created.timestamp()
                if not max(0, pending['internal_seconds'] - 2) <= total <= MAX_BOOT:
                    # A Done line from another/previous JVM is not a current boot.
                    state['last_skipped'] = {'reason': 'process-time-mismatch', 'timestamp': pending['timestamp']}
                    state['pending'] = None
                    write_json(self.path, state)
                    return 'process-mismatch'
            except Exception as exc:
                pending.update(retry_after=now + 15, identity_error=type(exc).__name__)
                write_json(self.path, state)
                return 'identity-unavailable'
            key = f'{self.server["id"]}:{identity["pid"]}:{created.timestamp():.6f}'
            state['pending'] = None
            if key in state['deliveries']:
                write_json(self.path, state)
                return 'already-recorded'
            message = render_notice(self.server, pending, total)
            entry = {'status': 'sending', 'pid': int(identity['pid']), 'created': identity['created'],
                     'ready_at': pending['timestamp'], 'total_seconds': total,
                     'internal_seconds': pending['internal_seconds'], 'attempted_at': now,
                     'group_id': int(group_id), 'message': message}
            state['deliveries'][key] = entry
            state['deliveries'] = dict(sorted(state['deliveries'].items(), key=lambda item: item[1].get('ready_at', 0))[-100:])
            # Durable claim before any network send: uncertain receipts are not replayed.
            write_json(self.path, state)
            try:
                entry['status'] = 'sent' if push_fn(message, group_id) is True else 'uncertain'
            except Exception as exc:
                entry.update(status='uncertain', error_type=type(exc).__name__)
            entry['finished_at'] = time.time()
            write_json(self.path, state)
            if entry['status'] == 'uncertain':
                LOGGER.warning('Startup notice receipt uncertain; automatic replay suppressed. See startup-notice.json.')
            return entry['status']
