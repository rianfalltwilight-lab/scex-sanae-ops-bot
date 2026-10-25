#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SCEX Legacy Genesis 事件监控（WSL systemd user service）。"""
import hashlib
import json
from onebot_utf8 import onebot_headers
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

from onebot_utf8 import JSON_CONTENT_TYPE, json_bytes, onebot_success_utf8, utf8_text
from ops_contract import read_incremental_lines
from ops_telemetry import OpsTelemetry
from weekly_reports import publish_due
from startup_notice import StartupNotifier, parse_lifecycle
from lag_forensics import coordinator
from server_registry import parse_player_list, query_server, resolve_server, server_prefix
from advancement_localization import AdvancementLocalizer
from remote_log_reader import read_remote_lines, source_id


SERVER = resolve_server('legacy')
REMOTE = bool(SERVER.get('remote_ssh_host'))
PREFIX = server_prefix(SERVER)
SERVER_DIR = SERVER['server_dir']
LOG_PATH = os.path.join(SERVER_DIR, 'logs', 'latest.log')
CRASH_DIR = os.path.join(SERVER_DIR, 'crash-reports')
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state')
LLBOT_API = os.environ.get('LLBOT_API', 'http://127.0.0.1:3000')
LLBOT_GROUP = int(os.environ.get('LLBOT_GROUP', '0'))
MONITOR_GROUPS = tuple(dict.fromkeys(
    int(value.strip()) for value in os.environ.get('LEGACY_MONITOR_GROUPS', str(LLBOT_GROUP)).split(',')
    if value.strip().isdigit() and int(value.strip()) > 0)) or (LLBOT_GROUP,)
LLBOT_MANAGEMENT_GROUP = int(os.environ.get('LLBOT_MANAGEMENT_GROUP', '0'))
POLL_SECONDS = max(2, int(os.environ.get('LEGACY_MONITOR_INTERVAL', '5')))
os.makedirs(STATE_DIR, exist_ok=True)
OPS = OpsTelemetry('legacy', PREFIX, SERVER_DIR,
                   query_fn=lambda command: query_server(SERVER, command))
STARTUP = None if REMOTE else StartupNotifier(SERVER, OPS.root)
ADVANCEMENTS = AdvancementLocalizer(
    SERVER_DIR, os.path.join(STATE_DIR, 'legacy-advancement-localization.json'),
    extra_roots=[item for item in os.environ.get('ADVANCEMENT_PACK_ROOTS', '').split(os.pathsep)
                 if item])

