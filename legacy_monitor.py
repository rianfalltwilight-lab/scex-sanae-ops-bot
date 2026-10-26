#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SCEX Legacy Genesis 事件监控（WSL systemd user service）。"""
import hashlib
import json
from onebot_utf8 import onebot_headers
import os
import re
import sys
import threading
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
from remote_crash_reader import read_remote_crashes, render as render_crash
import remote_mods_mirror
import remote_server_reader
import command_catalog
import player_progress
import server_watch
import world_status


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
COLLECT_SECONDS = max(15, int(os.environ.get('WEEKLY_COLLECT_INTERVAL', '30')))
STALE_CURSOR_SECONDS = 600
CRASH_POLL_SECONDS = 60
MIRROR_SECONDS = 1800
CATALOG_SECONDS = 86400
# Small text trees mirrored next to the mods: quest book, configs, scripts, the per-team
# quest progress with team membership (quest_progress) and per-player advancements.
MIRROR_TREES = ('config', 'kubejs', 'defaultconfigs', 'world/serverconfig', 'world/ftbquests', 'world/ftbteams',
                'world/advancements')
RESOURCES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources')
# Remote crash notices go here (management group in production); empty = record only.
CRASH_NOTIFY_GROUPS = tuple(dict.fromkeys(
    int(value.strip()) for value in os.environ.get('LEGACY_CRASH_NOTIFY_GROUPS', '').split(',')
    if value.strip().isdigit() and int(value.strip()) > 0))



def _groups(name):
    return tuple(dict.fromkeys(
        int(value.strip()) for value in os.environ.get(name, '').split(',')
        if value.strip().isdigit() and int(value.strip()) > 0))


# Sustained low TPS goes here (management group in production); empty = off.
LAG_ALERT_GROUPS = _groups('LEGACY_LAG_ALERT_GROUPS')
LAG_ALERT_TPS = float(os.environ.get('LEGACY_LAG_ALERT_TPS', '10'))
LAG_ALERT_MINUTES = 5
LAG_ALERT_REPEAT = 3600
# Mod changes found by the mirror; empty = record only.
UPDATE_NOTIFY_GROUPS = _groups('LEGACY_UPDATE_NOTIFY_GROUPS')
# The bridge sets this to a one-line AI summary of a change list; None = list only.
UPDATE_SUMMARY_FN = None
_NOTICE_LOCK = threading.Lock()
_MIRROR_THREAD = None
os.makedirs(STATE_DIR, exist_ok=True)
OPS = OpsTelemetry('legacy', PREFIX, SERVER_DIR,
                   query_fn=lambda command: query_server(SERVER, command), remote=REMOTE)
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
    """RCON-only sampling: no local process, backup, index or lag probes. Returns the new row or None."""
    now = time.time()
    state = load_state('legacy-remote-sample', {}) or {}
    if now - float(state.get('attempted_at', 0)) < 60:
        return None
    save_state('legacy-remote-sample', {'attempted_at': now})
    return OPS.sample_runtime(now)


def _queue_notice(name, groups, text):
    """Durable per-group queue, flushed by the collector loop (_flush_notices)."""
    with _NOTICE_LOCK:
        for group in groups:
            state_name = f'{name}-{group}'
            items = (load_state(state_name, {'items': []}) or {}).get('items', [])
            items.append({'text': text, 'queued_at': time.time()})
            save_state(state_name, {'items': items[-20:]})


def _flush_notices(push_fn):
    """One message per queued notice; lag alerts expire after 30 minutes, updates after 6 hours."""
    with _NOTICE_LOCK:
        for name, groups, max_age in (('legacy-lag-notify', LAG_ALERT_GROUPS, 1800),
                                      ('legacy-update-notify', UPDATE_NOTIFY_GROUPS, 6 * 3600)):
            for group in groups:
                state_name = f'{name}-{group}'
                if (load_state(state_name, {'items': []}) or {}).get('items'):
                    _flush_pending(state_name, [], group, 1, push_fn, max_age_seconds=max_age)


