#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mac 文件服务客户端（2026-08-17 A/C 档跨机通道）
- A档：AI 文件工具（list/read/tail/search/nbt/prop/replace）
- C档：!backup 异地同步触发（/sync）
"""
import os
import urllib.request
import urllib.parse

from local_secrets import require_secret

MCFILE_URL = os.environ.get('MCFILE_URL', 'http://127.0.0.1:18800')
MCFILE_TOKEN = require_secret('mcfile_token')


def mcfile_get(endpoint, **params):
    q = urllib.parse.urlencode({'token': MCFILE_TOKEN, **params})
    with urllib.request.urlopen(MCFILE_URL + endpoint + '?' + q, timeout=25) as r:
        return r.read().decode('utf-8')


def mcfile_post(endpoint, **params):
    q = urllib.parse.urlencode({'token': MCFILE_TOKEN, **params})
    req = urllib.request.Request(MCFILE_URL + endpoint + '?' + q, data=b'', method='POST')
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode('utf-8')
