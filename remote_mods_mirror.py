"""Keep a local copy of a remote POSIX server's mods/*.jar for the mod and recipe indexes.

Read-only on the remote host: one SSH call lists name, size and mtime; changed
jars are streamed back as one tar over SSH and written atomically. Local jars
that vanished remotely are deleted from the mirror. Only *.jar files directly
inside the mirror's mods directory are ever touched.
"""
import base64
import json
import os
import re
import shlex
import subprocess
import tarfile
import threading
import time
from pathlib import Path

_LIST_SCRIPT = r"""
import base64, json, os
cfg = json.loads(base64.b64decode('__CONFIG__'))
rows = []
for name in os.listdir(cfg['mods']):
    path = os.path.join(cfg['mods'], name)
    if name.endswith('.jar') and os.path.isfile(path):
        stat = os.stat(path)
        rows.append([name, stat.st_size, int(stat.st_mtime)])
print(json.dumps(rows))
"""

_TAR_SCRIPT = r"""
import base64, json, os, sys, tarfile
cfg = json.loads(base64.b64decode('__CONFIG__'))
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as tar:
    for name in cfg['names']:
        path = os.path.join(cfg['mods'], name)
        if os.path.basename(name) == name and name.endswith('.jar') and os.path.isfile(path):
            tar.add(path, arcname=name, recursive=False)
"""

_FILE_SCRIPT = r"""
import base64, json, os
cfg = json.loads(base64.b64decode('__CONFIG__'))
path, data = cfg['path'], b''
exists = os.path.isfile(path) and os.path.getsize(path) <= cfg['limit']
if exists:
    with open(path, 'rb') as stream:
        data = stream.read(cfg['limit'] + 1)
print(json.dumps({'exists': exists, 'data': base64.b64encode(data).decode('ascii')}))
"""

# Characters Windows cannot store in a file name.
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_LOCK = threading.Lock()