def _lag_watch(row):
    """Queue an alert when TPS stays below LAG_ALERT_TPS for LAG_ALERT_MINUTES samples."""
    if not LAG_ALERT_GROUPS or not row:
        return None
    state = load_state('legacy-lag-watch', {}) or {}
    state, streak = server_watch.lag_step(state, row, threshold=LAG_ALERT_TPS,
                                          minutes=LAG_ALERT_MINUTES, repeat=LAG_ALERT_REPEAT)
    text = None
    # A fresh streak within an hour of the last alert stays quiet; a long one repeats hourly.
    if streak and (streak['continuing']
                   or time.time() - float(state.get('last_alert_at') or 0) >= LAG_ALERT_REPEAT):
        details = {}
        for command in ('neoforge tps', 'list', 'neoforge entity list', 'mio_icif perf',
                        'neoforge entity list "minecraft:item"', 'neoforge entity list "minecraft:experience_orb"',
                        'statistics block-entities byType', 'servercore status', 'tick query'):
            try:
                details[command] = query_server(SERVER, command)
            except Exception:
                details[command] = ''
        try:
            host = remote_server_reader.host_status(SERVER) if REMOTE else None
        except Exception:
            host = None
        extra = world_status.lag_alert_lines(details['neoforge entity list'], details['mio_icif perf'],
                                             details['neoforge entity list "minecraft:item"'],
                                             details['neoforge entity list "minecraft:experience_orb"'],
                                             details['statistics block-entities byType'],
                                             details['servercore status'], host, details['tick query'])
        text = server_watch.format_lag_alert(PREFIX, streak, details['neoforge tps'], details['list'], extra)
        state['last_alert_at'] = time.time()
        OPS.record_event('lag_alert', text, min_tps=streak['min_tps'], since=streak['start'])
        print(f'{PREFIX} [卡顿提醒] 最低 TPS {streak["min_tps"]:.1f}，已排队发送', flush=True)
    save_state('legacy-lag-watch', state)
    if text:
        _queue_notice('legacy-lag-notify', LAG_ALERT_GROUPS, text)
    return text


def _announce_update(old_inventory, result):
    """Queue a version diff when the mirror changed the installed mods."""
    if not (old_inventory or {}).get('mods'):
        return None          # first index: nothing to compare with
    try:
        with open(os.path.join(str(OPS.root), 'mod-inventory.json'), encoding='utf-8') as stream:
            new_inventory = json.load(stream)
    except (OSError, ValueError):
        return None
    changes = server_watch.mod_changes(old_inventory, new_inventory, result.get('fetched') or ())
    if not any(changes.values()):
        return None
    now = time.time()
    summary = ''
    if UPDATE_SUMMARY_FN is not None:
        try:
            summary = UPDATE_SUMMARY_FN(server_watch.format_update(PREFIX, changes, now, limit=120)) or ''
        except Exception as exc:
            print(f'{PREFIX} [模组更新] 概括失败 {type(exc).__name__}', flush=True)
    text = server_watch.format_update(PREFIX, changes, now, summary)
    OPS.record_event('mod_update', text, updated=len(changes['updated']),
                     added=len(changes['added']), removed=len(changes['removed']))
    if UPDATE_NOTIFY_GROUPS:
        _queue_notice('legacy-update-notify', UPDATE_NOTIFY_GROUPS, text)
    return text


def _publish_weekly(push_fn):
    # Every player group gets the report; each has its own at-most-once entry.
    status = publish_due(OPS, push_fn, MONITOR_GROUPS)
    if status == 'generation-failed':
        print(f'{PREFIX} [周报] 生成失败，30分钟后重试；详见 weekly-delivery.json', file=sys.stderr)
    return status


def remote_crashwatch(now=None):
    """New remote crash reports as rendered lines, polled at most once a minute.

    Every report is recorded as a 'crash' event at its file time, so the weekly
    report counts it. On first attach the last seven days come back as history:
    recorded, but not returned for announcement.
    """
    now = time.time() if now is None else now
    state = load_state('legacy-remote-crashwatch', {}) or {}
    if now - float(state.get('attempted_at', 0)) < CRASH_POLL_SECONDS:
        return []
    reports, next_state, ok = read_remote_crashes(SERVER, state, now=now)
    fresh = []
    if ok:
        for report in sorted(reports, key=lambda row: row['mtime']):
            rendered = render_crash(PREFIX, report)
            OPS.record_event('crash', rendered, occurred_at=report['mtime'],
                             report=report['name'], kind=report['kind'], source='remote')
            if not report['historic']:
                fresh.append(rendered)
        if reports:
            print(f'{PREFIX} [崩溃监视] 新增 {len(fresh)} 份，历史补记 {len(reports) - len(fresh)} 份', flush=True)
    # A failed read keeps the previous cursor whatever the reader handed back.
    save_state('legacy-remote-crashwatch', dict((next_state if ok else state) or {}, attempted_at=now))
    return fresh


