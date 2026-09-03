#!/usr/bin/env python3
"""Optional QQ media adapters.

Network/model calls are opt-in through environment variables. Media is only
processed for an explicit AI request and is treated as untrusted evidence.
"""
import base64
import json
import os
import subprocess
import tempfile
import urllib.parse
import urllib.request

from media_segments import media_segments


def _enabled(name):
    return os.environ.get(name, "0") == "1"


def _download(url, limit=64 * 1024 * 1024):
    req = urllib.request.Request(str(url), headers={"User-Agent": "SanaeMedia/1.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("媒体文件超过安全大小限制")
        return data, response.headers.get("Content-Type", "application/octet-stream")


def fetch_segments(event, api_url, limit=3):
    """Resolve at most a few explicit record/audio/video segments to bytes."""
    result = []
    for segment in media_segments(event, ("record", "audio", "video"))[:limit]:
        url = segment.get("url")
        if not url and segment.get("file") and str(segment["file"]).startswith("http"):
            url = segment["file"]
        if not url and segment.get("file"):
            payload = json.dumps({"file": segment["file"]}).encode()
            req = urllib.request.Request(api_url.rstrip("/") + "/get_record", data=payload,
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=15) as response:
                    body = json.loads(response.read().decode("utf-8"))
                url = ((body.get("data") or {}).get("url") if isinstance(body, dict) else None)
            except Exception:
                url = None
        if url:
            data, content_type = _download(url)
            result.append({"type": segment.get("type"), "data": data,
                           "contentType": content_type, "source": str(url)[:200]})
    return result


def _data_url(data, content_type):
    return "data:%s;base64,%s" % (content_type or "application/octet-stream",
                                  base64.b64encode(data).decode("ascii"))


def _post_json(url, key, payload, timeout=90):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _extract_text(response):
    choices = response.get("choices") or []
    message = (choices[0].get("message") if choices else {}) or {}
    return str(message.get("content") or "").strip()


def transcribe(media, context_text=""):
    if not _enabled("SANAE_AUDIO_TRANSCRIPTION_ENABLED"):
        return None, "audio transcription disabled"
    url = os.environ.get("SANAE_AUDIO_ASR_URL", "")
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    model = os.environ.get("SANAE_AUDIO_ASR_MODEL", "qwen-audio-3.0-asr-flash")
    if not url or not key:
        return None, "audio transcription is not configured"
    payload = {"model": model, "input": {"messages": [{"role": "user", "content": [
        {"audio": _data_url(media["data"], media.get("contentType"))},
        {"text": context_text[:400] if context_text else "请准确转写这段音频。"},
    ]}]}}
    try:
        return _extract_text(_post_json(url, key, payload)), "ok"
    except Exception as exc:
        return None, str(exc)


def understand_audio(media, prompt=""):
    if not _enabled("SANAE_AUDIO_UNDERSTANDING_ENABLED"):
        return None, "audio understanding disabled"
    url = os.environ.get("SANAE_AUDIO_OMNI_URL", "")
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    model = os.environ.get("SANAE_AUDIO_OMNI_MODEL", "qwen3.5-omni-flash")
    if not url or not key:
        return None, "audio understanding is not configured"
    payload = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": _data_url(media["data"], media.get("contentType")), "format": "wav"}},
        {"type": "text", "text": prompt[:800] or "判断这段音频是说话、唱歌、音乐还是环境声，并说明能听出的特征。"},
    ]}]}
    try:
        return _extract_text(_post_json(url, key, payload)), "ok"
    except Exception as exc:
        return None, str(exc)


def understand_video(media, prompt=""):
    """Optional native video vision path; callers must explicitly enable it."""
    if not _enabled("SANAE_VIDEO_UNDERSTANDING_ENABLED"):
        return None, "video understanding disabled"
    url = os.environ.get("SANAE_VIDEO_URL", "")
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    model = os.environ.get("SANAE_VIDEO_MODEL", "qwen3.7-flash")
    if not url or not key:
        return None, "video understanding is not configured"
    payload = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "video_url", "video_url": {"url": _data_url(media["data"], media.get("contentType"))}},
        {"type": "text", "text": prompt[:800] or "概括这段视频中可确认的画面内容。"},
    ]}], "stream": False, "extra_body": {"fps": 1.0}}
    try:
        return _extract_text(_post_json(url, key, payload)), "ok"
    except Exception as exc:
        return None, str(exc)


def describe_media(event, api_url, prompt=""):
    """Return evidence only; never executes commands or uploads media."""
    media = fetch_segments(event, api_url)
    if not media:
        return None, "no media"
    reports = []
    for item in media:
        if item.get("type") == "video":
            video, video_status = understand_video(item, prompt)
            if video:
                reports.append("视频视觉：" + video[:3000])
            else:
                reports.append("视频视觉证据不可用（%s）" % video_status)
        asr, asr_status = transcribe(item, "Minecraft、NeoForge、Forge、RCON、TPS、MSPT、mod、modpack")
        omni, omni_status = understand_audio(item, prompt) if item.get("type") != "video" else (None, "not requested for video")
        if asr:
            reports.append("ASR：" + asr[:2000])
        if omni:
            reports.append("声音理解：" + omni[:2000])
        if not asr and not omni and item.get("type") != "video":
            reports.append("媒体证据不可用（ASR=%s；Omni=%s）" % (asr_status, omni_status))
    return "\n".join(reports), "ok"


def image_host_upload(data, content_type="image/png"):
    """Optional private image-host adapter; never enabled by default."""
    if not _enabled("SANAE_IMAGE_HOST_ENABLED"):
        return None, "image host disabled"
    url = os.environ.get("SANAE_IMAGE_HOST_URL", "")
    token = os.environ.get("SANAE_IMAGE_HOST_TOKEN", "")
    if not url or not token:
        return None, "image host is not configured"
    request = urllib.request.Request(url, data=data, headers={
        "Authorization": "Bearer " + token, "Content-Type": content_type}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        result = body.get("url") or (body.get("data") or {}).get("url")
        return (str(result), "ok") if result else (None, "image host returned no URL")
    except Exception as exc:
        return None, str(exc)
