"""Explicit media evidence using the existing provider switches and credentials."""
import json
import os
import re
from pathlib import Path
import tempfile
import urllib.request
from media_transport import (download as _download, fetch_media, data_url as _data_url,
    read_bounded, omni_inputs, compress_audio, normalize_audio, sniff, runtime, _run, resolve_segments)
from request_runtime import Budget, begin_call, finish_call, remaining_timeout, RequestTimeout


def _enabled(name): return os.environ.get(name, '0') == '1'


def _extract_text(response):
    if not isinstance(response, dict): return ''
    choices = response.get('choices') or []
    if choices:
        message = choices[0].get('message') or choices[0].get('delta') or {}
        content = message.get('content') or ''
        if isinstance(content, list):
            return '\n'.join(str(x.get('text', '')) for x in content if isinstance(x, dict)).strip()
        return str(content).strip()
    if response.get('text'): return str(response['text']).strip()
    for key in ('sentence', 'output'):
        if isinstance(response.get(key), dict):
            value = _extract_text(response[key])
            if value: return value
    return ''


def parse_response(raw, content_type=''):
    text = raw.decode('utf-8-sig')
    if 'text/event-stream' not in content_type and not text.lstrip().startswith('data:'):
        result = json.loads(text)
        if result.get('error') or result.get('code'): raise ValueError('媒体服务返回错误')
        return result
    chunks, sentences, usage, model = [], {}, {}, ''
    for line in text.splitlines():
        if not line.startswith('data:'): continue
        data = line[5:].strip()
        if not data or data == '[DONE]': continue
        obj = json.loads(data)
        if obj.get('error') or obj.get('code'): raise ValueError('媒体流返回错误')
        if obj.get('usage'): usage = obj['usage']
        if obj.get('model'): model = obj['model']
        output = obj.get('output') or {}
        sentence = output.get('sentence') or (output.get('output') or {}).get('sentence')
        if isinstance(sentence, dict):
            sentences[str(sentence.get('sentence_id', 0))] = str(sentence.get('text', ''))
        else:
            value = _extract_text(obj)
            if value: chunks.append(value)
    return {'choices': [{'message': {'content': ''.join(chunks) if chunks else '\n'.join(sentences.values())}}],
            'usage': usage, 'model': model}


def _post_json(url, key, payload, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key}, method='POST')
    with urllib.request.urlopen(req, timeout=remaining_timeout(timeout)) as response:
        return parse_response(read_bounded(response, 4 * 1024 * 1024), response.headers.get('Content-Type', ''))


def stage_config(kind):
    enabled, url, model, fallback = {
        'ASR': ('SANAE_AUDIO_TRANSCRIPTION_ENABLED', 'SANAE_AUDIO_ASR_URL', 'SANAE_AUDIO_ASR_MODEL', 'qwen-audio-3.0-asr-flash'),
        'Omni': ('SANAE_AUDIO_UNDERSTANDING_ENABLED', 'SANAE_AUDIO_OMNI_URL', 'SANAE_AUDIO_OMNI_MODEL', 'qwen3.5-omni-flash'),
        '视频': ('SANAE_VIDEO_UNDERSTANDING_ENABLED', 'SANAE_VIDEO_URL', 'SANAE_VIDEO_MODEL', 'qwen3.7-flash'),
    }[kind]
    key, address = os.environ.get('DASHSCOPE_API_KEY', ''), os.environ.get(url, '')
    status = 'ok' if _enabled(enabled) and address and key else ('未配置' if _enabled(enabled) else '未启用')
    return status, address, key, os.environ.get(model, fallback)


def asr_payload(media, model, context=''):
    messages = []
    if context:
        messages.append({'role': 'user', 'content': [{'type': 'input_text', 'text': context[:400]}]})
    messages.append({'role': 'user', 'content': [{'type': 'input_audio',
        'input_audio': {'data': _data_url(media['data'], media['contentType'])}}]})
    return {'model': model, 'input': {'messages': messages}, 'parameters': {'format': media['format']}}


