"""Read-only crash-report watch for a remote POSIX Minecraft server over SSH.

One SSH call lists crash-reports/*.txt and the JVM's hs_err_pid*.log, and returns
a short summary (description, root cause, block or entity, location) for files
newer than the caller's cursor. Nothing on the remote host is written.
"""
import base64
import json
import logging
import os
import posixpath
import re
import shlex
import subprocess
import time

_LOGGER = logging.getLogger(__name__)
_last_failure_notice = float('-inf')
HISTORY_SECONDS = 7 * 86400
MAX_DETAILS = 20
MAX_SEEN = 400

_POSIX_SCRIPT = r"""
import base64, json, os, re
cfg = json.loads(base64.b64decode('__CONFIG__'))
root, since, limit = cfg['root'], float(cfg['since']), int(cfg['limit'])
files = []
crash_dir = os.path.join(root, 'crash-reports')
if os.path.isdir(crash_dir):
    for name in os.listdir(crash_dir):
        path = os.path.join(crash_dir, name)
        if name.endswith('.txt') and os.path.isfile(path):
            files.append(['crash', name, os.path.getmtime(path), path])
for name in os.listdir(root):
    path = os.path.join(root, name)
    if name.startswith('hs_err_pid') and name.endswith('.log') and os.path.isfile(path):
        files.append(['jvm', name, os.path.getmtime(path), path])

def summary(kind, path):
    info = {'description': '', 'cause': '', 'subject': '', 'location': ''}
    with open(path, encoding='utf-8', errors='replace') as stream:
        lines = [stream.readline() for _ in range(900)]
    previous = ''
    for raw in lines:
        line = raw.strip()
        if kind == 'jvm':
            if not info['description'] and re.match(r'#\s+(SIG\w+|EXCEPTION_\w+|Out of Memory)', line):
                info['description'] = line.lstrip('# ').strip()[:160]
            if previous.startswith('# Problematic frame') and not info['cause']:
                info['cause'] = line.lstrip('# ').strip()[:200]
        else:
            if line.startswith('Description:') and not info['description']:
                info['description'] = line.split(':', 1)[1].strip()[:160]
            if not info['cause'] and (line.startswith('Caused by:') or re.match(
                    r'(?:java|net|com|org)\.[\w.$]+(?:Exception|Error)(?::|$)', line)):
                info['cause'] = line[:220]
            if not info['subject'] and re.match(r'(Block|Entity Type|Block entity type):', line):
                info['subject'] = line[:120]
            if not info['location'] and re.match(r'(Block location|Entity.s Exact location):', line):
                found = re.search(r'\(([-\d., ]+)\)', line)
                info['location'] = found.group(1)[:60] if found else line[:80]
        previous = line
    return info

files.sort(key=lambda row: row[2])
details = {}
for kind, name, mtime, path in [row for row in files if row[2] > since][-limit:]:
    try:
        details[name] = summary(kind, path)
    except OSError as exc:
        details[name] = {'description': 'unreadable: ' + type(exc).__name__}
print(json.dumps({'files': [[k, n, m] for k, n, m, _ in files], 'details': details}))
"""


def _failed(state, reason):
    global _last_failure_notice
    now = time.monotonic()
    if now - _last_failure_notice >= 300:
        _LOGGER.warning('Remote crash watch failed (%s); cursor preserved, retrying.', reason)
        _last_failure_notice = now
    return [], state, False


def source_id(server):
    return (str(server['remote_ssh_host']).strip().casefold() + '|' +
            posixpath.normpath(str(server['remote_server_dir'])))


def read_remote_crashes(server, state, *, runner=None, now=None, history_seconds=HISTORY_SECONDS):
    """Return (reports, next_state, ok).

    reports: dicts with kind ('crash' or 'jvm'), name, mtime, historic and the
    summary fields. On first attach (or a new source) files from the last
    history_seconds come back marked historic, so they can be counted without
    being announced as new. Errors leave the cursor untouched.
    """
    original = state
    now = float(time.time() if now is None else now)
    try:
        host = str(server['remote_ssh_host']).strip()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', host):
            raise ValueError('invalid SSH alias')
        if str(server.get('remote_os') or '').casefold() != 'posix':
            raise ValueError('crash watch supports POSIX hosts only')
        source = source_id(server)
        state = state if isinstance(state, dict) else {}
        initial = state.get('source') != source
        seen = {} if initial else dict(state.get('seen') or {})
        since = now - history_seconds if initial else float(state.get('since', now - history_seconds))
        config = {'root': str(server['remote_server_dir']), 'since': since - 1, 'limit': MAX_DETAILS}
        script = _POSIX_SCRIPT.replace(
            '__CONFIG__', base64.b64encode(json.dumps(config).encode('utf-8')).decode('ascii'))
        python = str(server.get('remote_python') or 'python3')
        ssh = (os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'OpenSSH', 'ssh.exe')
               if os.name == 'nt' else 'ssh')
        result = (runner or subprocess.run)(
            [ssh, '-T', '-n', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
             '-o', 'ConnectTimeout=8', host, shlex.quote(python) + ' -c ' + shlex.quote(script)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            return _failed(original, 'SSH exit ' + str(result.returncode))
        payload = json.loads(result.stdout.decode('utf-8-sig'))
        files = [(str(kind), str(name), float(mtime)) for kind, name, mtime in payload['files']]
        details = payload.get('details') or {}
        reports = []
        for kind, name, mtime in files:
            if name in seen:
                continue
            seen[name] = mtime
            historic = initial
            if initial and mtime < now - history_seconds:
                continue  # older history: remembered as seen, not reported
            info = details.get(name) or {}
            reports.append({'kind': kind, 'name': name, 'mtime': mtime, 'historic': historic,
                            'description': str(info.get('description') or '')[:160],
                            'cause': str(info.get('cause') or '')[:220],
                            'subject': str(info.get('subject') or '')[:120],
                            'location': str(info.get('location') or '')[:60]})
        newest = max([mtime for _, _, mtime in files] + [since])
        if len(seen) > MAX_SEEN:
            seen = dict(sorted(seen.items(), key=lambda item: item[1])[-MAX_SEEN:])
        return reports, {'source': source, 'since': newest, 'seen': seen}, True
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return _failed(original, type(exc).__name__)


def render(prefix, report, tz=None):
    """One line for events and admin notices; no raw stack trace."""
    from datetime import datetime, timedelta, timezone
    when = datetime.fromtimestamp(report['mtime'], tz or timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
    label = 'JVM 致命错误' if report['kind'] == 'jvm' else '崩溃'
    parts = [report.get('description'), report.get('subject'),
             ('位置 ' + report['location']) if report.get('location') else '', report.get('cause')]
    body = '｜'.join(part for part in parts if part) or '摘要不可读'
    return f'{prefix} {label} {when}｜{report["name"]}｜{body}'[:600]
