"""Explicit OneBot media retrieval and bounded local conversion; no model configuration."""
import base64
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.parse
import urllib.request
import wave

from media_segments import media_segments
from request_runtime import remaining_timeout, check_active

LLBOT_BIN = Path(os.environ.get('LLBOT_BIN', str(Path(__file__).parent / 'bin' / 'llbot')))
AUDIO_LIMIT = 32 * 1024 * 1024
VIDEO_LIMIT = 64 * 1024 * 1024
OMNI_LIMIT = 9_500_000


def read_bounded(stream, limit):
    chunks, total = [], 0
    while True:
        check_active()
        part = stream.read(min(65536, limit + 1 - total))
        if not part:
            return b''.join(chunks)
        chunks.append(part)
        total += len(part)
        if total > limit:
            raise ValueError('媒体或响应超过大小上限')


def public_url(url):
    check_active()
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('媒体地址格式不允许')
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('媒体地址不能指向本机或内网')
    return str(url)


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, public_url(newurl))


def download(url, limit=VIDEO_LIMIT):
    req = urllib.request.Request(public_url(url), headers={'User-Agent': 'SanaeMedia/2.0'})
    with urllib.request.build_opener(PublicRedirect()).open(req, timeout=remaining_timeout(30)) as response:
        return read_bounded(response, limit), response.headers.get('Content-Type', 'application/octet-stream')


def onebot(api, action, payload):
    check_active()
    request = urllib.request.Request(api.rstrip('/') + '/' + action,
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=remaining_timeout(15)) as response:
        body = json.loads(read_bounded(response, 2 * 1024 * 1024))
    if body.get('status') != 'ok' or body.get('retcode') != 0:
        raise ValueError('OneBot 无法取得这条媒体')
    return body.get('data') or {}


def resolve_segments(event, api, *, max_depth=3, max_nodes=100):
    result, seen, visited = [], set(), 0
    def walk(value, depth):
        nonlocal visited
        if depth > max_depth or visited >= max_nodes or len(result) >= 8:
            return
        if isinstance(value, list):
            value = {'message': value}
        for segment in media_segments(value, ('record', 'audio', 'video', 'reply', 'forward', 'file')):
            visited += 1
            if visited > max_nodes:
                return
            kind = segment.get('type')
            if kind in {'reply', 'forward'}:
                ident = segment.get('id') or segment.get('message_id')
                key = (kind, ident)
                if not ident or key in seen or depth >= max_depth:
                    continue
                seen.add(key)
                try:
                    data = onebot(api, 'get_msg' if kind == 'reply' else 'get_forward_msg',
                                  {'message_id': ident} if kind == 'reply' else {'id': ident})
                except Exception:
                    continue
                if kind == 'reply':
                    walk(data, depth + 1)
                else:
                    for node in (data.get('messages') or [])[:max_nodes-visited]:
                        walk(node.get('content') or node.get('message') or [], depth + 1)
            else:
                if kind == 'file':
                    name = str(segment.get('file') or segment.get('name') or '').lower()
                    if not re.search(r'\.(wav|mp3|amr|silk|ogg|opus|flac|m4a|mp4|webm)$', name):
                        continue
                    segment = {**segment, 'type': 'video' if name.endswith(('.mp4', '.webm')) else 'audio'}
                key = (segment.get('type'), segment.get('url') or segment.get('file'))
                if key not in seen:
                    seen.add(key)
                    result.append(segment)
    walk(event, 0)
    return result


def sniff(data):
    if data.startswith(b'#!SILK_V3') or data.startswith(b'\x02#!SILK_V3'):
        return 'silk', 'audio/silk'
    if data[:4] == b'RIFF' and data[8:12] == b'WAVE': return 'wav', 'audio/wav'
    if data.startswith(b'#!AMR'): return 'amr', 'audio/amr'
    if data.startswith(b'ID3') or (len(data) > 1 and data[0] == 255 and data[1] & 0xe0 == 0xe0):
        return 'mp3', 'audio/mp3'
    if data.startswith(b'OggS'): return ('opus' if b'OpusHead' in data[:128] else 'ogg'), 'audio/ogg'
    if data.startswith(b'fLaC'): return 'flac', 'audio/flac'
    if data[4:8] == b'ftyp': return 'mp4', 'video/mp4'
    if data.startswith(b'\x1a\x45\xdf\xa3'): return 'webm', 'video/webm'
    return 'unknown', 'application/octet-stream'


