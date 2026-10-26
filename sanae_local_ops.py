"""Read-only AI operations bound to an enabled canonical local server."""
import os
from pathlib import Path
import re
import subprocess
import threading
import time

import server_registry
import server_insight
import player_progress
import remote_server_reader
import world_records
import world_status
from mod_inventory import scan_installed_mods, format_inventory
from ops_telemetry import OpsTelemetry
from runtime_paths import default_path

LOCAL_READ_TOOLS = frozenset({'read_server_log', 'read_crash_report', 'list_mods', 'incident_postmortem'})
DISABLED_FILE_TOOLS = frozenset({'list_dir', 'read_file', 'search_files', 'read_nbt',
                                 'set_server_property', 'replace_in_config'})
# Read-only answers from collected and mirrored data. Config lookups are admins only:
# configs and scripts describe server internals that members do not need.
INSIGHT_TOOLS = frozenset({'server_trends', 'server_events', 'player_stats', 'quest_lookup',
                           'search_server_config', 'read_server_config', 'server_world',
                           'quest_progress', 'thaumcraft_progress', 'advancement_progress',
                           'server_market', 'thaumcraft_world', 'machine_profile'})
ENTITY_ALIASES = {'掉落物': 'minecraft:item', '物品': 'minecraft:item', 'item': 'minecraft:item',
                  '经验球': 'minecraft:experience_orb', '经验': 'minecraft:experience_orb',
                  'experience_orb': 'minecraft:experience_orb', 'xp': 'minecraft:experience_orb'}
ADMIN_INSIGHT_TOOLS = frozenset({'search_server_config', 'read_server_config'})
WORLD_TOPICS = ('chunks', 'entities', 'lag', 'machines', 'host')
HOST_CACHE_SECONDS = 30
_HOST_CACHE = {}
# Admin-started NeoForge timing samples: one at a time, a fresh one at most every 5 minutes.
PROFILE_COOLDOWN = 300
_PROFILE_LOCK = threading.Lock()
_PROFILE_LAST = {}
_sleep = time.sleep


def canonical_local_server(selected):
    if not isinstance(selected, dict) or not selected.get('id'):
        raise ValueError('请先明确选择现役服务器')
    server = server_registry.resolve_server(selected['id'])
    if not server or server['id'] != selected['id']:
        raise ValueError('目标不是当前启用的服务器')
    root = Path(server.get('server_dir') or '')
    if not root.is_absolute() or str(root).startswith('\\\\'):
        raise ValueError('该目标未配置为本机绝对目录')
    root = root.resolve()
    if str(root).startswith('\\\\') or not root.is_dir():
        raise ValueError('现役本地服务器目录不可用')
    return server, root


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('拒绝访问服务器目录之外的路径')
    return path


def tail(path, *, max_bytes=1024 * 1024, lines=400):
    with path.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        start = max(0, stream.tell() - max_bytes)
        stream.seek(start)
        raw = stream.read(max_bytes)
    if start:
        raw = raw.partition(b'\n')[2]
    value = '\n'.join(raw.decode('utf-8', errors='replace').splitlines()[-lines:])
    return re.sub(r'(?i)\b(password|passwd|token|secret|api[_-]?key)\b\s*[:=]\s*[^\s,;]+',
                  r'\1=<redacted>', value)


def _resolved_server(selected):
    if not isinstance(selected, dict) or not selected.get('id'):
        raise ValueError('请先明确选择现役服务器')
    server = server_registry.resolve_server(selected['id'])
    if not server or server['id'] != selected['id']:
        raise ValueError('目标不是当前启用的服务器')
    return server


