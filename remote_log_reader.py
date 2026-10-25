"""Bounded, read-only SSH tail for Windows and POSIX Minecraft servers.

The cursor belongs to a host/path pair. A new source starts at EOF; later file
rotation starts at byte zero. Bytes are decoded only after a complete newline.
"""
import base64
import hashlib
import json
import logging
import ntpath
import os
import posixpath
import re
import shlex
import subprocess
import time

_LOGGER = logging.getLogger(__name__)
_last_failure_notice = float('-inf')


def _failed(state, reason):
    global _last_failure_notice
    now = time.monotonic()
    if now - _last_failure_notice >= 60:
        _LOGGER.warning('Remote log read failed (%s); cursor preserved, retrying.', reason)
        _last_failure_notice = now
    return [], state, False


_SCRIPT = r"""
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$cfg=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__CONFIG__')) | ConvertFrom-Json
$stream=[IO.File]::Open($cfg.path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
try {
    $size=$stream.Length
    $identity=(Get-Item -LiteralPath $cfg.path).CreationTimeUtc.Ticks.ToString()
    function HeadHash([int]$count) {
        $stream.Position=0
        $bytes=[byte[]]::new($count)
        $used=0
        while($used -lt $count) {
            $n=$stream.Read($bytes,$used,$count-$used)
            if($n -eq 0){throw 'log changed during read'}
            $used+=$n
        }
        $sha=[Security.Cryptography.SHA256]::Create()
        try {return [Convert]::ToBase64String($sha.ComputeHash($bytes))} finally {$sha.Dispose()}
    }
    $offset=[long]$cfg.offset
    $discard=[bool]$cfg.discard_partial
    $reset=$false
    if($cfg.initialize) {
        $offset=$size
        $discard=$false
        if($size -gt 0){$stream.Position=$size-1;$discard=$stream.ReadByte() -ne 10}
    } else {
        $reset=($identity -ne $cfg.identity -or $offset -gt $size)
        if(-not $reset -and $cfg.head_size -gt 0){
            $reset=(HeadHash ([int]$cfg.head_size)) -ne $cfg.head
        }
        if($reset){$offset=0;$discard=$false}
    }
    $count=if($cfg.initialize){0}else{[int][Math]::Min($cfg.max_bytes,$size-$offset)}
    $bytes=[byte[]]::new($count)
    $stream.Position=$offset
    $used=0
    while($used -lt $count){
        $n=$stream.Read($bytes,$used,$count-$used)
        if($n -eq 0){throw 'log changed during read'}
        $used+=$n
    }
    # The caller anchors the prefix up to its newly committed complete line.
    $headBytes=[byte[]]::new([int][Math]::Min(128,$size))
    $stream.Position=0
    $used=0
    while($used -lt $headBytes.Length){
        $n=$stream.Read($headBytes,$used,$headBytes.Length-$used)
        if($n -eq 0){throw 'log changed during read'}
        $used+=$n
    }
    [ordered]@{identity=$identity;offset=$offset;size=$size;
        head_data=[Convert]::ToBase64String($headBytes);
        discard_partial=$discard;data=[Convert]::ToBase64String($bytes)} |
        ConvertTo-Json -Compress
} finally {$stream.Dispose()}
"""


_POSIX_SCRIPT = r"""
import base64, hashlib, json, os
cfg = json.loads(base64.b64decode('__CONFIG__'))
with open(cfg['path'], 'rb') as stream:
    stat = os.fstat(stream.fileno())
    size = stat.st_size
    identity = str(stat.st_dev) + ':' + str(stat.st_ino)
    def read_exact(count):
        data = stream.read(count)
        if len(data) != count:
            raise OSError('log changed during read')
        return data
    def head_hash(count):
        stream.seek(0)
        return base64.b64encode(hashlib.sha256(read_exact(count)).digest()).decode('ascii')
    offset = int(cfg['offset'])
    discard = bool(cfg['discard_partial'])
    if cfg['initialize']:
        offset = size
        discard = False
        if size:
            stream.seek(size - 1)
            discard = read_exact(1) != b'\n'
    else:
        reset = identity != cfg['identity'] or offset > size
        if not reset and cfg['head_size'] > 0:
            reset = head_hash(cfg['head_size']) != cfg['head']
        if reset:
            offset, discard = 0, False
    count = 0 if cfg['initialize'] else min(cfg['max_bytes'], size - offset)
    stream.seek(offset)
    data = read_exact(count)
    stream.seek(0)
    head_data = read_exact(min(128, size))
    print(json.dumps(dict(identity=identity, offset=offset, size=size,
        head_data=base64.b64encode(head_data).decode('ascii'),
        discard_partial=discard, data=base64.b64encode(data).decode('ascii'))))
"""