def trusted_onebot_file(value, limit):
    # Only paths returned by the local OneBot API are passed here; never event paths.
    path = Path(str(value)).resolve()
    roots = [LLBOT_BIN.parent.parent, Path(tempfile.gettempdir()),
             Path.home() / 'Documents' / 'Tencent Files',
             Path.home() / 'AppData' / 'Roaming' / 'Tencent',
             Path.home() / 'AppData' / 'Local' / 'Tencent']
    roots += [Path(x).resolve() for x in os.environ.get('SANAE_MEDIA_CACHE_ROOTS', '').split(os.pathsep) if x]
    if str(path).startswith('\\\\') or not any(path.is_relative_to(root.resolve()) for root in roots):
        raise ValueError('OneBot 媒体文件不在允许的缓存目录')
    with path.open('rb') as stream:
        return read_bounded(stream, limit)


def record_conversion(segment, api):
    data = onebot(api, 'get_record', {'file': segment.get('file') or segment.get('url'), 'out_format': 'mp3'})
    if data.get('file'):
        try:
            return trusted_onebot_file(data['file'], AUDIO_LIMIT)
        except (OSError, ValueError):
            if not data.get('url'):
                raise
    if data.get('url'):
        return download(data['url'], AUDIO_LIMIT)[0]
    raise ValueError('OneBot 转码未返回文件')


def runtime(name):
    override = os.environ.get({'ffmpeg': 'SANAE_FFMPEG', 'node': 'SANAE_NODE'}[name])
    if override and Path(override).is_file(): return str(Path(override).resolve())
    bundled = Path(__file__).parent / 'tools' / 'media' / (name + '.exe')
    if bundled.is_file(): return str(bundled)
    local = LLBOT_BIN / (name + '.exe')
    if local.is_file(): return str(local)
    return shutil.which(name)