def _ssh_command(server, script, config):
    host = str(server['remote_ssh_host']).strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', host):
        raise ValueError('invalid SSH alias')
    if str(server.get('remote_os') or '').casefold() != 'posix':
        raise ValueError('mods mirror supports POSIX hosts only')
    body = script.replace('__CONFIG__', base64.b64encode(json.dumps(config).encode('utf-8')).decode('ascii'))
    python = str(server.get('remote_python') or 'python3')
    ssh = (os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'OpenSSH', 'ssh.exe')
           if os.name == 'nt' else 'ssh')
    return [ssh, '-T', '-n', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=8', host, shlex.quote(python) + ' -c ' + shlex.quote(body)]


def _remote_mods(server):
    return str(server['remote_server_dir']).rstrip('/') + '/mods'


def remote_listing(server, *, runner=None):
    """{name: (size, mtime)} for remote mods/*.jar."""
    argv = _ssh_command(server, _LIST_SCRIPT, {'mods': _remote_mods(server)})
    result = (runner or subprocess.run)(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=40, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise OSError('SSH exit %s' % result.returncode)
    return {str(name): (int(size), int(mtime))
            for name, size, mtime in json.loads(result.stdout.decode('utf-8-sig'))}


def remote_file(server, relative, *, runner=None, limit=64 * 1024):
    """Bytes of one small file under the remote server dir, or None if absent."""
    if os.path.basename(relative) != relative:
        raise ValueError('top-level file names only')
    path = str(server['remote_server_dir']).rstrip('/') + '/' + relative
    argv = _ssh_command(server, _FILE_SCRIPT, {'path': path, 'limit': int(limit)})
    result = (runner or subprocess.run)(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise OSError('SSH exit %s' % result.returncode)
    payload = json.loads(result.stdout.decode('utf-8-sig'))
    return base64.b64decode(payload['data']) if payload.get('exists') else None


def _sync_properties(server, mirror_root, runner=None):
    """Mirror server.properties; returns 'updated', 'same', 'missing' or 'failed'."""
    try:
        data = remote_file(server, 'server.properties', runner=runner)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return 'failed'
    target = Path(mirror_root) / 'server.properties'
    if data is None:
        return 'missing'
    if target.is_file() and target.read_bytes() == data:
        return 'same'
    temp = target.with_name(target.name + '.part')
    temp.write_bytes(data)
    os.replace(temp, target)
    return 'updated'


def local_listing(mods_dir):
    rows = {}
    for path in Path(mods_dir).glob('*.jar'):
        if path.is_file():
            stat = path.stat()
            rows[path.name] = (stat.st_size, int(stat.st_mtime))
    return rows


def plan(remote, local):
    """(to_fetch, to_delete, skipped_names)."""
    skipped = sorted(name for name in remote if _BAD_NAME.search(name))
    wanted = {name: meta for name, meta in remote.items() if name not in skipped}
    fetch = sorted(name for name, meta in wanted.items() if local.get(name) != meta)
    delete = sorted(name for name in local if name not in wanted)
    return fetch, delete, skipped


def _fetch(server, mods_dir, names, remote, *, popen=None, timeout=1800):
    argv = _ssh_command(server, _TAR_SCRIPT, {'mods': _remote_mods(server), 'names': names})
    process = (popen or subprocess.Popen)(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    timer = threading.Timer(timeout, process.kill)
    timer.daemon = True
    timer.start()
    wanted, written = set(names), []
    try:
        with tarfile.open(fileobj=process.stdout, mode='r|') as tar:
            for member in tar:
                name = member.name
                if (not member.isfile() or name not in wanted or os.path.basename(name) != name
                        or _BAD_NAME.search(name)):
                    continue
                source = tar.extractfile(member)
                target = Path(mods_dir) / name
                temp = target.with_name(target.name + '.part')
                with temp.open('wb') as stream:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        stream.write(chunk)
                size, mtime = remote[name]
                if temp.stat().st_size != size:
                    temp.unlink(missing_ok=True)
                    continue
                os.utime(temp, (mtime, mtime))
                os.replace(temp, target)
                written.append(name)
    finally:
        timer.cancel()
        process.wait(timeout=30)
    return written


# ----- text trees (config, scripts): read-only copies for lookup tools -----
TREE_SUFFIXES = ('.toml', '.json', '.json5', '.snbt', '.cfg', '.conf', '.properties', '.js', '.ts',
                 '.txt', '.yml', '.yaml', '.ini', '.zs', '.md', '.lang')
TREE_MAX_FILE = 2 * 1024 * 1024

_TREE_LIST_SCRIPT = r"""
import base64, json, os
cfg = json.loads(base64.b64decode('__CONFIG__'))
base = os.path.realpath(cfg['base'])
rows = []
if os.path.isdir(base):
    for folder, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for name in files:
            path = os.path.join(folder, name)
            if not name.lower().endswith(tuple(cfg['suffixes'])) or not os.path.isfile(path):
                continue
            stat = os.stat(path)
            if stat.st_size <= cfg['max_file']:
                rows.append([os.path.relpath(path, base).replace(os.sep, '/'), stat.st_size, int(stat.st_mtime)])
print(json.dumps({'exists': os.path.isdir(base), 'rows': rows}))
"""

_TREE_TAR_SCRIPT = r"""
import base64, json, os, sys, tarfile
cfg = json.loads(base64.b64decode('__CONFIG__'))
base = os.path.realpath(cfg['base'])
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as tar:
    for name in cfg['names']:
        path = os.path.realpath(os.path.join(base, name))
        if path.startswith(base + os.sep) and os.path.isfile(path):
            tar.add(path, arcname=name, recursive=False)
"""


def _safe_relative(name):
    parts = str(name).split('/')
    return (bool(parts) and all(part not in ('', '.', '..') and not _BAD_NAME.search(part)
                                for part in parts) and not str(name).startswith('/'))


def _tree_listing(root):
    rows = {}
    root = Path(root)
    if root.is_dir():
        for path in root.rglob('*'):
            if path.is_file() and not path.name.endswith('.part'):
                stat = path.stat()
                rows[path.relative_to(root).as_posix()] = (stat.st_size, int(stat.st_mtime))
    return rows


def sync_tree(server, relative, mirror_root, *, runner=None, popen=None):
    """Mirror remote <server>/<relative> text files into <mirror_root>/<relative>."""
    if not _safe_relative(relative):
        raise ValueError('unsafe tree path')
    base = str(server['remote_server_dir']).rstrip('/') + '/' + relative
    argv = _ssh_command(server, _TREE_LIST_SCRIPT, {'base': base, 'suffixes': list(TREE_SUFFIXES),
                                                     'max_file': TREE_MAX_FILE})
    result = (runner or subprocess.run)(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise OSError('SSH exit %s' % result.returncode)
    payload = json.loads(result.stdout.decode('utf-8-sig'))
    remote = {str(name): (int(size), int(mtime)) for name, size, mtime in payload.get('rows') or []
              if _safe_relative(name)}
    local_root = Path(mirror_root) / relative
    local_root.mkdir(parents=True, exist_ok=True)
    local = _tree_listing(local_root)
    fetch = sorted(name for name, meta in remote.items() if local.get(name) != meta)
    delete = sorted(name for name in local if name not in remote)
    written = []
    if fetch:
        argv = _ssh_command(server, _TREE_TAR_SCRIPT, {'base': base, 'names': fetch})
        process = (popen or subprocess.Popen)(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        timer = threading.Timer(600, process.kill)
        timer.daemon = True
        timer.start()
        wanted = set(fetch)
        try:
            with tarfile.open(fileobj=process.stdout, mode='r|') as tar:
                for member in tar:
                    name = member.name
                    if not member.isfile() or name not in wanted or not _safe_relative(name):
                        continue
                    target = local_root.joinpath(*name.split('/'))
                    if not target.resolve().is_relative_to(local_root.resolve()):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temp = target.with_name(target.name + '.part')
                    with temp.open('wb') as stream:
                        stream.write(tar.extractfile(member).read(TREE_MAX_FILE + 1))
                    size, mtime = remote[name]
                    if temp.stat().st_size != size:
                        temp.unlink(missing_ok=True)
                        continue
                    os.utime(temp, (mtime, mtime))
                    os.replace(temp, target)
                    written.append(name)
        finally:
            timer.cancel()
            process.wait(timeout=30)
    for name in delete:
        local_root.joinpath(*name.split('/')).unlink(missing_ok=True)
    return {'tree': relative, 'exists': bool(payload.get('exists')), 'remote': len(remote),
            'fetched': len(written), 'deleted': len(delete),
            'missing': len(set(fetch) - set(written))}


def sync(server, mirror_root, *, runner=None, popen=None):
    """Bring mirror_root/mods in line with the remote; returns a summary dict."""
    with _LOCK:
        mods_dir = Path(mirror_root) / 'mods'
        mods_dir.mkdir(parents=True, exist_ok=True)
        for stale in mods_dir.glob('*.jar.part'):
            stale.unlink(missing_ok=True)
        remote = remote_listing(server, runner=runner)
        fetch, delete, skipped = plan(remote, local_listing(mods_dir))
        written = _fetch(server, mods_dir, fetch, remote, popen=popen) if fetch else []
        for name in delete:
            (mods_dir / name).unlink(missing_ok=True)
        missing = sorted(set(fetch) - set(written))
        properties = _sync_properties(server, mirror_root, runner=runner)
        return {'remote': len(remote), 'fetched': written, 'deleted': delete,
                'properties': properties,
                'skipped': skipped, 'missing': missing,
                'changed': bool(written or delete), 'complete': not missing,
                'finished_at': time.time()}
