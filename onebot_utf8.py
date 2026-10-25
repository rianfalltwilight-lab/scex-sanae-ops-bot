#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OneBot JSON 的唯一 UTF-8 编解码入口。"""
import json
import os


JSON_CONTENT_TYPE = 'application/json; charset=utf-8'
JSON_HEADERS = {'Content-Type': JSON_CONTENT_TYPE}


def onebot_headers(token=None):
    """Headers for OneBot only; an explicit empty token disables authentication."""
    value = os.environ.get('LLBOT_API_TOKEN', '') if token is None else token
    headers = {'Content-Type': JSON_CONTENT_TYPE}
    if value:
        headers['Authorization'] = 'Bearer ' + str(value)
    return headers


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def utf8_text(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    return str(value).lstrip('\ufeff')


def json_loads_utf8(value):
    return json.loads(utf8_text(value))


def onebot_success_utf8(value):
    try:
        data = json_loads_utf8(value)
    except (TypeError, ValueError):
        return False, None
    if data.get('status') == 'failed':
        return False, data
    return data.get('retcode') in (0, '0'), data
