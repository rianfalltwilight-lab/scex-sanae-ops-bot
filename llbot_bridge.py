#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
早苗群消息桥接服务（2026-08-16 完全重做版）
- 监听 SANAE_BRIDGE_PORT，接收早苗 LLBot 的 OneBot 11 HTTP 上报
- 只处理 LLBOT_GROUP 指定的群；不依赖 OpenClaw agent/hooks
- 全部回复统一走早苗 API（3002）发回
- 功能路由（全部独立模块，零 agent 依赖）：
  1. !wiki/!百科     → mcmod.cn 抓取（_fetch_wiki）
  2. !mod/!模组/!cf  → Modrinth + CF + 自动翻译（mod_lookup.py）
  3. !tps/!list/status → RCON 直查（rcon_client.py）
  4. @早苗           → 正则匹配 → DeepSeek API → 早苗人设回复（sanae_ai.py）
  5. 群→服 G2S      → 群里消息 say 进游戏内聊天（_relay_g2s）
  6. 私聊           → 转发给狗蛋
"""
import json
import re
import html
import threading
import time
import collections
import unicodedata
import difflib
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

# 同目录导入依赖模块（systemd 启动时工作目录可能不是脚本目录，显式加 sys.path）
import os as _os
import sys as _sys
_BRIDGE_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _BRIDGE_DIR not in _sys.path:
    _sys.path.insert(0, _BRIDGE_DIR)
from ops_contract import onebot_success
from onebot_utf8 import JSON_CONTENT_TYPE, json_bytes, utf8_text
from server_registry import (clear_selected, extract_server_selector,
                             extract_natural_server_selector, fanout_group_message,
                             get_selected, list_servers, query_server, resolve_server,
                             select_server, server_hint, server_prefix)
from ops_commands import STATE_BASE as OPS_STATE_BASE, dispatch_ops_command, match_ops_command

# 模组查询模块（CF 站 + M 站 + 自动翻译）
from local_secrets import require_secret as _require_secret
import mod_lookup as _mod_lookup
_mod_lookup.set_zhipu_key(_require_secret('zhipu_api_key'))
_mod_lookup.set_cf_api_key(_require_secret('curseforge_api_key'))

# 早苗 AI 独立模块（正则匹配 + DeepSeek API，不走 agent）
import sanae_ai as _sanae_ai
import backup_watch as _backup_watch
from social_lite import SocialLite, OneBotActions, split_social_reply
from slang_learner import SlangLearner
from social_feedback import FeedbackStore
from sticker_catalog import StickerCatalog
from chat_archive import ChatArchive
from social_memory import SocialMemory

# RCON 反控命令模块（2026-08-16 按腐竹运维监控工具包 QQConsoleBridge 命令集实现）
import rcon_ops as _rcon_ops
from player_bindings import PlayerBindings
from media_segments import media_segments
from media_ai import image_host_upload, _download

# ===== 基本配置 =====
LLBOT_GROUP = int(_os.environ.get('LLBOT_GROUP', '0'))
WIKI_BOT_QQ = _os.environ.get('SANAE_BOT_QQ', '0')
WIKI_API = _os.environ.get("LLBOT_API", "http://127.0.0.1:3000")
BRIDGE_PORT = int(_os.environ.get('SANAE_BRIDGE_PORT', '18790'))
CALLBACK_TOKEN = _os.environ.get('LLBOT_CALLBACK_TOKEN', '')
CALLBACK_SOURCE_IPS = {
    ip.strip() for ip in _os.environ.get('LLBOT_SOURCE_IPS', '127.0.0.1').split(',')
    if ip.strip()
}

# Group-local easter egg moderation.  This stays inside the existing bridge:
# no agent, model call, extra process or token usage.
FUN_MUTE_GROUP = int(_os.environ.get('FUN_MUTE_GROUP', str(LLBOT_GROUP)))
FUN_MUTE_USER = str(_os.environ.get('FUN_MUTE_USER', '*')).strip()
FUN_MUTE_SECONDS = max(60, min(3600, int(_os.environ.get('FUN_MUTE_SECONDS', '300'))))

# 群聊功能总开关（环境变量 GROUP_CHAT_ENABLED=0 关闭；保留私聊转发）
GROUP_CHAT_ENABLED = _os.environ.get('GROUP_CHAT_ENABLED', '1') == '1'
# 群→双服全量转发开关；生产默认开启，显式 RELAY_G2S=0 才关闭。
RELAY_G2S_ENABLED = _os.environ.get('RELAY_G2S', '1') != '0'

# Social Lite is scoped to the existing production group only.  It is opt-in
# and adds no process, account, or agent.
SOCIAL_LITE_GROUP = int(_os.environ.get('SOCIAL_LITE_GROUP', str(LLBOT_GROUP)))
SOCIAL_LITE_ENABLED = _os.environ.get('SOCIAL_LITE_ENABLED', '0') == '1'
SOCIAL_LITE = SocialLite(
    _os.environ.get('SOCIAL_LITE_STATE', _os.path.join(_BRIDGE_DIR, 'state', 'social-lite.json')),
    _os.environ.get('SOCIAL_LITE_ACTIVITY', _os.path.join(_BRIDGE_DIR, 'logs', 'social-activity.log')),
    self_id=WIKI_BOT_QQ,
    max_recent=int(_os.environ.get('SOCIAL_LITE_MAX_RECENT', '30')),
    cooldown_seconds=float(_os.environ.get('SOCIAL_LITE_COOLDOWN', '12')),
    spontaneous_probability=float(_os.environ.get('SOCIAL_LITE_SPONTANEOUS', '0')),
    ambient_reply_weight=float(_os.environ.get('SOCIAL_LITE_AMBIENT_WEIGHT', '1.0')),
)
QQ_ACTIONS = OneBotActions(_os.environ.get('LLBOT_API', WIKI_API),
                           token=_os.environ.get('LLBOT_API_TOKEN', ''))
SLANG_LEARNER = SlangLearner(
    _os.environ.get('SLANG_PATH', _os.path.join(_BRIDGE_DIR, 'state', 'slang.json')),
    group_id=SOCIAL_LITE_GROUP,
    max_entries=int(_os.environ.get('SLANG_MAX_ENTRIES', '300')),
    max_evidence=int(_os.environ.get('SLANG_MAX_EVIDENCE', '8')),
)
FEEDBACK_STORE = FeedbackStore(
    _os.environ.get('FEEDBACK_PATH', _os.path.join(_BRIDGE_DIR, 'state', 'social-feedback.json')),
    group_id=SOCIAL_LITE_GROUP,
    max_items=int(_os.environ.get('FEEDBACK_MAX_ITEMS', '500')),
)
STICKER_CATALOG = StickerCatalog(
    _os.environ.get('STICKER_MANIFEST', _os.path.join(_BRIDGE_DIR, 'state', 'stickers', 'stickers.json')))
SOCIAL_ARCHIVE = ChatArchive(_os.environ.get(
    'SOCIAL_ARCHIVE_ROOT', _os.path.join(_BRIDGE_DIR, 'logs', 'chat-archive')))
SOCIAL_MEMORY = SocialMemory(_os.environ.get(
    'SOCIAL_MEMORY_PATH', _os.path.join(_BRIDGE_DIR, 'state', 'social-memory.json')),
    group_id=SOCIAL_LITE_GROUP,
    max_topics=int(_os.environ.get('SOCIAL_MEMORY_MAX_TOPICS', '80')))
SOCIAL_STICKER_MODE = _os.environ.get('SOCIAL_STICKER_MODE', 'recommend').strip().lower()
SOCIAL_STICKER_COOLDOWN = max(10.0, float(_os.environ.get('SOCIAL_STICKER_COOLDOWN', '45')))
_STICKER_COOLDOWN_LOCK = threading.Lock()
_LAST_AUTO_STICKER: dict[str, float] = {}


def _sticker_cooldown_ok(group_id):
    now = time.time()
    key = str(group_id)
    with _STICKER_COOLDOWN_LOCK:
        last = float(_LAST_AUTO_STICKER.get(key) or 0)
        if now - last < SOCIAL_STICKER_COOLDOWN:
            return False
        _LAST_AUTO_STICKER[key] = now
        return True


def _fun_mute_thread(event, reason):
    gid = int(event.get('group_id') or 0)
    uid = str(event.get('user_id') or '')
    try:
        QQ_ACTIONS.mute(gid, uid, FUN_MUTE_SECONDS)
        send_group_msg('片没有，五分钟冷静套餐有。', api=WIKI_API)
        print(f'[fun-mute] group={gid} user={uid} duration={FUN_MUTE_SECONDS} '
              f'reason={reason} ok=True', flush=True)
    except Exception as exc:
        print(f'[fun-mute] group={gid} user={uid} duration={FUN_MUTE_SECONDS} '
              f'reason={reason} ok=False error={type(exc).__name__}', flush=True)


PLAYER_BINDINGS_ENABLED = _os.environ.get('PLAYER_BINDINGS_ENABLED', '') == '1'
PLAYER_BINDINGS = PlayerBindings(_os.environ.get(
    'PLAYER_BINDINGS_PATH', _os.path.join(_BRIDGE_DIR, 'logs', 'qq-player-binds.json')))
CHAT_RELAY_STATE = _os.path.join(_BRIDGE_DIR, 'logs', 'qq-chat-relay.json')
_CHAT_RELAY_LOCK = threading.Lock()

# ===== 权限配置（私聊转发目标用）=====
_PERMS_FILE = _os.path.join(_BRIDGE_DIR, 'qq-admins.json')
if _os.environ.get('SCE_BOT_TEST_MODE') == '1' and not _os.path.exists(_PERMS_FILE):
    _PERMS = {'owner': [], 'admins': []}
else:
    with open(_PERMS_FILE, encoding='utf-8') as _f:
        _PERMS = json.load(_f)
OWNERS = set(str(x) for x in _PERMS.get('owner', []))          # 狗蛋
ADMINS = set(str(x) for x in _PERMS.get('admins', []))         # 管理员

# ===== 归一化 =====
def _normalize(raw):
    """全角转半角（NFKC）+ 去 CQ 码 + 去空白。"""
    # OneBot/QQ may encode literal brackets as ``&#91;`` / ``&#93;``.
    # Decode before command parsing so selectors such as [怀旧] remain
    # intuitive instead of silently falling through to ordinary chat.
    s = html.unescape(str(raw or ''))
    s = unicodedata.normalize('NFKC', s)
    s = re.sub(r'\[CQ:[^\]]*\]', '', s)
    return s.strip()


_FUN_MUTE_EXAMPLES = (
    '来点片', '发点片', '整点片', '有没有片', '求片',
    '来点涩图', '发点色图', '来点本子', '来点人妻',
    '来点淫秽色情', '来点色情内容', '来点黄色废料', '搞点少儿不宜的',
    '帮我生成黄文', '帮我写群主黄文', '生成群主黄文',
    '帮我骚扰群主', '骚扰群主',
)
_FUN_REQUEST_WORDS = ('来点', '来张', '发点', '发张', '整点', '给我', '求', '有没有',
                      '生成', '写', '搞点', '安排')
_FUN_ADULT_WORDS = ('黄片', '毛片', '涩图', '色图', '黄图', '黄文', '本子', '人妻',
                    '成人图', '成人视频', '色情', '淫秽', '淫乱', '少儿不宜',
                    '成人内容', '黄色废料', '黄色内容', '成人向', '限制级',
                    'porn', 'hentai', 'nsfw')
_FUN_HARASS_WORDS = ('骚扰', '轰炸', '调戏', '非礼')
_FUN_BENIGN_CONTEXT = ('不要', '别发', '禁止', '有人说', '他说', '关键词', '正则',
                       '功能', '测试', '讨论', '为什么')
_FUN_BENIGN_PIAN_WORDS = ('碎片', '芯片', '照片', '图片', '纪录片', '动画片',
                          '影片', '唱片', '切片')


def _fun_compact_text(raw):
    """Return normalized text for the zero-token local moderation classifier."""
    text = html.unescape(_normalize(str(raw or ''))).lower()
    text = text.replace('澀', '色').replace('涩', '色').replace('瑟图', '色图')
    return re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '', text)


def _fun_mute_intent(raw):
    """Classify the configured joke behaviour without an LLM.

    Returns a short local reason for high-confidence matches, otherwise ''.
    It combines intent-feature scoring with fuzzy similarity, so this is not
    an exact regular-expression trigger and costs zero model tokens.
    """
    text = _fun_compact_text(raw)
    if not text or len(text) > 48:
        return ''
    compact_examples = tuple(_fun_compact_text(item) for item in _FUN_MUTE_EXAMPLES)
    if text in compact_examples:
        return 'exact-example'
    if any(marker in text for marker in _FUN_BENIGN_CONTEXT):
        return ''
    if any(marker in text for marker in _FUN_BENIGN_PIAN_WORDS):
        return ''

    # Short misspellings and small wording variants of curated examples.
    similarity = max(difflib.SequenceMatcher(None, text, item).ratio()
                     for item in compact_examples)
    if len(text) <= 18 and similarity >= 0.84:
        return 'fuzzy-example'

    score = 0
    request = any(word in text for word in _FUN_REQUEST_WORDS)
    adult = any(word in text for word in _FUN_ADULT_WORDS)
    harassment = any(word in text for word in _FUN_HARASS_WORDS)
    named_target = any(word in text for word in ('群主', '管理', '早苗', '她', '他'))
    if request:
        score += 2
    if adult:
        score += 3
    if harassment:
        score += 3
    if harassment and named_target:
        score += 2
    if ('生成' in text or '写' in text) and ('黄文' in text or '黄图' in text):
        score += 2
    if any(phrase in text for phrase in ('来点片', '发点片', '整点片', '有没有片', '求片',
                                         '好久没发片', '发片了')):
        score += 5
    return 'intent-score' if score >= 5 else ''


def _fun_mute_reason(event):
    if int(event.get('group_id') or 0) != FUN_MUTE_GROUP:
        return ''
    uid = str(event.get('user_id') or '')
    if not uid or uid == WIKI_BOT_QQ or uid in OWNERS or uid in ADMINS:
        return ''
    if FUN_MUTE_USER not in ('', '*', 'all') and uid != FUN_MUTE_USER:
        return ''
    return _fun_mute_intent(event.get('raw_message') or event.get('message') or '')


def _selection_reply(raw, group_id, user_id):
    """Handle one-step, persistent per-user operation-target commands."""
    text = _normalize(raw)
    show = re.fullmatch(r'!(?:服|服务器|servers?)', text, re.I)
    clear = re.fullmatch(r'!(?:服\s+(?:自动|清除|取消|无)|取消选服|取消选择服务器)', text, re.I)
    choice = re.fullmatch(r'!(?:服|选择服务器|绑定服务器|select)\s+(.+)', text, re.I)
    server = None
    if not (show or clear or choice):
        direct = re.fullmatch(r'!(.+)', text, re.S)
        server = resolve_server(direct.group(1)) if direct else None
        if not server:
            return None
    if clear:
        clear_selected(group_id, user_id)
        return ('操作目标已清除。未指定目标的在线查询会汇总两服；'
                '普通群消息仍同步两服。')
    if show:
        selected, _ = get_selected(group_id, user_id)
        current = (f'{server_prefix(selected)} {selected["name"]}'
                   if selected else '未设置（在线查询默认汇总两服）')
        names = [f'{server_prefix(item)} {item["name"]}' for item in list_servers()]
        return ('当前操作目标：' + current + '\n可选：' + '；'.join(names)
                + '\n切换：!怀旧 / !服 <名称>；清除：!服 自动'
                + '\n说明：只影响查询和管理，普通群消息仍同步两服。')
    if choice:
        server = resolve_server(choice.group(1))
    if not server:
        return f'{server_hint()} 目标不在允许清单。发送 !服 查看可选项。'
    selected = select_server(group_id, user_id, server['id'])
    return (f'{server_prefix(selected)} 操作目标已设为：{selected["name"]}\n'
            '只影响查询和管理；普通群消息仍同步两服。')


def _command_target(raw, group_id, user_id):
    explicit, cleaned = extract_server_selector(_normalize(raw))
    if explicit:
        return explicit, cleaned, True, ''
    selected, error = get_selected(group_id, user_id)
    if selected:
        return selected, cleaned, False, error or ''
    enabled = list_servers()
    if len(enabled) == 1:
        return enabled[0], cleaned, False, ''
    return None, cleaned, False, error or ''


def _high_risk_target_allowed(command, target, explicit):
    """Allow prefix omission only for the confirmation state machine on one server.

    ``cmd`` still calls ``_request_risk`` and never executes here.  Restart and
    stop deliberately remain excluded and continue to require an explicit target.
    """
    if explicit:
        return bool(target)
    if not target or len(list_servers()) != 1:
        return False
    return _rcon_ops.command_word(command) in {
        'cmd', '控制台', '确认', 'confirm', '取消确认', 'cancel'}


def _ai_operation_target(raw, group_id, user_id):
    """Resolve AI tools; a single enabled server is inherently unambiguous."""
    explicit, cleaned = extract_natural_server_selector(raw)
    if explicit:
        return explicit, cleaned, True
    selected, _error = get_selected(group_id, user_id)
    if selected:
        return selected, raw, False
    enabled = list_servers()
    if len(enabled) == 1:
        return enabled[0], raw, True
    return None, raw, False


def _server_query_backend(server):
    def routed(command):
        return query_server(server, command)
    routed.server_id = server['id']
    return routed

# ===== RCON 只读查询路由（!tps/!list/status，任何权限可查，秒回零 token）=====
_QUERY_PATTERNS = [
    (re.compile(r'^!?\s*tps\s*$', re.I), 'tps'),
    (re.compile(r'^!?\s*性能\s*$'), 'tps'),
    (re.compile(r'^!?\s*list\s*$', re.I), 'list'),
    (re.compile(r'^!?\s*在线\s*$'), 'list'),
    (re.compile(r'^!?\s*status\s*$', re.I), 'list'),
]

def _match_query(raw):
    _server, cleaned = extract_server_selector(_normalize(raw))
    for pat, kind in _QUERY_PATTERNS:
        if pat.search(cleaned):
            return kind
    return None


def _pending_natural_operation_reply(raw, user_id, privileged):
    """Route a plain “确认/取消” only when this admin actually owns a pending task.

    This keeps ambient group chat out of the operations path while making QQ's
    normal reply gesture work without forcing the user to mention Sanae again.
    """
    if not privileged:
        return False
    if not (_sanae_ai._has_confirmation_intent(raw)
            or _sanae_ai._has_cancellation_intent(raw)):
        return False
    return bool(_rcon_ops.pending_confirmation(str(user_id)))


def _natural_server_operation_candidate(raw, privileged, direct_call=False, at_call=False):
    """Route explicit live queries and admin imperatives away from social-only mode."""
    text = re.sub(r'\[CQ:[^\]]*\]', '', str(raw or ''), flags=re.I).strip()
    explicit, _cleaned = extract_natural_server_selector(raw)
    mentions_server = bool(explicit or re.search(
        r'(?i)(?:本服|服务器|怀旧服|服里|游戏里|Minecraft|\bMC\b|\bTPS\b)', text))
    if _sanae_ai._has_server_query_intent(raw):
        return bool(direct_call or at_call or mentions_server)
    # Admin mutations remain confirmation-gated in sanae_ai.  Avoid treating a
    # casual sentence containing a broad verb such as “给” as an operation:
    # without a direct call/server mention, require a command-shaped subject.
    command_subject = re.search(
        r'(?i)(?:\bop\b|管理(?:员|权限)|权限|封禁|解封|白名单|踢出|传送|召唤|'
        r'重载|重启|停服|备份|\bgive\b|\bfill\b|\bkill\b|\btp\b|\bban\b|'
        r'\bpardon\b|\bwhitelist\b|(?:^|\s)/[A-Za-z])', text)
    return bool(privileged and _sanae_ai._has_console_command_intent(raw) and
                (direct_call or at_call or mentions_server or command_subject))


def _is_ai_operation_turn(raw, privileged):
    """运维意图必须只使用当前消息，不能混入社交记忆和风格提示。"""
    return bool(
        _sanae_ai._has_server_query_intent(raw) or
        (privileged and (
            _sanae_ai._has_console_command_intent(raw) or
            _sanae_ai._has_confirmation_intent(raw) or
            _sanae_ai._has_cancellation_intent(raw))))

# ===== MC 百科查询（!wiki/!百科 → mcmod.cn）=====
_WIKI_PATTERNS = [
    re.compile(r'^!?\s*wiki\s+(.+)$', re.I),
    re.compile(r'^!?\s*百科\s+(.+)$'),
]
_WIKI_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'

def _match_wiki(raw):
    cleaned = _normalize(raw)
    for pat in _WIKI_PATTERNS:
        m = pat.search(cleaned)
        if m:
            return m.group(1).strip()[:30]
    return None

def _fetch_wiki(keyword):
    """抓 mcmod.cn 搜索页第一条结果，返回 (标题, 简介, 链接)；失败返回 None。"""
    try:
        url = 'https://search.mcmod.cn/s?key=' + urllib.parse.quote(keyword)
        req = urllib.request.Request(url, headers={'User-Agent': _WIKI_UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read().decode('utf-8', errors='replace')
        start = page.find('<div class="result-item">')
        if start < 0:
            return None
        end = page.find('<div class="result-item">', start + 1)
        item = page[start:end] if end > 0 else page[start:]
        mh = re.search(r'<a[^>]*href="(https?://www\.mcmod\.cn/class/\d+\.html|//www\.mcmod\.cn/class/\d+\.html)"[^>]*>(.*?)</a>', item, re.S)
        if not mh:
            return None
        link = mh.group(1)
        if link.startswith('//'):
            link = 'https:' + link
        title = html.unescape(re.sub(r'<[^>]+>', '', mh.group(2))).strip()
        mb = re.search(r'<div class="body">(.*?)</div>', item, re.S)
        intro = ''
        if mb:
            t = re.sub(r'<[^>]+>', '', mb.group(1))
            t = re.sub(r'\[h1=[^\]]*\]|\[ban:[^\]]*\]|\[mark:[^\]]*\]', '', t)
            t = html.unescape(t).replace('\xa0', ' ')
            intro = re.sub(r'\s+', ' ', t).strip()[:150]
        return title, intro, link
    except Exception as e:
        print(f'[wiki] 抓取失败: {e}', flush=True)
        return None


def _wiki_mod_candidates(keyword, title):
    """Return ordered search terms for existing Modrinth/CurseForge lookups."""
    candidates = [keyword, title]
    for value in re.findall(r'[（(]([^()（）]+)[)）]', title or ''):
        candidates.append(value)
    seen = set()
    return [value for value in candidates
            if value and not (value.casefold() in seen or seen.add(value.casefold()))]


def _wiki_mod_links(keyword, title):
    """Reuse mod_lookup sources to enrich a MC百科 hit with project links."""
    for query in _wiki_mod_candidates(keyword, title):
        hits = _mod_lookup.search_modrinth(query, limit=3)
        if not hits:
            continue
        hit = hits[0]
        cf = _mod_lookup.search_curseforge(hit.get('slug', ''), hit.get('title', ''))
        original_intro = hit.get('description', '').strip()
        translated_intro = _mod_lookup.translate(original_intro).strip()
        return {
            'intro': translated_intro if translated_intro != original_intro else '',
            'modrinth': hit.get('link', ''),
            'curseforge': (cf or {}).get('link', ''),
        }
    return {'intro': '', 'modrinth': '', 'curseforge': ''}


def _format_wiki_result(keyword, result):
    """Format !wiki with the same fields regardless of lookup source availability."""
    title, wiki_intro, wiki_link = result
    links = _wiki_mod_links(keyword, title)
    intro = links['intro'] or wiki_intro or '（暂无简介）'
    return '\n'.join([
        f'【模组百科】{title}',
        f'简介：{intro[:300]}',
        f'MC百科：{wiki_link or "未找到"}',
        f'CurseForge：{links["curseforge"] or "未找到"}',
        f'Modrinth：{links["modrinth"] or "未找到"}',
    ])

# ===== 模组查询（!mod/!模组 → Modrinth；!cf → CurseForge 官方搜索）=====
_MOD_PATTERNS = [
    re.compile(r'^!?\s*mod\s+(.+)$', re.I),
    re.compile(r'^!?\s*模组\s+(.+)$'),
]
_CF_PATTERNS = [
    re.compile(r'^!?\s*cf\s+(.+)$', re.I),
]

def _match_mod(raw):
    cleaned = _normalize(raw)
    for pat in _MOD_PATTERNS:
        m = pat.search(cleaned)
        if m:
            return m.group(1).strip()[:40]
    return None

def _match_cf(raw):
    cleaned = _normalize(raw)
    for pat in _CF_PATTERNS:
        m = pat.search(cleaned)
        if m:
            return m.group(1).strip()[:40]
    return None


def _binding_command(raw, uid, privileged):
    if not PLAYER_BINDINGS_ENABLED:
        return None
    text = _normalize(raw)
    if not text.startswith('!'):
        return None
    body = text[1:].strip()
    if body == '绑定查询' or body == '绑定':
        item = PLAYER_BINDINGS.get_qq(uid)
        return '当前绑定：' + item['name'] if item else '你还没有绑定游戏 ID。用法：!绑定 游戏ID'
    if body == '解绑':
        return PLAYER_BINDINGS.unbind(uid)[1]
    if body == '绑定列表' and privileged:
        rows = PLAYER_BINDINGS.list()
        return '绑定列表：\n' + '\n'.join(f'{row["qq"]} → {row["name"]}' for row in rows) if rows else '暂无绑定。'
    match = re.fullmatch(r'绑定\s+([A-Za-z0-9_]{1,16})', body)
    if match:
        return PLAYER_BINDINGS.bind(uid, match.group(1))[1]
    return None


def _media_command(raw, event, uid, privileged):
    """Deterministic media inspection without uploading or invoking a model."""
    text = _normalize(raw)
    if text not in ('!媒体', '!媒体信息'):
        return None
    rows = media_segments(event)
    if not rows:
        return '这条消息里没有识别到图片、语音、视频或文件。'
    return '媒体信息：' + '、'.join(str(row.get('type')) for row in rows)


def _image_host_command(raw, event, uid, privileged):
    if _normalize(raw) not in ('!转图床', '!上传图床'):
        return None
    images = media_segments(event, ('image',))
    if not images:
        return '请引用一条包含图片或表情包的消息后再发送 !转图床。'
    if not privileged:
        return '只有管理员可以使用图床转存。'
    results = []
    for item in images[:5]:
        url = item.get('url') or item.get('file')
        if not url or not str(url).startswith(('http://', 'https://')):
            continue
        try:
            data, content_type = _download(url, 12 * 1024 * 1024)
            hosted, status = image_host_upload(data, content_type.split(';', 1)[0])
            if hosted:
                results.append(hosted)
            elif status != 'image host disabled':
                results.append('转存失败：' + status[:120])
        except Exception as exc:
            results.append('下载失败：' + str(exc)[:120])
    if not results:
        return '图床未启用或图片地址不可用；没有向外部服务上传图片。'
    return '图床链接：\n' + '\n'.join(results)


def _media_ai_requested(raw, event):
    text = _normalize(raw)
    has_media = bool(media_segments(event, ('record', 'audio', 'video')))
    return has_media and (text in ('!转写', '!语音转写', '!听语音') or '早苗' in text or '[CQ:at' in raw)


def _chat_relay_enabled():
    try:
        with open(CHAT_RELAY_STATE, encoding='utf-8') as stream:
            return bool(json.load(stream).get('enabled', True))
    except (OSError, ValueError):
        return True


def _set_chat_relay(enabled, uid):
    os.makedirs(_os.path.dirname(CHAT_RELAY_STATE), exist_ok=True)
    with _CHAT_RELAY_LOCK:
        temp = CHAT_RELAY_STATE + '.tmp'
        with open(temp, 'w', encoding='utf-8') as stream:
            json.dump({'enabled': bool(enabled), 'updatedBy': str(uid),
                       'updatedAt': int(time.time())}, stream)
        _os.replace(temp, CHAT_RELAY_STATE)

# ===== 重复回收（15 秒窗口）=====
_DUP_WINDOW = 15
_dup_ring = collections.deque(maxlen=200)

def _is_duplicate(raw, source=''):
    fp = str(source) + '\0' + _normalize(raw).lower()
    now = time.time()
    for old_fp, old_ts in _dup_ring:
        if old_fp == fp and now - old_ts < _DUP_WINDOW:
            return True
    _dup_ring.append((fp, now))
    return False

# ===== 群消息发送（统一走早苗 API）=====
def send_group_msg(text, api=WIKI_API):
    try:
        payload = json_bytes({"group_id": LLBOT_GROUP, "message": text})
        req = urllib.request.Request(
            api + "/send_group_msg", data=payload,
            headers={"Content-Type": JSON_CONTENT_TYPE}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = utf8_text(resp.read())[:500]
        ok, _ = onebot_success(raw)
        if not ok:
            print(f"[回执失败] OneBot业务失败: {raw[:200]}", flush=True)
        return ok
    except Exception as e:
        print(f"[回执失败] {e}", flush=True)
        return False

def send_private_msg(qq, text):
    try:
        payload = json_bytes({"user_id": int(qq), "message": text})
        req = urllib.request.Request(
            WIKI_API + "/send_private_msg", data=payload,
            headers={"Content-Type": JSON_CONTENT_TYPE}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = utf8_text(resp.read())[:500]
        ok, _ = onebot_success(raw)
        if not ok:
            print(f"[私聊回执失败] OneBot业务失败: {raw[:200]}", flush=True)
        return ok
    except Exception as e:
        print(f"[私聊转发失败] {e}", flush=True)
        return False


def _qq_capability_command(raw):
    """Parse explicit, admin-only QQ capability commands."""
    text = _normalize(raw)
    patterns = (
        (r'^!qq(?:状态|登录状态)$', lambda m: ('status', {})),
        (r'^!qq(?:群列表|群组列表)$', lambda m: ('groups', {})),
        (r'^!qq群成员(?:\s+(\d+))?$', lambda m: ('members', {'group_id': m.group(1)})),
        (r'^!qq(?:群历史|历史)(?:\s+(\d+))?(?:\s+(\d+))?$',
         lambda m: ('history', {'group_id': m.group(1), 'count': m.group(2)})),
        (r'^!qq发群\s+(.+)$', lambda m: ('send_group', {'text': m.group(1)})),
        (r'^!qq私聊\s+(\d+)\s+(.+)$', lambda m: ('send_private', {'user_id': m.group(1), 'text': m.group(2)})),
        (r'^!qq拍一拍(?:\s+(\d+))?$', lambda m: ('poke', {'user_id': m.group(1)})),
        (r'^!qq引用\s+(\d+)\s+(.+)$', lambda m: ('reply', {'message_id': m.group(1), 'text': m.group(2)})),
        (r'^!qq表情\s+(\d+)$', lambda m: ('face', {'face_id': m.group(1)})),
    )
    for pattern, builder in patterns:
        match = re.match(pattern, text, re.I | re.S)
        if match:
            return builder(match)
    return None


def _qq_capability_thread(event, action, args, privileged):
    """Execute one explicit QQ action and return a compact report."""
    if not privileged:
        send_group_msg('QQ 管理能力仅群主/管理员可用。', api=WIKI_API)
        return
    gid = str(args.get('group_id') or event.get('group_id') or LLBOT_GROUP)
    try:
        if action == 'status':
            status = QQ_ACTIONS.status() or {}
            login = QQ_ACTIONS.login_info() or {}
            online = status.get('online') if isinstance(status, dict) else None
            nickname = login.get('nickname') if isinstance(login, dict) else None
            send_group_msg(f'QQ 在线：{online!s}，昵称：{nickname or "未知"}', api=WIKI_API)
        elif action == 'groups':
            groups = QQ_ACTIONS.groups() or []
            lines = [f"{g.get('group_name') or '未命名'} ({g.get('group_id')})"
                     for g in groups[:50] if isinstance(g, dict)]
            send_group_msg('群列表（最多 50 个）：\n' + ('\n'.join(lines) or '暂无数据'), api=WIKI_API)
        elif action == 'members':
            members = QQ_ACTIONS.members(args.get('group_id') or gid) or []
            lines = [f"{m.get('card') or m.get('nickname') or m.get('user_id')}"
                     for m in members[:100] if isinstance(m, dict)]
            send_group_msg(f'群成员 {len(members)} 人（展示前 {min(100, len(members))}）：\n' +
                           ('、'.join(lines) or '暂无数据'), api=WIKI_API)
        elif action == 'history':
            history = QQ_ACTIONS.history(args.get('group_id') or gid, args.get('count') or 30) or []
            lines = []
            for item in history[-20:]:
                if not isinstance(item, dict):
                    continue
                sender = (item.get('sender') or {}).get('card') or (item.get('sender') or {}).get('nickname') or item.get('user_id') or '?'
                message = re.sub(r'\[CQ:[^\]]*\]', '[媒体]', str(item.get('raw_message') or item.get('message') or ''))
                lines.append(f'{sender}：{message[:180]}')
            send_group_msg('最近群消息：\n' + ('\n'.join(lines) or '暂无数据'), api=WIKI_API)
        elif action == 'send_group':
            QQ_ACTIONS.send_group(gid, args['text'])
            send_group_msg('QQ 群消息已发送。', api=WIKI_API)
        elif action == 'send_private':
            QQ_ACTIONS.send_private(args['user_id'], args['text'])
            send_group_msg('QQ 私聊消息已发送。', api=WIKI_API)
        elif action == 'poke':
            QQ_ACTIONS.poke(args.get('user_id') or event.get('user_id'), gid)
            send_group_msg('已执行拍一拍。', api=WIKI_API)
        elif action == 'reply':
            payload = [{'type': 'reply', 'data': {'id': int(args['message_id'])}},
                       {'type': 'text', 'data': {'text': args['text']}}]
            QQ_ACTIONS.send_group(gid, payload)
            send_group_msg('引用回复已发送。', api=WIKI_API)
        elif action == 'face':
            QQ_ACTIONS.send_group(gid, [{'type': 'face', 'data': {'id': str(args['face_id'])}}])
            send_group_msg('表情已发送。', api=WIKI_API)
    except Exception as exc:
        print(f'[qq-capability] {action} failed: {type(exc).__name__}', flush=True)
        send_group_msg(f'QQ 操作暂不可用：{action}（{type(exc).__name__}）。', api=WIKI_API)


class BridgeHandler(BaseHTTPRequestHandler):
    def _read_body(self, max_bytes=2 * 1024 * 1024):
        """支持 Content-Length 和 chunked 两种编码。"""
        if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
            body = b''
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                try:
                    size = int(line, 16)
                except ValueError:
                    break
                if size == 0:
                    while self.rfile.readline().strip():
                        pass
                    break
                if len(body) + size > max_bytes:
                    raise ValueError('request body too large')
                body += self.rfile.read(size)
                self.rfile.readline()
            return body
        length = int(self.headers.get('Content-Length', 0))
        if length < 0 or length > max_bytes:
            raise ValueError('request body too large')
        return self.rfile.read(length) if length else b''

    def _authorized(self):
        if CALLBACK_SOURCE_IPS and self.client_address[0] not in CALLBACK_SOURCE_IPS:
            return False
        if CALLBACK_TOKEN:
            supplied = self.headers.get('X-OneBot-Token', '')
            return supplied == CALLBACK_TOKEN
        return True

    def do_POST(self):
        if not self._authorized():
            self._respond(403, '{"status":"error","retcode":403,"message":"forbidden"}')
            return
        try:
            body = self._read_body()
        except (ValueError, TypeError):
            self._respond(413, '{"status":"error","retcode":413,"message":"request too large"}')
            return
        try:
            event = json.loads(utf8_text(body))
        except Exception:
            event = {}
        print('[recv] post={} type={} group={}'.format(
            event.get('post_type', ''), event.get('message_type', ''),
            event.get('group_id', '')), flush=True)

        # 私聊消息：转发给狗蛋
        if event.get('post_type') == 'message' and event.get('message_type') == 'private':
            uid = str(event.get('user_id', ''))
            if uid != str(event.get('self_id', '')):
                threading.Thread(target=self._forward_private, args=(event,), daemon=True).start()
            self._respond(200, '{"status":"ok"}')
            return

        # 群消息
        if event.get('post_type') == 'message' and event.get('message_type') == 'group':
            gid = event.get('group_id')
            is_bot_self = (event.get('user_id') == event.get('self_id'))
            is_wiki_bot = (str(event.get('user_id', '')) == WIKI_BOT_QQ)
            if gid == LLBOT_GROUP and not is_bot_self and not is_wiki_bot:
                raw = str(event.get('raw_message', ''))
                uid = str(event.get('user_id', ''))
                privileged = uid in OWNERS or uid in ADMINS

                # 0) 重复回收。明确点名是用户主动召唤，不应被普通消息去重吞掉。
                direct_name_candidate = self._is_direct_name_call(event)
                at_candidate = self._is_at_bot(event)
                pending_operation_candidate = _pending_natural_operation_reply(
                    raw, uid, privileged)
                natural_operation_candidate = _natural_server_operation_candidate(
                    raw, privileged, direct_name_candidate, at_candidate)
                fun_mute_reason = _fun_mute_reason(event)
                if (_is_duplicate(raw, uid) and
                        not (direct_name_candidate or at_candidate or
                             pending_operation_candidate or natural_operation_candidate or
                             fun_mute_reason)):
                    self._respond(200, '{"status":"ok"}')
                    return

                # TPS/在线查询必须独立于 AI、黑话、记忆和贴纸状态。
                # 即使社交持久化故障，也应能直接走只读 RCON 并回群。
                qk = _match_query(raw)
                if qk:
                    try:
                        target, _cleaned, _explicit, _error = _command_target(raw, gid, uid)
                    except Exception as exc:
                        print(f'[rcon] 读取操作目标失败: {type(exc).__name__}', flush=True)
                        target = None
                    threading.Thread(target=self._rcon_query_and_reply,
                                     args=(qk, target), daemon=True).start()
                    print(f"[rcon] 查询: user={uid} type={qk} target="
                          f"{target['id'] if target else 'all'}", flush=True)
                    self._respond(200, '{"status":"ok"}')
                    return

                # Long-term archive stores sanitized group activity; aggregate
                # memory stores topics only and never raw text.
                sender = event.get('sender') or {}
                nick = sender.get('card') or sender.get('nickname') or uid
                archive_text = _normalize(raw)
                if re.match(r'^[!！](?:qq(?:私聊|发群|引用)|cmd|rcon|配置|改配置|知识库记住)\b', archive_text):
                    archive_text = '[管理员 QQ 动作]'
                try:
                    SOCIAL_ARCHIVE.append(gid, event.get('group_name') or str(gid), nick, archive_text)
                    SOCIAL_MEMORY.observe(event)
                except Exception as exc:
                    print(f'[social-memory] archive failed: {type(exc).__name__}', flush=True)

                # Local zero-token easter egg moderation for one configured
                # member in one configured group.  Consume the message here so
                # it cannot also wake the model or trigger stickers.
                if fun_mute_reason:
                    threading.Thread(target=_fun_mute_thread,
                                     args=(event, fun_mute_reason), daemon=True).start()
                    self._respond(200, '{"status":"ok"}')
                    return

                # 只记录本群语料；黑话必须由管理员显式确认后才会注入 AI。
                # 语料状态是可选持久化，写入失败不能阻断正常收消息/回复。
                try:
                    SLANG_LEARNER.observe(event)
                    slang_reply = SLANG_LEARNER.command(raw, privileged)
                except Exception as exc:
                    print(f'[slang] state write/read failed: {type(exc).__name__}', flush=True)
                    slang_reply = None
                if slang_reply:
                    threading.Thread(target=lambda t=slang_reply: send_group_msg(t, api=WIKI_API), daemon=True).start()
                    self._respond(200, '{"status":"ok"}')
                    return

                feedback_reply = FEEDBACK_STORE.command(raw, uid, privileged=privileged)
                if feedback_reply:
                    threading.Thread(target=lambda t=feedback_reply: send_group_msg(t, api=WIKI_API), daemon=True).start()
                    self._respond(200, '{"status":"ok"}')
                    return

                sticker_reply = STICKER_CATALOG.command(raw, privileged=privileged)
                if sticker_reply:
                    threading.Thread(target=lambda t=sticker_reply: send_group_msg(t, api=WIKI_API), daemon=True).start()
                    self._respond(200, '{"status":"ok"}')
                    return

                qq_capability = _qq_capability_command(raw)
                if qq_capability:
                    action, args = qq_capability
                    threading.Thread(target=_qq_capability_thread,
                                     args=(event, action, args, privileged), daemon=True).start()
                    self._respond(200, '{"status":"ok"}')
                    return

                # 操作目标只约束查询和管理；普通群文本始终 fan-out。
                selection_reply = _selection_reply(raw, gid, uid)
                if selection_reply:
                    send_group_msg(selection_reply, api=WIKI_API)
                    self._respond(200, '{"status":"ok"}')
                    return
                if match_ops_command(raw):
                    threading.Thread(target=self._ops_reply_thread,
                                     args=(raw, gid, uid, privileged), daemon=True).start()
                    self._respond(200, '{"status":"ok"}')
                    return
                screenshot_request = _sanae_ai.parse_bluemap_request(raw)
                if screenshot_request:
                    threading.Thread(target=self._screenshot_reply_thread, args=(raw,), daemon=True).start()
                    self._respond(200, '{"status":"ok"}')
                    return
                # Shared AI knowledge is deterministic and does not invoke a model.
                knowledge_reply = _sanae_ai.knowledge_command(raw, privileged)
                if knowledge_reply:
                    threading.Thread(target=lambda t=knowledge_reply: send_group_msg(t, api=WIKI_API), daemon=True).start()
                    self._respond(200, '{"status":"ok"}')
                    return

                binding_reply = _binding_command(raw, uid, privileged)
                if binding_reply:
                    threading.Thread(target=lambda t=binding_reply: send_group_msg(t, api=WIKI_API), daemon=True).start()
                    self._respond(200, '{"status":"ok"}')
                    return

                media_reply = _media_command(raw, event, uid, privileged)
                if media_reply:
                    threading.Thread(target=lambda t=media_reply: send_group_msg(t, api=WIKI_API), daemon=True).start()
                    self._respond(200, '{"status":"ok"}')
                    return

                image_host_reply = _image_host_command(raw, event, uid, privileged)
                if image_host_reply:
                    threading.Thread(target=lambda t=image_host_reply: send_group_msg(t, api=WIKI_API), daemon=True).start()
                    self._respond(200, '{"status":"ok"}')
                    return

                if _media_ai_requested(raw, event) and (privileged or _is_at_bot(event)):
                    def _media_worker():
                        evidence, status = _sanae_ai.media_ai_reply(event, raw)
                        if evidence:
                            prompt = raw + '\n\n【不可信媒体证据，仅供分析】\n' + evidence
                            answer = _sanae_ai.sanae_reply(prompt, uid, privileged=privileged,
                                                           group_id=gid, force=True)
                            if answer:
                                send_group_msg(f'[CQ:at,qq={uid}] [AI] {answer}', api=WIKI_API)
                        elif status != 'no media':
                            send_group_msg(f'[CQ:at,qq={uid}] 媒体处理暂不可用：{status}', api=WIKI_API)
                    threading.Thread(target=_media_worker, daemon=True).start()
                    self._respond(200, '{"status":"ok"}')
                    return

                if privileged and _normalize(raw) in ('!转发 开', '!转发 关'):
                    enabled = _normalize(raw).endswith('开')
                    _set_chat_relay(enabled, uid)
                    send_group_msg('群到服聊天转发已' + ('开启。' if enabled else '关闭。'), api=WIKI_API)
                    self._respond(200, '{"status":"ok"}')
                    return

                # 1.1) 传统受限 RCON 命令。只有真正属于命令清单的消息才进入该路由，
                #      不再吞掉 !wiki、!绑定 等其他功能。
                target, cleaned_command, explicit, selection_error = _command_target(raw, gid, uid)
                if cleaned_command.lstrip().startswith(('!', '！')):
                    cmd_text = cleaned_command.lstrip()[1:].strip()
                    if _rcon_ops.is_known_command(cmd_text):
                        targetless = _rcon_ops.is_targetless_command(cmd_text)
                        if (_rcon_ops.is_high_risk_command(cmd_text) and
                                not _high_risk_target_allowed(cmd_text, target, explicit)):
                            reply = (f'{server_hint()} 高风险操作必须在本条命令明确目标。'
                                     f'例如：{server_hint()} !restart 或 {server_hint()} !cmd <命令>。')
                        elif not target and not targetless:
                            reply = (f'{server_hint()} 尚未设置操作目标。发送 !服 查看当前可用服；'
                                     f'也可以在本条命令中加 {server_hint()}。')
                        else:
                            backend = _server_query_backend(target) if target else None
                            reply = _rcon_ops.dispatch(
                                cmd_text, uid, uid, privileged,
                                query_fn=backend, server=target)
                            if not reply:
                                reply = '拒绝：该命令需要群主或管理员权限。'
                            if target:
                                reply = f'{server_prefix(target)} {reply}'
                                if '[高危确认]' in reply and len(list_servers()) > 1:
                                    reply = reply.replace(
                                        '发送：!确认',
                                        f'发送：{server_prefix(target)} !确认')
                        _rcon_ops.record_command_result(cmd_text, uid, reply)
                        threading.Thread(target=lambda t=reply: send_group_msg(t, api=WIKI_API), daemon=True).start()
                        print(f"[rcon] 反控命令: user={uid} cmd={cmd_text[:40]} "
                              f"target={target['id'] if target else 'none'} privileged={privileged}", flush=True)
                        self._respond(200, '{"status":"ok"}')
                        return

                # 1.2) !问 <问题> → 早苗 AI（仅群主/管理员，对齐工具包）
                m_ask = re.match(r'^[!！]\s*问\s+(.+)$', raw, re.S)
                if m_ask:
                    priv = (str(uid) in OWNERS or str(uid) in ADMINS)
                    if not priv:
                        send_group_msg('[CQ:at,qq={}] AI 问答仅群主/管理员可用，群友可以 @我 聊天哦~'.format(uid), api=WIKI_API)
                        self._respond(200, '{"status":"ok"}')
                        return
                    threading.Thread(target=self._sanae_reply_thread, args=(event, True), daemon=True).start()
                    print(f"[sanae] !问: user={uid}", flush=True)
                    self._respond(200, '{"status":"ok"}')
                    return

                # 2) MC 百科查询（!wiki/!百科）
                kw = _match_wiki(raw)
                if kw:
                    threading.Thread(target=self._wiki_query_and_reply, args=(kw,), daemon=True).start()
                    print(f"[wiki] 查询: user={uid} kw={kw}", flush=True)
                    self._respond(200, '{"status":"ok"}')
                    return

                # 3) 模组查询（!mod/!模组 → Modrinth；!cf → CurseForge 官方）
                mk = _match_mod(raw)
                if mk:
                    threading.Thread(target=self._mod_query_and_reply, args=(mk, 'modrinth'), daemon=True).start()
                    print(f"[mod] 查询: user={uid} kw={mk}", flush=True)
                    self._respond(200, '{"status":"ok"}')
                    return
                ck = _match_cf(raw)
                if ck:
                    threading.Thread(target=self._mod_query_and_reply, args=(ck, 'cf'), daemon=True).start()
                    print(f"[mod] CF查询: user={uid} kw={ck}", flush=True)
                    self._respond(200, '{"status":"ok"}')
                    return

                # 4) @早苗/自然点名 → 独立 AI 模块（不走 agent）
                # 除了 OneBot 的 at 段，也接住“早苗，…”、“早苗？”这类自然点名。
                # 仅限消息开头点名，避免普通聊天里提到角色名就抢答。
                direct_name_call = direct_name_candidate
                if pending_operation_candidate:
                    threading.Thread(target=self._sanae_reply_thread,
                                     args=(event, True), daemon=True).start()
                    print(f"[sanae] 待确认操作自然回复: user={uid}", flush=True)
                    self._respond(200, '{"status":"ok"}')
                    return
                if natural_operation_candidate:
                    threading.Thread(target=self._sanae_reply_thread,
                                     args=(event, True), daemon=True).start()
                    print(f"[sanae] 自然服务器操作: user={uid}", flush=True)
                    self._respond(200, '{"status":"ok"}')
                    return
                if at_candidate:
                    threading.Thread(target=self._sanae_reply_thread, args=(event,), daemon=True).start()
                    print(f"[sanae] @早苗: user={uid}", flush=True)
                    self._respond(200, '{"status":"ok"}')
                    return
                if direct_name_call and SOCIAL_LITE_ENABLED and gid == SOCIAL_LITE_GROUP:
                    try:
                        scheduled, reason = SOCIAL_LITE.maybe_schedule(
                            event, self._social_reply_thread,
                            delay_seconds=float(_os.environ.get('SOCIAL_LITE_DEBOUNCE', '2.5')))
                    except Exception as exc:
                        # 状态文件权限/短暂 I/O 故障不能让明确点名变成无响应。
                        print(f'[sanae] 点名社交调度失败: {type(exc).__name__}', flush=True)
                        threading.Thread(
                            target=lambda: send_group_msg(
                                f'[CQ:at,qq={uid}] 我在，刚才没接稳。把要接的话再发一次，我马上接上。',
                                api=WIKI_API), daemon=True).start()
                        self._respond(200, '{"status":"ok"}')
                        return
                    print(f"[sanae] 点名早苗转社交上下文: user={uid} scheduled={scheduled} reason={reason}", flush=True)
                    self._respond(200, '{"status":"ok"}')
                    return

                # Social Lite 只在配置群启用；本地门控后再单轮唤醒模型。
                if SOCIAL_LITE_ENABLED and gid == SOCIAL_LITE_GROUP:
                    try:
                        scheduled, reason = SOCIAL_LITE.maybe_schedule(
                            event, self._social_reply_thread,
                            delay_seconds=float(_os.environ.get('SOCIAL_LITE_DEBOUNCE', '2.5')))
                        if scheduled:
                            print(f'[social-lite] scheduled group={gid} reason={reason}', flush=True)
                    except Exception as exc:
                        print(f'[social-lite] state write/read failed: {type(exc).__name__}', flush=True)

                # 5) 群→服 G2S 全量转发（非 bot 消息都 say 进游戏）
                if RELAY_G2S_ENABLED and _chat_relay_enabled():
                    threading.Thread(target=self._relay_g2s, args=(event,), daemon=True).start()

            self._respond(200, '{"status":"ok"}')
            return

        # 其他事件（通知等）：静默
        self._respond(200, '{"status":"ok"}')

    def _forward_private(self, event):
        """私聊消息转发给狗蛋。"""
        uid = str(event.get('user_id', ''))
        raw = str(event.get('raw_message', ''))
        nickname = (event.get('sender') or {}).get('nickname', '') or uid
        text = re.sub(r'\[CQ:image,[^\]]*\]', '[图片]', raw)
        text = re.sub(r'\[CQ:[^\]]*\]', '[CQ码]', text)
        msg = f"[私聊] {nickname}({uid}): {text}"
        for owner in OWNERS:
            if send_private_msg(owner, msg):
                print(f"[private→狗蛋] user={uid} msg={raw[:50]}", flush=True)
            else:
                print(f"[private→狗蛋失败] user={uid} msg={raw[:50]}", flush=True)

    def _wiki_query_and_reply(self, keyword):
        """!wiki: MC百科 primary result enriched by existing mod_lookup sources."""
        try:
            result = _fetch_wiki(keyword)
        except Exception:
            result = None
        if not result:
            send_group_msg(f'百科里没找到「{keyword}」，换个关键词试试~', api=WIKI_API)
            return
        msg = _format_wiki_result(keyword, result)
        send_group_msg(msg, api=WIKI_API)

    def _mod_query_and_reply(self, keyword, source='modrinth'):
        """!mod → Modrinth；!cf → CurseForge 官方搜索。"""
        try:
            msg = _mod_lookup.lookup_cf(keyword) if source == 'cf' else _mod_lookup.lookup_mod(keyword)
        except Exception as e:
            print(f'[mod] 查询异常: {e}', flush=True)
            msg = f'模组查询出错了（{e}），稍后再试~'
        if not msg:
            msg = f'模组站没找到「{keyword}」，换个关键词试试~'
        send_group_msg(msg, api=WIKI_API)

    def _sanae_reply_thread(self, event, force=False):
        """@早苗 / !问 独立回复：正则匹配 → DeepSeek API（带工具）→ 早苗 API 发回（完全不走 agent）。"""
        try:
            raw = str(event.get('raw_message', ''))
            uid = event.get('user_id')
            nick = ((event.get('sender') or {}).get('card') or (event.get('sender') or {}).get('nickname') or '')
            gid = event.get('group_id', LLBOT_GROUP)
            privileged = str(uid) in OWNERS or str(uid) in ADMINS

            def _interim(m):
                try:
                    send_group_msg(f'[AI] {m}', api=WIKI_API)
                except Exception as e:
                    print(f'[sanae] interim 失败: {e}', flush=True)

            selected_server, raw, target_unambiguous = _ai_operation_target(raw, gid, uid)
            # 运维工具的意图识别必须基于干净的当前消息。把长期记忆、黑话或
            # 风格策略拼进来，会让其中的“解释/说明”等词误关掉 run_rcon。
            if gid == SOCIAL_LITE_GROUP and not _is_ai_operation_turn(raw, privileged):
                extras = ['\n〖回复策略〗' + FEEDBACK_STORE.strategy_hint()]
                slang_context = SLANG_LEARNER.context()
                if slang_context:
                    extras.append(slang_context)
                memory_context = SOCIAL_MEMORY.context()
                if memory_context:
                    extras.append(memory_context)
                style_hint = SOCIAL_LITE.expression_style_hint(gid, event)
                if style_hint:
                    extras.append(style_hint)
                sticker_rows = STICKER_CATALOG.recommend(raw, limit=3)
                if sticker_rows:
                    extras.append('〖可选贴纸语义〗' + '、'.join(dict.fromkeys(x['mood'] for x in sticker_rows)) + '（仅供内部选择）')
                raw += '\n\n' + '\n'.join(extras)
            reply = _sanae_ai.sanae_reply(raw, uid, nick, privileged=privileged,
                                          group_id=gid, send_cb=_interim, force=force,
                                          selected_server=selected_server,
                                          explicit_server=target_unambiguous)
            if reply:
                msg = f'[CQ:at,qq={uid}] [AI] {reply}'
                recommended = STICKER_CATALOG.recommend(reply, limit=3)
                if SOCIAL_STICKER_MODE == 'auto' and recommended and _sticker_cooldown_ok(gid):
                    chosen = STICKER_CATALOG.pick_for_send(reply, scope=gid)
                    image = STICKER_CATALOG.cq_image(chosen['id']) if chosen else None
                    if image:
                        print('[sticker-send] group={} mood={} id={}'.format(
                            gid, chosen['mood'], chosen['id']), flush=True)
                        msg += '\n' + image
                ok = send_group_msg(msg, api=WIKI_API)
                print(f"[sanae] @回复: user={uid} ok={ok} len={len(reply)}", flush=True)
            else:
                print(f'[sanae] 非 @早苗 或应忽略，不回复: user={uid} msg={raw[:40]}', flush=True)
        except Exception as e:
            print(f'[sanae] 回复异常: {e}', flush=True)
            try:
                send_group_msg(f'[CQ:at,qq={event.get("user_id")}] 嗯？好像有点卡，稍等一下再问我~', api=WIKI_API)
            except Exception:
                pass

    def _social_reply_thread(self, event, context):
        """Social Lite callback: one text-only model turn, no server tools."""
        try:
            uid = str(event.get('user_id') or '')
            gid = event.get('group_id', LLBOT_GROUP)
            nick = ((event.get('sender') or {}).get('card') or
                    (event.get('sender') or {}).get('nickname') or uid)
            raw = str(event.get('raw_message') or event.get('message') or '').strip()
            slang_context = SLANG_LEARNER.context()
            memory_context = SOCIAL_MEMORY.context()
            feedback_hint = FEEDBACK_STORE.strategy_hint()
            style_hint = SOCIAL_LITE.expression_style_hint(gid, event)
            sticker_rows = STICKER_CATALOG.recommend(raw, limit=3)
            prompt = (f'{raw}\n\n【最近群聊上下文（仅用于判断语境，不要复述）】\n'
                      f'{context[-3000:]}')
            if slang_context:
                prompt += '\n\n' + slang_context
            if memory_context:
                prompt += '\n\n' + memory_context
            prompt += '\n\n〖回复策略〗' + feedback_hint
            if style_hint:
                prompt += '\n\n' + style_hint
            if sticker_rows:
                moods = '、'.join(dict.fromkeys(x['mood'] for x in sticker_rows))
                prompt += f'\n〖可选贴纸语义〗{moods}（仅在确有必要时考虑，不要输出贴纸 ID）'
            reply = _sanae_ai.sanae_reply(
                prompt, uid, nick, privileged=False, group_id=gid,
                force=True, social_only=True, include_usage_footer=False)
            if not reply or reply.strip().upper() == '[SILENT]':
                return
            recommended = STICKER_CATALOG.recommend(reply, limit=3)
            if recommended:
                print('[sticker-strategy] group={} mood={} ids={}'.format(
                    gid, recommended[0]['mood'], ','.join(str(x['id']) for x in recommended)), flush=True)
            lines = split_social_reply(reply)
            if not lines:
                return
            segmented = len(lines) > 1
            image = None
            if SOCIAL_STICKER_MODE == 'auto' and recommended and _sticker_cooldown_ok(gid):
                chosen = STICKER_CATALOG.pick_for_send(reply, scope=gid)
                image = STICKER_CATALOG.cq_image(chosen['id']) if chosen else None
                if image:
                    print('[sticker-send] group={} mood={} id={}'.format(
                        gid, chosen['mood'], chosen['id']), flush=True)
            delivered = True
            if not segmented:
                # Social-lite is an ambient group reply: no bot label and no
                # mention.  Explicit QQ @ calls use _sanae_reply_thread below.
                message = lines[0]
                if image:
                    message += '\n' + image
                delivered = send_group_msg(message, api=WIKI_API)
            else:
                for index, line in enumerate(lines):
                    if not send_group_msg(line, api=WIKI_API):
                        delivered = False
                        break
                    if index < len(lines) - 1:
                        # Keep the cadence legible without turning a single reply
                        # into a burst that trips OneBot rate limits.
                        time.sleep(0.20)
                if image and delivered:
                    delivered = send_group_msg(image, api=WIKI_API)
            if delivered:
                SOCIAL_LITE.record_outgoing(gid, reply.strip())
                print(f'[social-lite] replied group={gid} user={uid} len={len(reply)} '
                      f'segmented={segmented} parts={len(lines)}', flush=True)
        except Exception as exc:
            print(f'[social-lite] callback failed: {type(exc).__name__}', flush=True)
            try:
                send_group_msg(
                    f'[CQ:at,qq={event.get("user_id")}] 我在，刚才没接稳。把要接的话再发一次，我马上接上。',
                    api=WIKI_API)
            except Exception:
                pass

    def _screenshot_reply_thread(self, raw):
        try:
            reply = _sanae_ai.handle_bluemap_request(raw)
            if reply:
                send_group_msg(reply, api=WIKI_API)
        except Exception as exc:
            print(f'{server_hint()} [screenshot 错误] {type(exc).__name__}: {exc}', flush=True)
            send_group_msg(f'{server_hint()} 截图通路暂不可用，请稍后再试。', api=WIKI_API)

    def _ops_reply_thread(self, raw, group_id, user_id, privileged):
        """Deterministic ops query; all output comes from auditable local artifacts."""
        parsed = match_ops_command(raw)
        selected = parsed.get('server') if parsed else None
        if not selected:
            selected, _ = get_selected(group_id, user_id)
        reply = dispatch_ops_command(raw, group_id, user_id, privileged,
                                     query_fn=query_server)
        if not reply:
            return
        message = reply.text
        if reply.image_path:
            message += f'\n[CQ:image,file={reply.image_path}]'
        ok = send_group_msg(message, api=WIKI_API)
        print(f'[ops-query] command={str(raw)[:40]} ok={ok} image={bool(reply.image_path)}',
              flush=True)

    def _relay_g2s(self, event):
        """群→双服逐服 best-effort 转发；一服失败不影响另一服。"""
        raw = str(event.get('raw_message', ''))
        text = re.sub(r'\[CQ:[^\]]*\]', '', raw).strip()
        if not text:
            return
        sender = event.get('sender', {}) or {}
        nick = sender.get('card') or sender.get('nickname') or str(event.get('user_id', ''))
        for result in fanout_group_message(nick, text):
            prefix = server_prefix(result['server'])
            if result['ok']:
                print(f'[relay-g2s] {prefix} ok nick={str(nick)[:64]} chars={len(text)}', flush=True)
            else:
                print(f'[relay-g2s] {prefix} failed {result["error"]}', flush=True)

    def _rcon_query_and_reply(self, query_kind, server=None):
        """RCON 直查并回群；未指定操作目标时汇总两服。"""
        replies = []
        for item in ([server] if server else list_servers()):
            prefix = server_prefix(item)
            try:
                command = 'tps' if query_kind == 'tps' else 'list'
                result = _rcon_ops.dispatch(
                    command, '', '', True,
                    query_fn=_server_query_backend(item), server=item)
                replies.append(prefix + ' ' + result)
            except Exception as exc:
                print(f"[rcon] {prefix} 查询失败: {type(exc).__name__}: {exc}", flush=True)
                replies.append(prefix + ' RCON 当前不可达，暂时无法查询。')
        send_group_msg('\n\n'.join(replies), api=WIKI_API)

    def _is_at_bot(self, event):
        """消息是否 @ 了早苗本人（或 @全体）。"""
        # 某些 OneBot 上报在重连窗口会缺少 self_id，使用已确认的早苗 QQ 号兜底。
        self_id = str(event.get('self_id') or WIKI_BOT_QQ)
        msg = event.get('message')
        if isinstance(msg, list):
            for seg in msg:
                if isinstance(seg, dict) and seg.get('type') == 'at':
                    qq = str(seg.get('data', {}).get('qq', ''))
                    if qq == self_id or qq == 'all':
                        return True
        raw = str(event.get('raw_message', ''))
        if self_id and (f'[CQ:at,qq={self_id}]' in raw or f'[CQ:at,qq={self_id},' in raw):
            return True
        if '[CQ:at,qq=all]' in raw:
            return True
        return False

    def _is_direct_name_call(self, event):
        """识别不带 CQ at 的自然点名，例如“早苗，看看这个”。"""
        raw = str(event.get('raw_message', '') or '')
        if not raw or self._is_at_bot(event):
            return False
        text = re.sub(r'\[CQ:[^\]]*\]', '', raw, flags=re.I).strip()
        return bool(re.match(r'^早苗(?:\s|[，,：:。！？!?、]|$)', text))

    def _respond(self, code, body):
        data = str(body).lstrip('\ufeff').encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', JSON_CONTENT_TYPE)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

if __name__ == '__main__':
    _sanae_ai.start_pricing_refresh()
    if _os.environ.get('SANAE_ENABLE_BACKUP_WATCH', '1') == '1':
        _backup_watch.start()
    if _os.environ.get('SANAE_ENABLE_LEGACY_MONITOR', '0') == '1':
        # Keep the Windows production deployment single-process: the existing
        # monitor only emits join/advancement/crash/ops events and deliberately
        # excludes ordinary player chat.
        import legacy_monitor as _legacy_monitor
        threading.Thread(target=_legacy_monitor.main,
                         name='legacy-monitor', daemon=True).start()
    print(f"桥接服务启动: 监听 0.0.0.0:{BRIDGE_PORT} → 早苗独立模块体系（!wiki/!mod/RCON/@早苗/G2S/私聊），完全不走 agent", flush=True)
    HTTPServer(('0.0.0.0', BRIDGE_PORT), BridgeHandler).serve_forever()