def _notify_crashes(events, push_fn):
    # Timestamped queue: a failed push retries for up to six hours, then is archived.
    for group in CRASH_NOTIFY_GROUPS:
        _flush_pending(f'legacy-crash-notify-pending-{group}', events, group, 5, push_fn,
                       max_age_seconds=6 * 3600)


def _mirror_extras():
    """Config trees, player statistics and (daily) the command catalog; each best effort."""
    notes = []
    for tree in MIRROR_TREES:
        try:
            result = remote_mods_mirror.sync_tree(SERVER, tree, SERVER_DIR)
            if result['fetched'] or result['deleted'] or result['missing']:
                notes.append(f"{tree} 更新{result['fetched']} 删除{result['deleted']}"
                             + (f" 未完成{result['missing']}" if result['missing'] else ''))
        except Exception as exc:
            notes.append(f'{tree} 同步失败 {type(exc).__name__}')
    try:
        stats = remote_server_reader.player_stats(SERVER)
        path = os.path.join(str(OPS.root), 'player-stats.json')
        temp = path + '.tmp'
        with open(temp, 'w', encoding='utf-8') as stream:
            json.dump(stats, stream, ensure_ascii=False)
        os.replace(temp, path)
        # One copy a day for the weekly player section.
        server_watch.snapshot_player_stats(stats, os.path.join(str(OPS.root), 'player-stats-history'))
    except Exception as exc:
        notes.append(f'玩家统计同步失败 {type(exc).__name__}')
    try:
        # Force-loading tickets as last saved (vanilla and mods), for the chunk report.
        forced = remote_server_reader.forced_chunks(SERVER)
        path = os.path.join(str(OPS.root), 'forced-chunks.json')
        with open(path + '.tmp', 'w', encoding='utf-8') as stream:
            json.dump(forced, stream, ensure_ascii=False)
        os.replace(path + '.tmp', path)
        if forced['errors']:
            notes.append('强加载快照部分读取失败 ' + '、'.join(forced['errors'][:3]))
    except Exception as exc:
        notes.append(f'强加载快照失败 {type(exc).__name__}')
    try:
        # Research keys, clues, aspects and warp per player from world/playerdata.
        progress = remote_server_reader.thaumcraft_progress(SERVER)
        _write_json(os.path.join(str(OPS.root), 'thaumcraft-progress.json'), progress)
        if progress['errors']:
            notes.append(f"神秘研究快照有 {progress['errors']} 个玩家文件读取失败")
    except Exception as exc:
        notes.append(f'神秘研究快照失败 {type(exc).__name__}')
    try:
        _refresh_research_index(notes)
    except Exception as exc:
        notes.append(f'神秘研究目录刷新失败 {type(exc).__name__}')
    try:
        # Last login and login count from the MSLX panel; the IPs it also keeps stay on the host.
        _write_json(os.path.join(str(OPS.root), 'login-records.json'), remote_server_reader.login_records(SERVER))
    except Exception as exc:
        notes.append(f'登录记录同步失败 {type(exc).__name__}')
    try:
        _refresh_advancement_index(notes)
        player_progress.warm_advancements(SERVER_DIR, str(OPS.root))
    except Exception as exc:
        notes.append(f'进度目录刷新失败 {type(exc).__name__}')
    state = load_state('legacy-command-catalog', {}) or {}
    if time.time() - float(state.get('refreshed_at', 0)) >= CATALOG_SECONDS:
        try:
            row = command_catalog.refresh_server(SERVER, query_server)
            save_state('legacy-command-catalog', {'refreshed_at': time.time(), 'count': row['count']})
            notes.append(f"命令目录刷新 {row['count']} 条")
        except Exception as exc:
            notes.append(f'命令目录刷新失败 {type(exc).__name__}')
    if notes:
        print(f'{PREFIX} [服务器资料] ' + '；'.join(notes), flush=True)