def execute_insight(name, args, selected, privileged):
    if name in ADMIN_INSIGHT_TOOLS and not privileged:
        return '拒绝：配置和脚本查询仅群主/管理员可用。'
    server = _resolved_server(selected)
    ops_root = Path(os.environ.get('OPS_STATE_BASE') or default_path('state', 'ops')) / server['id']
    mirror = server.get('server_dir') or ''
    prefix = server['prefix'] + ' '
    if name == 'server_trends':
        return prefix + server_insight.trends(ops_root, args.get('window') or '24h')
    if name == 'server_events':
        try:
            # Live names let the tool say how long each online player has been on.
            online = (server_registry.parse_player_list(server_registry.query_server(server, 'list')) or {}).get('players')
        except Exception:
            online = None
        return prefix + server_insight.events(ops_root, args.get('window') or '24h',
                                              player=args.get('player') or '',
                                              kind=args.get('kind') or '',
                                              limit=args.get('limit') or 30, online=online)
    if name == 'player_stats':
        return prefix + server_insight.player_stats(ops_root, player=args.get('player') or '',
                                                    top=args.get('top') or '')
    if name == 'quest_lookup':
        return prefix + server_insight.quest_lookup(mirror, ops_root, args.get('query') or '')
    if name == 'server_world':
        return _world(server, args, ops_root, mirror, privileged)
    if name == 'quest_progress':
        return prefix + player_progress.quest_progress(mirror, ops_root, args.get('player') or '')
    if name == 'thaumcraft_progress':
        return prefix + player_progress.research_progress(ops_root, args.get('player') or '', args.get('top') or '',
                                                          mirror_root=mirror or None)
    if name == 'advancement_progress':
        return prefix + player_progress.advancement_progress(mirror, ops_root, args.get('player') or '',
                                                             args.get('query') or '')
    if name in ('server_market', 'thaumcraft_world'):
        if not server.get('remote_ssh_host'):
            return prefix + '这台服务器不在远端主机上，暂时查不了存档数据。'
        try:
            if name == 'server_market':
                return world_records.format_market(server['prefix'], world_records.market(server),
                                                   world_records.item_names(ops_root), args.get('item') or '')
            topic = str(args.get('topic') or 'nodes')
            if topic not in ('nodes', 'outer_lands'):
                raise ValueError('topic 只能是 nodes 或 outer_lands')
            return world_records.format_thaumcraft_world(
                server['prefix'], world_records.thaumcraft(server), topic, args.get('node_type') or '',
                args.get('aspect') or '', detailed=privileged,
                names=world_records.aspect_names(Path(mirror) / 'mods' if mirror else None))
        except (OSError, subprocess.SubprocessError) as exc:
            return prefix + f'服务器主机暂时连不上（{type(exc).__name__}），稍后再查。'
    if name == 'machine_profile':
        if not privileged:
            return '拒绝：机器耗时采样只有管理员能开。'
        return _profile(server, args, ops_root)
    if not mirror:
        return prefix + '这台服务器没有本机同步的配置副本。'
    if name == 'search_server_config':
        return prefix + server_insight.search_config(mirror, args.get('query') or '',
                                                     scope=args.get('scope') or '')
    if name == 'read_server_config':
        return prefix + server_insight.read_config(mirror, args.get('path') or '',
                                                   start_line=args.get('start_line') or 1)
    raise ValueError('未知服务器查询工具')


def _world(server, args, ops_root, mirror, privileged):
    """Chunks, entities or lag sources from fixed read-only commands; coordinates for admins only."""
    prefix = server['prefix']

    def query(command):
        return server_registry.query_server(server, command)
    topic = str(args.get('topic') or 'chunks')
    if topic == 'chunks':
        data = world_status.collect_chunks(query, ops_root, Path(mirror or '.') / 'server.properties')
        return world_status.format_chunks(prefix, data, detailed=privileged)
    def optional(command):
        # Sources added later (tick percentiles, the entity limiter) must not break older answers.
        try:
            return str(query(command) or '')
        except Exception:
            return ''
    if topic == 'entities':
        entity = str(args.get('entity') or '').strip().strip('"').lower()
        entity = ENTITY_ALIASES.get(entity, entity)
        if entity and not re.fullmatch(r'[a-z0-9_.-]+:[a-z0-9_./-]+', entity):
            return prefix + ' 实体类型要写完整 ID，例如 minecraft:item（掉落物）、minecraft:experience_orb（经验球）。'
        filtered = query(f'neoforge entity list "{entity}"') if entity else ''
        return world_status.format_entities(prefix, query('neoforge entity list'), filtered, entity,
                                            detailed=privileged, limiter_text=optional('chesirtab status'))
    if topic == 'lag':
        try:
            host = _host(server)
        except Exception:
            host = None           # the RCON numbers still answer the question
        return world_status.format_lag(prefix, query('neoforge tps'), query('mio_icif perf'),
                                       query('neoforge entity list'), query('statistics block-entities byType'),
                                       query('servercore status'), host, server_insight.item_names(ops_root),
                                       tick_text=optional('tick query'))
    if topic == 'machines':
        return world_status.format_machines(prefix, query('statistics block-entities byType'),
                                            query('statistics block-entities byType 2'),
                                            query('statistics block-entities byPlayer'),
                                            query('statistics entities byPlayer'), server_insight.item_names(ops_root))
    if topic == 'host':
        if not server.get('remote_ssh_host'):
            return prefix + ' 这台服务器不在远端主机上，暂时查不了进程内存和 CPU。'
        try:
            return world_status.format_host(prefix, _host(server))
        except Exception as exc:
            return prefix + f' 服务器主机暂时连不上（{type(exc).__name__}），稍后再查。'
    raise ValueError('topic 只能是 ' + '、'.join(WORLD_TOPICS))