def video_frames(data):
    ffmpeg = runtime('ffmpeg')
    if not ffmpeg: raise ValueError('本机 ffmpeg 不可用')
    with tempfile.TemporaryDirectory(prefix='sanae-video-') as folder:
        source = Path(folder) / 'input.video'
        source.write_bytes(data)
        _run([ffmpeg, '-nostdin', '-v', 'error', '-y', '-i', str(source), '-vf',
              'fps=1/10,scale=512:-2', '-frames:v', '3', str(Path(folder) / 'frame-%02d.jpg')])
        frames = []
        for path in sorted(Path(folder).glob('frame-*.jpg'))[:3]:
            with path.open('rb') as stream: frames.append(read_bounded(stream, 2 * 1024 * 1024))
        if not frames: raise ValueError('没有取得视频关键帧')
        return frames


def run_stage(kind, media, prompt=''):
    status, url, key, model = stage_config(kind)
    result = {'kind': kind, 'text': '', 'status': status, 'usage': [], 'model': model}
    if status != 'ok': return result
    if media.get('error'):
        result['status'] = '媒体读取失败'
        return result
    texts = []
    try:
        if kind == 'ASR':
            audio = media
            if media['type'] == 'video':
                raw = normalize_audio(media['data'], suffix=media['format'])
                fmt, mime = sniff(raw)
                audio = {**media, 'data': raw, 'contentType': mime, 'format': fmt}
            payloads = [asr_payload(audio, model, 'Minecraft、NeoForge、Forge、RCON、TPS、MSPT、mod、modpack')]
        elif kind == 'Omni':
            payloads = [{'model': model, 'messages': [{'role': 'user', 'content': [
                {'type': 'input_audio', 'input_audio': {'data': value, 'format': fmt}},
                {'type': 'text', 'text': prompt[:800] or '描述说话、唱歌、音乐或环境声音。'}]}],
                'modalities': ['text'], 'stream': True, 'stream_options': {'include_usage': True}, 'max_tokens': 900}
                for value, fmt in omni_inputs(media)]
        else:
            payloads = [{'model': model, 'messages': [{'role': 'user', 'content': [
                {'type': 'video_url', 'video_url': {'url': _data_url(media['data'], media['contentType']), 'fps': 1.0}},
                {'type': 'text', 'text': prompt[:800] or '描述整段视频中可确认的画面事实。'}]}], 'stream': False}]
        texts = []
        for payload in payloads:
            try:
                response = _post_json(url, key, payload)
            except RequestTimeout:
                raise
            except Exception:
                if kind != '视频': raise
                frames = video_frames(media['data'])
                response = _post_json(url, key, {'model': model, 'messages': [{'role': 'user', 'content':
                    [{'type': 'image_url', 'image_url': {'url': _data_url(frame, 'image/jpeg')}} for frame in frames]
                    + [{'type': 'text', 'text': '以下仅为局部关键帧，不能推断未覆盖画面。' + prompt[:800]}]}], 'stream': False})
                result['partial'] = True
            value = _extract_text(response)
            if value: texts.append(value)
            if isinstance(response.get('usage'), dict): result['usage'].append(response['usage'])
        result['text'], result['status'] = '\n'.join(texts)[:6000], 'ok' if texts else '未取得可用内容'
    except Exception as exc:
        result['status'] = '超时' if isinstance(exc, RequestTimeout) else '处理失败（%s）' % type(exc).__name__
        if texts:
            result['text'] = '\n'.join(texts)[:6000]
            result['status'] = '部分完成；' + result['status']
    return result


def transcribe(media, context_text=''):
    result = run_stage('ASR', media, context_text)
    return result['text'] or None, result['status']


def understand_audio(media, prompt=''):
    result = run_stage('Omni', media, prompt)
    return result['text'] or None, result['status']


def understand_video(media, prompt=''):
    result = run_stage('视频', media, prompt)
    return result['text'] or None, result['status']


def fetch_segments(event, api_url, limit=4): return fetch_media(event, api_url, limit)


def media_intent(prompt):
    text = re.sub(r'\[CQ:[^\]]*\]', '', str(prompt)).strip()
    return 'transcribe' if re.match(r'^[!！]\s*(?:语音转写|转写)(?:\s|$)', text) else 'understand'