JOIN_PATTERN = re.compile(
    r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: (\S+) joined the game')
CHAT_PATTERN = re.compile(
    r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: '
    r'(?:\[Not Secure\]\s+)?<([^>\r\n]{1,64})>\s+(.+)$')
CHAT_ECHO_PREFIXES = ('[QQ群]', '[Server]', '<Server>')
EVENT_PATTERNS = [
    (re.compile(r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: (\S+) left the game'),
     lambda match: f'{PREFIX} 玩家 {match.group(1)} 离开服务器'),
    (re.compile(r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: (\S+) was slain by (\S+)'),
     lambda match: f'{PREFIX} {match.group(1)} 被 {match.group(2)} 击杀'),
    (re.compile(r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: (\S+) drowned'),
     lambda match: f'{PREFIX} {match.group(1)} 淹死了'),
]
ADVANCEMENT_PATTERNS = [
    (re.compile(r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: (\S+) has made the advancement \[(.+)\]'), 'progress'),
    (re.compile(r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: (\S+) has reached the goal \[(.+)\]'), 'goal'),
    (re.compile(r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: (\S+) has completed the challenge \[(.+)\]'), 'challenge'),
]


def _state_path(name):
    return os.path.join(STATE_DIR, name + '.json')


def load_state(name, default=None):
    try:
        with open(_state_path(name), encoding='utf-8-sig') as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def save_state(name, value):
    path = _state_path(name)
    temp = path + '.tmp'
    with open(temp, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
    os.replace(temp, path)


def _baseline_log_state():
    stat = os.stat(LOG_PATH)
    with open(LOG_PATH, 'rb') as stream:
        head = hashlib.sha256(stream.read(min(128, stat.st_size))).hexdigest()
    return {'inode': stat.st_ino, 'offset': stat.st_size, 'partial': '', 'head': head}


def _join_message(player, query_fn=query_server):
    try:
        parsed = parse_player_list(query_fn(SERVER, 'list'))
    except Exception:
        parsed = None
    if not parsed:
        return f'{PREFIX} 玩家 {player} 加入服务器（人数暂不可查）'
    return f'{PREFIX} 玩家 {player} 加入服务器（{parsed["current"]}/{parsed["max"]}）'


def _join_is_duplicate(player, now=None):
    now = float(now if now is not None else time.time())
    state = load_state('legacy-join-dedupe', {}) or {}
    recent = {name: float(seen) for name, seen in state.items() if now - float(seen) < 30}
    key = player.casefold()
    duplicate = key in recent
    recent[key] = now
    save_state('legacy-join-dedupe', recent)
    return duplicate


def _player_chat_message(line):
    """Render an ordinary player chat line without allowing bridge/CQ echoes."""
    match = CHAT_PATTERN.search(line)
    if not match:
        return None
    player = match.group(1).strip()
    content = re.sub(r'[\r\n\t]+', ' ', match.group(2)).strip()
    content = re.sub(r'§[0-9A-FK-ORa-fk-or]', '', content)
    if not player or player.casefold() == 'server':
        return None
    if not content or content.startswith(CHAT_ECHO_PREFIXES):
        return None
    # OneBot interprets CQ segments embedded in plain text. Full-width '[' keeps
    # the visible text while preventing a player from injecting mentions/images.
    content = re.sub(r'\[CQ:', '［CQ:', content, flags=re.IGNORECASE)
    return f'{PREFIX} <{player}> {content[:400]}'


def logwatch(query_fn=query_server, include_ops=False):
    state = load_state('legacy-logwatch')
    if REMOTE:
        lines, next_state, ok = read_remote_lines(SERVER, state)
    else:
        if not os.path.isfile(LOG_PATH):
            return ([], []) if include_ops else []
        if state is None:
            save_state('legacy-logwatch', _baseline_log_state())
            return ([], []) if include_ops else []
        lines, next_state, ok = read_incremental_lines(LOG_PATH, state)
    if not ok:
        return ([], []) if include_ops else []
    ops_alerts = OPS.observe_lines(lines)
    events = []
    for line in lines:
        if REMOTE:
            notice = _remote_startup_notice(line)
            if notice:
                events.append(notice)
                continue
        joined = JOIN_PATTERN.search(line)
        if joined:
            player = joined.group(1)
            if not _join_is_duplicate(player):
                rendered = _join_message(player, query_fn)
                events.append(rendered)
                OPS.record_event('join', rendered, player=player)
            continue
        chat = _player_chat_message(line)
        if chat:
            events.append(chat)
            continue
        advancement = None
        for pattern, kind in ADVANCEMENT_PATTERNS:
            match = pattern.search(line)
            if match:
                advancement = (match, kind)
                break
        if advancement:
            match, kind = advancement
            if REMOTE:
                # Preserve the server's title until a remote resource index is
                # available; never rebuild translations from the old local pack.
                label = {'progress': '进度', 'goal': '目标', 'challenge': '挑战'}[kind]
                rendered = f'{PREFIX} {match.group(1)} 完成{label} [{match.group(2)}]'
            else:
                rendered = ADVANCEMENTS.format_event(PREFIX, match.group(1), kind, match.group(2))
            events.append(rendered)
            OPS.record_event('minecraft_event', rendered)
            continue
        for pattern, render in EVENT_PATTERNS:
            match = pattern.search(line)
            if match:
                rendered = render(match)
                events.append(rendered)
                OPS.record_event('minecraft_event', rendered)
                break
    save_state('legacy-logwatch', next_state)
    return (events, ops_alerts) if include_ops else events


def _crash_summary(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as stream:
            lines = [stream.readline() for _ in range(800)]
        description = root_cause = ''
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('Description:') and not description:
                description = stripped.split(':', 1)[1].strip()[:220]
            if stripped.startswith('Caused by:') and not root_cause:
                root_cause = stripped[:260]
            elif (not root_cause and
                  re.match(r'(?:java|net|com)\.[\w.$]+(?:Exception|Error)(?::|$)', stripped)):
                root_cause = stripped[:260]
        return root_cause or description or '关键根因暂不可读'
    except Exception:
        return 'crash-report 不可读'


def crashwatch():
    # This host has no remote crash-report mirror. Severe errors from the live
    # remote log still flow through OPS.observe_lines; do not scan the old host.
    if REMOTE:
        return []
    if not os.path.isdir(CRASH_DIR):
        return []
    files = sorted(name for name in os.listdir(CRASH_DIR) if name.endswith('.txt'))
    state = load_state('legacy-crashwatch')
    if state is None:
        save_state('legacy-crashwatch', {'initialized': True, 'seen': files})
        return []
    seen = set(state.get('seen', []))
    new = [name for name in files if name not in seen]
    save_state('legacy-crashwatch', {'initialized': True, 'seen': files})
    events = []
    for name in new:
        path = os.path.join(CRASH_DIR, name)
        when = datetime.fromtimestamp(os.path.getmtime(path)).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
        rendered = f'{PREFIX} 崩溃 {when}｜{name}｜{_crash_summary(path)}'
        events.append(rendered)
        OPS.record_event('crash', rendered, report=name)
    return events


def _remote_startup_notice(line):
    ready = parse_lifecycle(line)
    if not ready or ready['type'] != 'ready' or not 0 <= time.time() - ready['timestamp'] <= 600:
        return None
    source = source_id(SERVER)
    state = load_state('legacy-remote-startup', {}) or {}
    if state.get('source') == source and float(state.get('timestamp', 0)) >= ready['timestamp']:
        return None
    save_state('legacy-remote-startup', {'source': source, 'timestamp': ready['timestamp']})
    return (f'{PREFIX} 服务器已启动完成（Minecraft 内部阶段：{ready["internal_text"]}s）。')


def _sample_remote_runtime():
    """RCON-only sampling: no local process, backup, index or lag probes."""
    now = time.time()
    state = load_state('legacy-remote-sample', {}) or {}
    if now - float(state.get('attempted_at', 0)) < 60:
        return
    save_state('legacy-remote-sample', {'attempted_at': now})
    OPS.sample_runtime(now)


def push_group(text, group_id=LLBOT_GROUP):
    request = urllib.request.Request(
        LLBOT_API + '/send_group_msg',
        data=json_bytes({'group_id': group_id, 'message': text}),
        headers=onebot_headers(), method='POST')
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = utf8_text(response.read())[:500]
        ok, data = onebot_success_utf8(raw)
        if not ok:
            print(f'{PREFIX} [push错误] group={group_id} '
                  f'retcode={(data or {}).get("retcode")}', file=sys.stderr)
        return ok
    except Exception as exc:
        print(f'{PREFIX} [push错误] group={group_id} '
              f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return False


def _flush_pending(state_name, new_events, group_id, limit, push_fn,
                   max_age_seconds=None):
    pending = (load_state(state_name, {'items': []}) or {}).get('items', [])
    if max_age_seconds is None:
        # Management alerts keep their existing durable delivery policy.
        queue = pending + [event for event in new_events if event]
    else:
        # 群聊是实时转发：退群、禁言或网络故障不能变成无限期历史补发。
        # 旧字符串队列没有时间戳，无法证明新鲜，保留到归档但不再发送。
        now = time.time()
        queue, expired = [], []
        for item in pending:
            stamp = item.get('queued_at') if isinstance(item, dict) else None
            if (isinstance(stamp, (int, float)) and
                    0 <= now - stamp < max_age_seconds and item.get('text')):
                queue.append(item)
            else:
                expired.append(item)
        if expired:
            archive = os.path.join(STATE_DIR, state_name + '-expired.jsonl')
            with open(archive, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps({'archived_at': now, 'group_id': group_id,
                    'reason': 'expired_or_legacy_undated', 'items': expired},
                    ensure_ascii=False) + '\n')
            print(f'{PREFIX} [relay过期归档] group={group_id} count={len(expired)}', flush=True)
        queue.extend({'text': event, 'queued_at': now}
                     for event in new_events if event)
        # Save even when emptied so a restart cannot replay an archived batch.
        save_state(state_name, {'items': queue})
    if not queue:
        return []
    save_state(state_name, {'items': queue})
    batch = queue[:limit]
    rendered = ([item['text'] for item in batch]
                if max_age_seconds is not None else batch)
    if push_fn('\n'.join(rendered), group_id):
        save_state(state_name, {'items': queue[len(batch):]})
        return rendered
    return []


def scan_once(push_fn=push_group):
    watched = logwatch(include_ops=True)
    if isinstance(watched, tuple) and len(watched) == 2:
        regular_events, ops_alerts = watched
    else:  # Preserve compatibility with existing offline fixtures/mocks.
        regular_events, ops_alerts = watched, []
    crash_events = crashwatch()
    player_batch = []
    for group_id in MONITOR_GROUPS:
        # Retain the original group's queue; other groups have separate ACKs.
        queue_name = ('legacy-monitor-pending' if group_id == LLBOT_GROUP
                      else f'legacy-monitor-pending-{group_id}')
        delivered = _flush_pending(queue_name, regular_events + crash_events,
                                   group_id, 10, push_fn, max_age_seconds=120)
        player_batch.extend(event for event in delivered if event not in player_batch)
    _flush_pending(
        'legacy-crash-management-pending', crash_events,
        LLBOT_MANAGEMENT_GROUP, 10, push_fn)
    _flush_pending(
        'legacy-ops-management-pending', ops_alerts,
        LLBOT_MANAGEMENT_GROUP, 10, push_fn)
    if push_fn is push_group and REMOTE:
        _sample_remote_runtime()
    elif push_fn is push_group:
        STARTUP.poll(push_fn, LLBOT_GROUP)
        coordinator(OPS).deliver(lambda summary: push_fn(summary, LLBOT_GROUP))
        maintenance = OPS.periodic_maintenance()
        if maintenance['errors']:
            print(f'{PREFIX} [ops采集] ' + ','.join(maintenance['errors']), file=sys.stderr)
        weekly_status = publish_due(OPS, push_fn, LLBOT_GROUP)
        if weekly_status == 'generation-failed':
            print(f'{PREFIX} [周报] 生成失败，30分钟后重试；详见 weekly-delivery.json', file=sys.stderr)
    if player_batch:
        print('\n'.join(player_batch), flush=True)
    return player_batch


def main():
    if REMOTE:
        print(f'{PREFIX} 远端日志监控已启用；崩溃报告目录、本机进程/备份/索引采集未启用。', flush=True)
    if '--once' in sys.argv:
        scan_once()
        return
    while True:
        try:
            scan_once()
        except Exception as exc:
            print(f'{PREFIX} [monitor错误] {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