def _write_json(path, data):
    with open(path + '.tmp', 'w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False)
    os.replace(path + '.tmp', path)


def _refresh_research_index(notes):
    """Daily, or when the mod set changed: every research key with its name and mod."""
    inventory = os.path.join(str(OPS.root), 'mod-inventory.json')
    try:
        with open(inventory, encoding='utf-8') as stream:
            signature = str(json.load(stream).get('signature') or '')
    except (OSError, ValueError, AttributeError):
        signature = ''
    state = load_state('legacy-research-index', {}) or {}
    path = os.path.join(str(OPS.root), 'thaumcraft-research.json')
    if (os.path.exists(path) and state.get('signature') == signature
            and time.time() - float(state.get('refreshed_at', 0)) < CATALOG_SECONDS):
        return
    try:
        with open(path, encoding='utf-8') as stream:
            previous = json.load(stream)
    except (OSError, ValueError):
        previous = None
    try:
        listing = query_server(SERVER, 'thaumcraft research list')
    except Exception:
        listing = ''          # server down: keep the last known key list
    index = player_progress.build_research_index(os.path.join(SERVER_DIR, 'mods'), inventory, listing, previous)
    if not index['keys'] and not index['names']:
        return
    _write_json(path, index)
    if index['keys']:         # without the server's list, try again next cycle
        save_state('legacy-research-index', {'refreshed_at': time.time(), 'signature': signature,
                                             'count': len(index['keys'])})
    notes.append(f"神秘研究目录 {len(index['keys'])} 项")


def _refresh_advancement_index(notes):
    """Daily, or when the mod set changed: every advancement shown in the advancement screen."""
    inventory = os.path.join(str(OPS.root), 'mod-inventory.json')
    try:
        with open(inventory, encoding='utf-8') as stream:
            signature = str(json.load(stream).get('signature') or '')
    except (OSError, ValueError, AttributeError):
        signature = ''
    state = load_state('legacy-advancement-index', {}) or {}
    path = os.path.join(str(OPS.root), 'advancement-index.json')
    if (os.path.exists(path) and state.get('signature') == signature
            and time.time() - float(state.get('refreshed_at', 0)) < CATALOG_SECONDS):
        return
    try:
        with open(path, encoding='utf-8') as stream:
            previous = json.load(stream)
    except (OSError, ValueError):
        previous = {}
    try:
        vanilla = remote_server_reader.vanilla_advancements(SERVER)
    except Exception:
        vanilla = {}
    vanilla = vanilla or previous.get('vanilla') or {}          # host unreachable: keep the last list
    index = player_progress.build_advancement_index(
        os.path.join(SERVER_DIR, 'mods'), inventory, vanilla, os.path.join(RESOURCES_DIR, 'advancement-overrides'),
        os.path.join(RESOURCES_DIR, 'minecraft-1.21.1', 'lang', 'zh_cn.json'))
    if not index['advancements']:
        return
    index['vanilla'] = vanilla
    _write_json(path, index)
    if vanilla:               # without the vanilla list, try again next cycle
        save_state('legacy-advancement-index', {'refreshed_at': time.time(), 'signature': signature,
                                                'count': len(index['advancements'])})
    notes.append(f"进度目录 {len(index['advancements'])} 项")


def _mirror_job():
    try:
        _mirror_extras()
    except Exception as exc:
        print(f'{PREFIX} [服务器资料] 同步失败 {type(exc).__name__}', flush=True)
    try:
        try:
            with open(os.path.join(str(OPS.root), 'mod-inventory.json'), encoding='utf-8') as stream:
                old_inventory = json.load(stream)
        except (OSError, ValueError):
            old_inventory = None
        result = remote_mods_mirror.sync(SERVER, SERVER_DIR)
        changed = OPS.refresh_indexes()
        if 'mods' in changed:
            try:
                _announce_update(old_inventory, result)
            except Exception as exc:
                print(f'{PREFIX} [模组更新] 通知失败 {type(exc).__name__}', flush=True)
        properties = result.get('properties')
        if (result['changed'] or changed or not result['complete'] or result['skipped']
                or properties in ('updated', 'failed')):
            print(f'{PREFIX} [模组镜像] 远端 {result["remote"]} 个，更新 {len(result["fetched"])}，'
                  f'删除 {len(result["deleted"])}，索引 {"、".join(changed) or "未变"}'
                  + (f'，未完成 {len(result["missing"])}' if result['missing'] else '')
                  + (f'，跳过非法文件名 {len(result["skipped"])}' if result['skipped'] else '')
                  + ({'updated': '，server.properties 已更新', 'failed': '，server.properties 读取失败'}
                     .get(properties, '')),
                  flush=True)
    except Exception as exc:
        print(f'{PREFIX} [模组镜像] 同步失败 {type(exc).__name__}，30 分钟后重试', flush=True)


def refresh_mirror(now=None, spawn=None):
    """Every 30 minutes, mirror remote mods/*.jar and rebuild the mod/recipe indexes."""
    global _MIRROR_THREAD
    now = time.time() if now is None else now
    state = load_state('legacy-mods-mirror', {}) or {}
    if now - float(state.get('attempted_at', 0)) < MIRROR_SECONDS:
        return False
    if _MIRROR_THREAD is not None and _MIRROR_THREAD.is_alive():
        return False
    save_state('legacy-mods-mirror', {'attempted_at': now})
    if spawn is not None:
        spawn(_mirror_job)
    else:
        _MIRROR_THREAD = threading.Thread(target=_mirror_job, name='mods-mirror', daemon=True)
        _MIRROR_THREAD.start()
    return True


def _drop_stale_cursor(now=None, max_age=STALE_CURSOR_SECONDS):
    """After a long outage, attach at the live end instead of replaying it.

    Replayed joins and errors would carry the replay time and misdate the
    weekly report; the outage appears there as a sampling gap instead.
    """
    path = _state_path('legacy-logwatch')
    try:
        age = (time.time() if now is None else now) - os.path.getmtime(path)
    except OSError:
        return False
    if age <= max_age:
        return False
    os.replace(path, path + '.stale')
    print(f'{PREFIX} [周报采集] 日志游标已停 {int(age)} 秒，从当前末尾重新接入。', flush=True)
    return True


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
    if REMOTE and push_fn is push_group:
        crash_events = remote_crashwatch()
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
        _lag_watch(_sample_remote_runtime())
        refresh_mirror()
        _flush_notices(push_fn)
        _publish_weekly(push_fn)
    elif push_fn is push_group:
        STARTUP.poll(push_fn, LLBOT_GROUP)
        coordinator(OPS).deliver(lambda summary: push_fn(summary, LLBOT_GROUP))
        maintenance = OPS.periodic_maintenance()
        if maintenance['errors']:
            print(f'{PREFIX} [ops采集] ' + ','.join(maintenance['errors']), file=sys.stderr)
        _publish_weekly(push_fn)
    if player_batch:
        print('\n'.join(player_batch), flush=True)
    return player_batch


def collect_once(push_fn=push_group):
    """Weekly-report collection with relay off: record and sample, never forward.

    Log lines still pass through logwatch so joins and error fingerprints are
    recorded, but chat, join and ops notices are discarded. It sends only the
    scheduled weekly report and, if configured, remote crash notices.
    """
    if REMOTE:
        _drop_stale_cursor()
        logwatch(include_ops=True)
        _lag_watch(_sample_remote_runtime())
        crashes = remote_crashwatch()
        if CRASH_NOTIFY_GROUPS:
            _notify_crashes(crashes, push_fn)
        refresh_mirror()
        _flush_notices(push_fn)
    else:
        logwatch(include_ops=True)
        crashwatch()
        maintenance = OPS.periodic_maintenance()
        if maintenance['errors']:
            print(f'{PREFIX} [ops采集] ' + ','.join(maintenance['errors']), file=sys.stderr)
    return _publish_weekly(push_fn)


def collect_main():
    print(f'{PREFIX} 周报采集已启用：只采样并按时发周报，不转发聊天和进服；'
          f'崩溃通知 {",".join(map(str, CRASH_NOTIFY_GROUPS)) or "关闭"}；'
          f'卡顿提醒（TPS<{LAG_ALERT_TPS:g} 连续 {LAG_ALERT_MINUTES} 分钟）{",".join(map(str, LAG_ALERT_GROUPS)) or "关闭"}；'
          f'模组更新通知 {",".join(map(str, UPDATE_NOTIFY_GROUPS)) or "关闭"}。', flush=True)
    while True:
        try:
            collect_once()
        except Exception as exc:
            print(f'{PREFIX} [周报采集错误] {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        time.sleep(COLLECT_SECONDS)


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
