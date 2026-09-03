#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模组查询模块（2026-08-16 狗蛋要求：给 bot 植入，类似 !wiki）
- 数据源：Modrinth 官方 API（M 站）+ CurseForge（CF 站，走 cfwidget 第三方接口）
- 自动翻译：英文简介 → 中文（zhipu bigmodel chat API）
- 零 token、独立线程、不碰 AI 决策通道
"""
import json
import re
import html
import time
import urllib.parse
import urllib.request

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'

# zhipu chat 翻译（key 从 bridge 注入）
ZHIPU_KEY = ''
ZHIPU_URL = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
ZHIPU_MODEL = 'glm-4-flash'

# CurseForge 官方 API（key 从 bridge 注入）
CF_API_KEY = ''
CF_API = 'https://api.curseforge.com/v1'
CF_GAME_ID = 432  # Minecraft


def _http_json(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', errors='replace'))


def search_modrinth(keyword, limit=3):
    """Modrinth 搜索，返回命中列表 [{slug,title,description,categories,versions,downloads,link}]"""
    try:
        facets = urllib.parse.quote(json.dumps([['project_type:mod']], ensure_ascii=False))
        url = ('https://api.modrinth.com/v2/search?query=' + urllib.parse.quote(keyword)
               + f'&limit={limit}&facets=' + facets)
        d = _http_json(url)
        hits = d.get('hits', [])
        out = []
        for h in hits:
            out.append({
                'slug': h.get('slug', ''),
                'title': h.get('title', ''),
                'description': h.get('description', ''),
                'categories': h.get('display_categories', [])[:4],
                'versions': h.get('versions', [])[:4],
                'downloads': h.get('downloads', 0),
                'link': 'https://modrinth.com/mod/' + h.get('slug', ''),
            })
        return out
    except Exception as e:
        print(f'[mod] Modrinth 搜索失败: {e}', flush=True)
        return []


def search_curseforge(slug, title=''):
    """CF 站详情（cfwidget 接口）。slug 可能对不上，自动生成候选 slug 逐个试。"""
    candidates = [slug]
    # title 转 CF slug：小写、非字母数字变连字符、去尾部数字重复
    if title:
        t = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
        if t:
            candidates.append(t)
            # 去掉尾部 -数字（如 ...-2）再试一次
            t2 = re.sub(r'-\d+$', '', t)
            if t2 != t:
                candidates.append(t2)
    seen = set()
    for s in candidates:
        if s in seen:
            continue
        seen.add(s)
        try:
            url = 'https://api.cfwidget.com/minecraft/mc-mods/' + urllib.parse.quote(s)
            d = _http_json(url)
            if not d or d.get('error') or not d.get('title'):
                continue
            title_ = d.get('title', '')
            summary = re.sub(r'<[^>]+>', '', d.get('summary', '') or '').strip()
            desc = re.sub(r'<[^>]+>', ' ', d.get('description', '') or '')
            desc = html.unescape(re.sub(r'\s+', ' ', desc)).strip()
            intro = summary or desc[:600]
            return {
                'title': title_,
                'intro': intro,
                'downloads': d.get('downloads', 0),
                'link': 'https://www.curseforge.com/minecraft/mc-mods/' + s,
                'version': (d.get('versions', {}) or {}).get('latest', ''),
            }
        except Exception as e:
            print(f'[mod] CF 查询失败({s}): {e}', flush=True)
    return None


def translate(text, target='中文'):
    """zhipu chat 翻译英文简介到中文。失败返回原文。"""
    if not ZHIPU_KEY or not text:
        return text
    try:
        payload = json.dumps({
            'model': ZHIPU_MODEL,
            'messages': [
                {'role': 'system', 'content': f'你是 Minecraft 模组简介翻译器。把用户给的模组介绍翻译成简洁的{target}，保留专有名词（模组名、AE2 等缩写不译），去掉 HTML 标签，控制在 120 字内。只输出译文。'},
                {'role': 'user', 'content': text[:900]},
            ],
            'temperature': 0.3,
        }).encode()
        req = urllib.request.Request(ZHIPU_URL, data=payload, headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + ZHIPU_KEY,
        }, method='POST')
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode('utf-8'))
        return (d.get('choices') or [{}])[0].get('message', {}).get('content', '').strip() or text
    except Exception as e:
        print(f'[mod] 翻译失败: {e}', flush=True)
        return text


def lookup_mod(keyword):
    """主入口：搜索 Modrinth + 尝试 CF，返回格式化文本。"""
    hits = search_modrinth(keyword)
    if not hits:
        return f'模组站没找到「{keyword}」，换个关键词试试~'
    h = hits[0]
    lines = [f'【{h["title"]}】(Modrinth)']
    intro = h.get('description', '') or '（无简介）'
    zh = translate(intro)
    if zh and zh != intro:
        lines.append('简介: ' + zh)
    else:
        lines.append('简介: ' + intro[:120])
    if h.get('categories'):
        lines.append('分类: ' + ' / '.join(h['categories']))
    if h.get('versions'):
        lines.append('版本: ' + ' / '.join(h['versions'][:4]))
    lines.append(f'下载: {h.get("downloads", 0):,} | {h["link"]}')
    # CF 侧补充（同 slug 尝试）
    cf = search_curseforge(h['slug'], h.get('title', ''))
    if cf:
        lines.append(f'CF站: {cf["title"]} ({cf["link"]})')
    # 若还有第 2、3 条候选，附上名称供选择
    if len(hits) > 1:
        alt = '、'.join(f'{x["title"]}' for x in hits[1:])
        lines.append('其他候选: ' + alt)
    return '\n'.join(lines)


def set_zhipu_key(key):
    global ZHIPU_KEY
    ZHIPU_KEY = key


def set_cf_api_key(key):
    global CF_API_KEY
    CF_API_KEY = key


def search_cf_api(keyword, limit=3):
    """CurseForge 官方 API 搜索，返回命中列表 [{id,name,slug,summary,downloads,categories,versions,link}]"""
    if not CF_API_KEY:
        return []
    url = (CF_API + '/mods/search?gameId=' + str(CF_GAME_ID)
           + '&searchFilter=' + urllib.parse.quote(keyword)
           + '&pageSize=' + str(limit) + '&sortField=2&sortOrder=desc')
    req = urllib.request.Request(url, headers={'x-api-key': CF_API_KEY, 'Accept': 'application/json', 'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode('utf-8', errors='replace'))
    out = []
    for m in (d.get('data') or [])[:limit]:
        cats = ', '.join((c or {}).get('name', '') for c in (m.get('categories') or []) if c)
        out.append({
            'id': m.get('id'),
            'name': m.get('name', ''),
            'slug': m.get('slug', ''),
            'summary': (m.get('summary') or '').strip(),
            'downloads': m.get('downloadCount', 0),
            'categories': cats,
            'versions': '',
            'link': (m.get('links') or {}).get('websiteUrl', '') or f"https://www.curseforge.com/minecraft/mc-mods/{m.get('slug', '')}",
        })
    return out


def lookup_cf(keyword):
    """!cf 主入口：CurseForge 官方搜索 + 中文翻译。"""
    try:
        hits = search_cf_api(keyword)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return 'CurseForge 搜索接口拒绝访问（API key 权限问题），用 !mod 走 Modrinth 吧~'
        return f'CurseForge 搜索出错（HTTP {e.code}），稍后再试~'
    except Exception as e:
        print(f'[mod] CF API 搜索失败: {e}', flush=True)
        return f'CurseForge 搜索出错（{e}），稍后再试~'
    if not hits:
        return f'CurseForge 没找到「{keyword}」，换个关键词试试~'
    h = hits[0]
    lines = [f'【{h["name"]}】(CurseForge 官方)' ]
    intro = h.get('summary') or '（无简介）'
    zh = translate(intro)
    lines.append('简介: ' + (zh if zh and zh != intro else intro[:120]))
    if h.get('categories'):
        lines.append('分类: ' + h['categories'])
    lines.append(f'下载: {h.get("downloads", 0):,} | {h["link"]}')
    if len(hits) > 1:
        alt = '、'.join(x['name'] for x in hits[1:])
        lines.append('其他候选: ' + alt)
    return '\n'.join(lines) or ''