def _profile(server, args, ops_root):
    """Admin-only: time block entities or entities for a few seconds with NeoForge's tracker.

    Tracking adds a little overhead while it runs and switches itself off afterwards.
    """
    kind = str(args.get('kind') or 'blockentity')
    if kind not in world_status.PROFILE_KINDS:
        raise ValueError('kind 只能是 blockentity（方块实体）或 entity（实体）')
    try:
        seconds = max(5, min(30, int(args.get('seconds') or 20)))
    except (TypeError, ValueError):
        seconds = 20
    if not _PROFILE_LOCK.acquire(blocking=False):
        return server['prefix'] + ' 已经有一次采样在进行，等它结束再查。'
    try:
        last = _PROFILE_LAST.get((server['id'], kind))
        if last and time.time() - last[0] < PROFILE_COOLDOWN:
            minutes = max(1, int((time.time() - last[0]) // 60))
            return last[1] + f'\n（这是约 {minutes} 分钟前的采样结果；5 分钟内不重复采样。）'
        started = server_registry.query_server(server, f'neoforge track start {kind} {seconds}')
        if 'enabled' not in str(started).lower():
            return server['prefix'] + f' 采样没有启动：{str(started).strip()[:120] or "无输出"}'
        print(f'{server["prefix"]} [机器采样] {kind} {seconds} 秒', flush=True)
        _sleep(seconds + 2)
        text = server_registry.query_server(server, f'neoforge track {kind}')
        try:
            tick = server_registry.query_server(server, 'tick query')
        except Exception:
            tick = ''
        result = world_status.format_profile(server['prefix'], kind, text, seconds, tick,
                                             world_records.item_names(ops_root))
        if world_status.parse_track(text):
            _PROFILE_LAST[(server['id'], kind)] = (time.time(), result)
        return result
    finally:
        _PROFILE_LOCK.release()


def _host(server):
    """Remote host numbers, reused for HOST_CACHE_SECONDS so a burst of questions costs one SSH call."""
    if not server.get('remote_ssh_host'):
        return None
    cached = _HOST_CACHE.get(server['id'])
    if cached and time.time() - cached[0] < HOST_CACHE_SECONDS:
        return cached[1]
    data = remote_server_reader.host_status(server)
    _HOST_CACHE[server['id']] = (time.time(), data)
    return data


def _remote_read(name, args, server):
    """Remote servers: answer from the mirrored index and recorded events only."""
    prefix = server['prefix']
    telemetry = OpsTelemetry(server['id'], prefix, server.get('server_dir') or '.',
                             state_base=os.environ.get('OPS_STATE_BASE'), remote=True)
    if name == 'list_mods':
        data = telemetry.inventory()
        if not data:
            return prefix + ' 模组清单还在从远端主机同步，稍后再查。'
        when = time.strftime('%m-%d %H:%M', time.localtime(float(data.get('generated_at') or 0)))
        return format_inventory(data) + f'\n（清单同步自远端主机，生成于 {when}）'
    if name == 'incident_postmortem':
        window = str(args.get('window') or '24h')
        if window not in {'1h', '6h', '24h', '7d'}:
            raise ValueError('window 只能是 1h、6h、24h 或 7d')
        return telemetry.format_postmortem(telemetry.incident_postmortem(window))
    if name == 'read_server_log':
        lines = remote_server_reader.tail_log(server, args.get('lines') or 150)
        return (prefix + f' 远端最新日志末尾 {len(lines)} 行（只读，IP 与敏感值已遮挡）：\n'
                + '\n'.join(lines))
    if name == 'read_crash_report':
        report = remote_server_reader.crash_report(server, args.get('name') or '', kind=args.get('kind') or '')
        if not report['name']:
            return prefix + ' 远端没有崩溃报告。'
        return (prefix + f' 远端崩溃报告 {report["name"]}（前 {len(report["lines"])} 行）：\n'
                + '\n'.join(report['lines'])
                + '\n最近的报告：' + '、'.join(report['names'][-5:]))
    return (prefix + ' 这项远端读取暂不支持；已记录的崩溃和错误可用 !时间线、!错误摘要 查看。')


def execute_local_read(name, args, selected, privileged):
    if name != 'list_mods' and not privileged:
        return '拒绝：仅管理员可以读取运维日志与复盘。'
    if isinstance(selected, dict) and selected.get('id'):
        server = server_registry.resolve_server(selected['id'])
        if server and server['id'] == selected['id'] and server.get('remote_ssh_host'):
            return _remote_read(name, args, server)
    server, root = canonical_local_server(selected)
    prefix = server['prefix']
    if name == 'read_server_log':
        count = min(400, max(1, int(args.get('lines') or 150)))
        return prefix + ' 最近日志（最多 1 MiB）：\n' + tail(inside(root, 'logs/latest.log'), lines=count)
    if name == 'read_crash_report':
        directory = inside(root, 'crash-reports')
        paths = [p for p in directory.glob('*.txt') if p.resolve().is_relative_to(root)]
        if not paths:
            return prefix + ' 没有已记录的崩溃报告。'
        newest = max(paths, key=lambda p: p.stat().st_mtime)
        return prefix + ' 最新崩溃报告 ' + newest.name + '：\n' + tail(newest, lines=160)
    if name == 'list_mods':
        inside(root, 'mods')
        return format_inventory(scan_installed_mods(root, server['id'], prefix))
    if name == 'incident_postmortem':
        window = str(args.get('window') or '24h')
        if window not in {'1h', '6h', '24h', '7d'}:
            raise ValueError('window 只能是 1h、6h、24h 或 7d')
        inside(root, 'crash-reports')
        telemetry = OpsTelemetry(server['id'], prefix, root,
                                 state_base=os.environ.get('OPS_STATE_BASE'))
        return telemetry.format_postmortem(telemetry.incident_postmortem(window))
    raise ValueError('未知本地只读工具')