def usage_footer(stages):
    lines = []
    for stage in stages:
        usages = stage.get('usage') or []
        tokens = [u for u in usages if isinstance(u.get('prompt_tokens'), (int, float))
                  and isinstance(u.get('completion_tokens'), (int, float))]
        seconds = [u.get('duration', u.get('seconds')) for u in usages]
        seconds = [x for x in seconds if isinstance(x, (int, float))]
        if tokens:
            detail = '输入/输出 %d/%d tokens' % (sum(u['prompt_tokens'] for u in tokens), sum(u['completion_tokens'] for u in tokens))
        elif seconds: detail = '音频 %.1f 秒' % sum(seconds)
        elif stage['status'] == 'ok': detail = '服务未返回用量'
        else: detail = stage['status'] + ('，用量未知' if stage['status'] not in {'未启用', '未配置'} else '')
        lines.append(stage['kind'] + '：' + detail)
    return '；'.join(lines)


def analyze_media(event, api_url, prompt='', budget=None):
    budget = budget or Budget(120)
    resolved = resolve_segments(event, api_url)
    if not resolved:
        return {'evidence': '', 'status': 'no media', 'fingerprints': (), 'usage': ''}
    kinds = ['ASR'] if media_intent(prompt) == 'transcribe' else ['ASR', 'Omni', '视频']
    if not any(stage_config(kind)[0] == 'ok' for kind in kinds):
        return {'evidence': '', 'status': '媒体服务未启用或尚未配置。', 'fingerprints': (), 'usage': ''}
    media = fetch_segments({'message': [{'type': item['type'], 'data': {k: v for k, v in item.items() if k != 'type'}} for item in resolved]}, api_url)
    if not media:
        return {'evidence': '', 'status': '没有取得引用或当前消息的媒体。', 'fingerprints': (), 'usage': ''}
    stages, notes, fingerprints = [], [], []
    for item in media:
        budget.remaining()
        if item.get('fingerprint'): fingerprints.append(item['fingerprint'])
        selected = ['ASR'] if media_intent(prompt) == 'transcribe' else (['视频', 'ASR'] if item['type'] == 'video' else ['ASR', 'Omni'])
        pending = []
        for kind in selected:
            try: pending.append((kind, begin_call(lambda k=kind, m=item: run_stage(k, m, prompt), budget=budget, seconds=90)))
            except Exception as exc: stages.append({'kind': kind, 'text': '', 'status': type(exc).__name__, 'usage': []})
        for kind, handle in pending:
            try: stage = finish_call(handle)
            except Exception as exc: stage = {'kind': kind, 'text': '', 'status': type(exc).__name__, 'usage': []}
            stages.append(stage)
            if stage.get('text'):
                notes.append(kind + ('（仅局部关键帧）' if stage.get('partial') else '') + '：' + stage['text'])
    return {'evidence': '\n'.join(notes)[:10000], 'status': 'ok' if notes else '未取得可用媒体证据。',
            'fingerprints': tuple(fingerprints[:3]), 'usage': usage_footer(stages)}


def describe_media(event, api_url, prompt=''):
    result = analyze_media(event, api_url, prompt)
    return result['evidence'] or None, result['status']


def media_fingerprints(event, api_url):
    return tuple(item['fingerprint'] for item in fetch_media(event, api_url) if item.get('fingerprint'))[:3]


def image_host_upload(data, content_type='image/png'):
    if not _enabled('SANAE_IMAGE_HOST_ENABLED'): return None, 'image host disabled'
    url, token = os.environ.get('SANAE_IMAGE_HOST_URL', ''), os.environ.get('SANAE_IMAGE_HOST_TOKEN', '')
    if not url or not token: return None, 'image host is not configured'
    req = urllib.request.Request(url, data=data, headers={'Authorization': 'Bearer ' + token, 'Content-Type': content_type}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=remaining_timeout(30)) as response:
            body = json.loads(read_bounded(response, 1024 * 1024))
        result = body.get('url') or (body.get('data') or {}).get('url')
        return (str(result), 'ok') if result else (None, 'image host returned no URL')
    except Exception as exc: return None, type(exc).__name__
