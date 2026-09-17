"""Read-only AI operations bound to an enabled canonical local server."""
import os
from pathlib import Path
import re

import server_registry
from mod_inventory import scan_installed_mods, format_inventory
from ops_telemetry import OpsTelemetry

LOCAL_READ_TOOLS = frozenset({'read_server_log', 'read_crash_report', 'list_mods', 'incident_postmortem'})
DISABLED_FILE_TOOLS = frozenset({'list_dir', 'read_file', 'search_files', 'read_nbt',
                                 'set_server_property', 'replace_in_config'})


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


def execute_local_read(name, args, selected, privileged):
    if name != 'list_mods' and not privileged:
        return '拒绝：仅管理员可以读取运维日志与复盘。'
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