def _run(args):
    check_active()
    if Path(args[0]).name.lower().startswith('ffmpeg'):
        restricted = []
        for arg in args:
            if arg == '-i':
                restricted += ['-protocol_whitelist', 'file,pipe', '-format_whitelist',
                               'wav,mp3,amr,ogg,flac,mov,matroska,webm,aac,lavfi']
            restricted.append(arg)
        args = restricted
    result = subprocess.run(args, executable=args[0], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, timeout=remaining_timeout(30),
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    check_active()
    if result.returncode:
        raise ValueError('本地媒体转换失败')


def decode_silk(data):
    if sniff(data)[0] != 'silk' or not 0 < len(data) <= 8 * 1024 * 1024:
        raise ValueError('Silk 格式或大小不允许')
    node = runtime('node')
    module = Path(os.environ.get('SANAE_SILK_MODULE', str(LLBOT_BIN / 'node_modules/silk-wasm/lib/index.cjs')))
    if not node or not module.is_file():
        raise ValueError('本地 Silk 解码器不可用')
    with tempfile.TemporaryDirectory(prefix='sanae-silk-') as folder:
        source, target = Path(folder) / 'input.silk', Path(folder) / 'output.pcm'
        source.write_bytes(data)
        _run([node, '--max-old-space-size=128', str(Path(__file__).with_name('decode_silk.cjs')),
              str(module), str(source), str(target)])
        with target.open('rb') as stream:
            pcm = read_bounded(stream, AUDIO_LIMIT - 44)
        if not pcm or len(pcm) % 2:
            raise ValueError('Silk 解码结果不是完整 PCM')
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            wav.writeframes(pcm)
        return output.getvalue()


def compress_audio(data, *, suffix='bin'):
    ffmpeg = runtime('ffmpeg')
    if not ffmpeg:
        raise ValueError('本机 ffmpeg 不可用')
    with tempfile.TemporaryDirectory(prefix='sanae-audio-') as folder:
        source, target = Path(folder) / ('input.' + suffix), Path(folder) / 'output.mp3'
        source.write_bytes(data)
        _run([ffmpeg, '-nostdin', '-v', 'error', '-y', '-i', str(source), '-vn', '-ac', '1',
              '-ar', '24000', '-b:a', '64k', '-fs', str(AUDIO_LIMIT + 1), str(target)])
        with target.open('rb') as stream:
            result = read_bounded(stream, AUDIO_LIMIT)
        if sniff(result)[0] != 'mp3':
            raise ValueError('音频压缩未产生 MP3')
        return result


def data_url(data, content_type):
    return 'data:%s;base64,%s' % (content_type, base64.b64encode(data).decode())


def normalize_audio(data, *, suffix='bin'):
    """Prefer compact MP3; support decoder-only ffmpeg installations with PCM WAV."""
    try:
        return compress_audio(data, suffix=suffix)
    except (OSError, ValueError, subprocess.SubprocessError):
        check_active()
    return pcm_audio(data, suffix=suffix)


def pcm_audio(data, *, suffix='bin'):
    ffmpeg = runtime('ffmpeg')
    if not ffmpeg: raise ValueError('本机音频转换器不可用')
    with tempfile.TemporaryDirectory(prefix='sanae-pcm-') as folder:
        source, target = Path(folder) / ('input.' + suffix), Path(folder) / 'output.wav'
        source.write_bytes(data)
        _run([ffmpeg, '-nostdin', '-v', 'error', '-y', '-i', str(source), '-vn',
              '-ac', '1', '-ar', '24000', '-c:a', 'pcm_s16le', '-fs', str(AUDIO_LIMIT + 1), str(target)])
        with target.open('rb') as stream: value = read_bounded(stream, AUDIO_LIMIT)
        if sniff(value)[0] != 'wav': raise ValueError('音频转换未产生 WAV')
        return value


def split_wav(data, max_chars=OMNI_LIMIT):
    result = []
    with wave.open(io.BytesIO(data), 'rb') as source:
        if source.getcomptype() != 'NONE': raise ValueError('仅支持 PCM WAV 分片')
        frame_bytes = source.getnchannels() * source.getsampwidth()
        frames = (((max_chars - 256) // 4) * 3 - 44) // frame_bytes
        if frames < 1: raise ValueError('音频分片上限过小')
        while True:
            chunk = source.readframes(frames)
            if not chunk: break
            output = io.BytesIO()
            with wave.open(output, 'wb') as target:
                target.setparams(source.getparams())
                target.writeframes(chunk)
            value = data_url(output.getvalue(), 'audio/wav')
            if len(value) > max_chars or len(result) >= 8:
                raise ValueError('音频分片超过上限')
            result.append(value)
    return result


def omni_inputs(media, max_chars=OMNI_LIMIT):
    value = data_url(media['data'], media['contentType'])
    if len(value) <= max_chars:
        return [(value, media['format'])]
    try:
        mp3 = compress_audio(media['data'], suffix=media['format'])
        compressed = data_url(mp3, 'audio/mp3')
        if len(compressed) <= max_chars:
            return [(compressed, 'mp3')]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    pcm = media['data'] if media['format'] == 'wav' else pcm_audio(media['data'], suffix=media['format'])
    if sniff(pcm)[0] == 'wav':
        return [(value, 'wav') for value in split_wav(pcm, max_chars)]
    raise ValueError('音频超限，无法在本机压缩或分片')


def fetch_media(event, api, limit=4):
    result, counts, total = [], {'video': 0, 'audio': 0}, 0
    for segment in resolve_segments(event, api):
        kind = 'video' if segment['type'] == 'video' else 'audio'
        if counts[kind] >= (1 if kind == 'video' else 3) or len(result) >= limit:
            continue
        counts[kind] += 1
        cap = VIDEO_LIMIT if kind == 'video' else AUDIO_LIMIT
        try:
            url = segment.get('url') or segment.get('file')
            if str(url).startswith(('https://', 'http://')):
                data, _ = download(url, cap)
            elif kind == 'audio':
                data = record_conversion(segment, api)
            else:
                raise ValueError('原视频地址不可用')
            total += len(data)
            if total > 128 * 1024 * 1024: raise ValueError('本次媒体总量超限')
            fingerprint = hashlib.sha256(data).hexdigest()
            fmt, mime = sniff(data)
            if fmt == 'silk':
                try:
                    converted = record_conversion(segment, api)
                    if sniff(converted)[0] in {'unknown', 'silk'}: raise ValueError('OneBot 未转码')
                    data = converted
                except Exception:
                    data = decode_silk(data)
                fmt, mime = sniff(data)
            if kind == 'audio' and fmt in {'mp4', 'webm', 'ogg', 'opus', 'flac', 'amr'}:
                data = normalize_audio(data, suffix=fmt)
                fmt, mime = sniff(data)
            if fmt == 'unknown': raise ValueError('无法识别媒体真实格式')
            result.append({'type': kind, 'data': data, 'format': fmt,
                           'contentType': mime, 'fingerprint': fingerprint})
        except Exception as exc:
            result.append({'type': kind, 'error': type(exc).__name__})
    return result