def _log_path(server):
    remote_os = str(server.get('remote_os') or 'windows').casefold()
    if remote_os not in ('windows', 'posix'):
        raise ValueError('unsupported remote OS')
    paths = posixpath if remote_os == 'posix' else ntpath
    return paths.join(server['remote_server_dir'], 'logs', 'latest.log'), remote_os


def source_id(server):
    host = str(server['remote_ssh_host']).strip()
    path, remote_os = _log_path(server)
    # Preserve Windows cursor IDs. A new POSIX source starts at EOF, and its
    # case-sensitive path must not be folded like a Windows path.
    normalized = posixpath.normpath(path) if remote_os == 'posix' else ntpath.normcase(path)
    return host.casefold() + '|' + normalized


def read_remote_lines(server, state, *, runner=None, max_bytes=1024 * 1024):
    """Return (complete lines, next cursor, ok); errors leave the cursor intact."""
    original = state
    try:
        host = str(server['remote_ssh_host']).strip()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', host):
            raise ValueError('invalid SSH alias')
        source = source_id(server)
        state = state or {}
        initialize = state.get('source') != source
        limit = max(4096, min(int(max_bytes), 1024 * 1024))
        path, remote_os = _log_path(server)
        config = {
            'path': path,
            'initialize': initialize, 'offset': max(0, int(state.get('offset', 0))),
            'identity': state.get('identity'), 'head': state.get('head'),
            'head_size': int(state.get('head_size', 0)),
            'discard_partial': bool(state.get('discard_partial', False)),
            'max_bytes': limit,
        }
        encoded_config = base64.b64encode(json.dumps(config).encode('utf-8')).decode('ascii')
        if remote_os == 'posix':
            script = _POSIX_SCRIPT.replace('__CONFIG__', encoded_config)
            python = str(server.get('remote_python') or 'python3')
            command = [shlex.quote(python) + ' -c ' + shlex.quote(script)]
        else:
            script = _SCRIPT.replace('__CONFIG__', encoded_config)
            encoded_script = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
            command = ['powershell.exe', '-NoProfile', '-NonInteractive',
                       '-EncodedCommand', encoded_script]
        ssh = (os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                           'System32', 'OpenSSH', 'ssh.exe') if os.name == 'nt' else 'ssh')
        result = (runner or subprocess.run)(
            [ssh, '-T', '-n', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
             '-o', 'ConnectTimeout=8', host, *command],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            return _failed(original, 'SSH exit ' + str(result.returncode))
        payload = json.loads(result.stdout.decode('utf-8-sig'))
        raw = base64.b64decode(payload['data'], validate=True)
        offset, size = int(payload['offset']), int(payload['size'])
        if offset < 0 or offset + len(raw) > size or len(raw) > limit:
            raise ValueError('invalid remote log range')
        discard = bool(payload['discard_partial'])
        consumed = 0
        if discard:
            first = raw.find(b'\n')
            consumed = len(raw) if first < 0 else first + 1
            raw = raw[consumed:]
            discard = first < 0
        last = raw.rfind(b'\n')
        complete = raw[:last + 1]
        consumed += len(complete)
        # Drop an oversized unfinished record rather than forward a split UTF-8
        # sequence or reread the same bounded chunk forever.
        if last < 0 and len(raw) == limit:
            consumed += len(raw)
            discard = True
        committed = offset + consumed
        head_size = min(128, committed)
        head_data = base64.b64decode(payload['head_data'], validate=True)
        if len(head_data) < head_size:
            raise ValueError('incomplete remote prefix')
        next_state = {
            'source': source, 'identity': str(payload['identity']),
            'offset': committed, 'head_size': head_size,
            'head': base64.b64encode(hashlib.sha256(head_data[:head_size]).digest()).decode('ascii'),
            'discard_partial': discard,
        }
        return complete.decode('utf-8', errors='replace').splitlines(), next_state, True
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return _failed(original, type(exc).__name__)
