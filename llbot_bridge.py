#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
早苗群消息桥接服务（2026-08-16 完全重做版）
- 监听 18790，接收早苗 LLBot 的 OneBot 11 HTTP 上报
- 处理配置中的多个 QQ 群；完全不走 OpenClaw agent/hooks（布鲁斯链路已废弃封存）
- 全部回复统一走早苗 API（3002）发回
- 功能路由（全部独立模块，零 agent 依赖）：
  1. !wiki/!百科     → mcmod.cn 抓取（_fetch_wiki）
  2. !mod/!模组/!cf  → Modrinth + CF + 自动翻译（mod_lookup.py）
  3. !tps/!list/status → RCON 直查（rcon_client.py）
  4. @早苗           → 正则匹配 → DeepSeek API → 早苗人设回复（sanae_ai.py）
  5. 群→服 G2S      → 群里消息 say 进游戏内聊天（_relay_g2s）
  6. 私聊           → 转发给狗蛋
"""

# The Windows scheduled-task wrapper normally pipes stdout/stderr through a
# PowerShell supervisor.  If that wrapper is stopped while this child keeps
# running, a busy QQ group can eventually fill the pipe and stall the single
# event worker.  Bind the streams to append-only files inside the child when
# the launcher provides paths, so event intake never depends on a parent pipe.
import os as _early_os
import sys as _early_sys

_STDIO_HANDLES = []


class _TimestampedStream:
    """Prefix each log line with local time so incidents can be placed.

    One lock keeps a single write whole.  print() writes text and newline
    separately, so concurrent prints may still interleave as they did before.
    """

    def __init__(self, handle):
        import threading as _threading
        from datetime import datetime as _datetime
        self._handle, self._lock, self._datetime = handle, _threading.Lock(), _datetime
        self._at_line_start = True

    def write(self, text):
        if not text:
            return 0
        with self._lock:
            pieces = []
            for piece in str(text).splitlines(keepends=True):
                if self._at_line_start:
                    # Host-local time with its offset: the host runs UTC+9
                    # while group-facing archives use UTC+8.
                    pieces.append(self._datetime.now().astimezone().strftime('[%Y-%m-%d %H:%M:%S%z] '))
                pieces.append(piece)
                self._at_line_start = piece.endswith(('\n', '\r'))
            self._handle.write(''.join(pieces))
        return len(text)

    def flush(self):
        self._handle.flush()

    def __getattr__(self, name):
        return getattr(self._handle, name)


def _configure_child_stdio():
    for stream_name, env_name in (('stdout', 'SANAE_STDOUT_LOG'),
                                  ('stderr', 'SANAE_STDERR_LOG')):
        path = _early_os.environ.get(env_name, '').strip()
        if not path:
            continue
        try:
            parent = _early_os.path.dirname(_early_os.path.abspath(path))
            if parent:
                _early_os.makedirs(parent, exist_ok=True)
            handle = open(path, 'a', encoding='utf-8', buffering=1,
                          errors='replace')
            _STDIO_HANDLES.append(handle)
            setattr(_early_sys, stream_name, _TimestampedStream(handle))
        except Exception:
            # Keep the inherited stream if a diagnostic log cannot be opened;
            # startup and message handling must remain available.
            continue


_configure_child_stdio()

import json
from onebot_utf8 import onebot_headers
import re
import html
import threading
import time
import collections
import contextvars
from contextlib import contextmanager
import unicodedata
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler
from bridge_runtime import CallbackServer, start_worker as _start_worker, start_ai_worker as _start_ai_worker

# 同目录导入依赖模块（systemd 启动时工作目录可能不是脚本目录，显式加 sys.path）
import os as _os
import sys as _sys
_BRIDGE_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _BRIDGE_DIR not in _sys.path:
    _sys.path.insert(0, _BRIDGE_DIR)
from ops_contract import onebot_success
from onebot_utf8 import JSON_CONTENT_TYPE, json_bytes, utf8_text
from sanae_identity import SANAE_QQ, HISTORICAL_BOT_QQ, BOT_NAME, BOT_NAME_PATTERN
from server_registry import (clear_selected, extract_server_selector,
                             extract_natural_server_selector, fanout_group_message,
                             get_group_default, get_selected, list_servers, query_server, resolve_server,
                             select_server, server_hint, server_prefix)
from ops_commands import STATE_BASE as OPS_STATE_BASE, dispatch_ops_command, match_ops_command
from nast_ops_sync import sync_if_stale

# 模组查询模块（CF 站 + M 站 + 自动翻译）
from local_secrets import require_secret as _require_secret
import mod_lookup as _mod_lookup
from public_errors import public_failure_label
from runtime_paths import default_path as _default_path
import fun_mute as _fun
import qq_interact as _qq_interact
import bug_reports as _bugs
_mod_lookup.set_zhipu_key(_require_secret('zhipu_api_key'))
_mod_lookup.set_cf_api_key(_require_secret('curseforge_api_key'))

# 早苗 AI 独立模块（正则匹配 + DeepSeek API，不走 agent）
import sanae_ai as _sanae_ai
from model_display import strip_reply_period
import backup_watch as _backup_watch
import shop_query as _shop_query
from social_lite import SocialLite, OneBotActions, split_social_reply, split_informative_reply
from social_feedback import FeedbackStore
from sticker_catalog import StickerCatalog
from chat_archive import ChatArchive
from conversation_memory import ConversationMemory
from conversation_turns import ConversationTurns
from expression_memory import ExpressionMemory
from reply_lessons import ReplyLessons
from silent_vision import SilentVision
from functools import wraps

# RCON 反控命令模块（2026-08-16 按腐竹运维监控工具包 QQConsoleBridge 命令集实现）
import rcon_ops as _rcon_ops
import command_catalog as _command_catalog
import server_insight as _server_insight
from player_bindings import PlayerBindings, known_names as _known_player_names
from server_registry import parse_player_list as _parse_player_list
import player_trail as _player_trail
import sanae_local_ops as _sanae_local_ops
from media_segments import media_segments, media_urls
from media_ai import image_host_upload, _download, analyze_media as _analyze_media, media_fingerprints
from request_runtime import Budget, bounded_call, RequestTimeout, RequestBusy
import network_commands as _network_commands

# ===== 基本配置 =====
LLBOT_GROUP = int(_os.environ.get('LLBOT_GROUP', '0'))
def _group_ids(raw, fallback):
    values = set()
    for item in str(raw or '').split(','):
        try:
            value = int(item.strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.add(value)
    return frozenset(values or {int(fallback)})


# The original group remains the default.  Additional groups can share the
# same Sanae bridge without another QQ login or OneBot instance.
LLBOT_GROUPS = _group_ids(_os.environ.get('SANAE_GROUPS', str(LLBOT_GROUP)), LLBOT_GROUP)
DISABLED_GROUPS = _group_ids(_os.environ.get('SANAE_DISABLED_GROUPS', ''), 0)
RELAY_GROUPS = _group_ids(_os.environ.get('SANAE_RELAY_GROUPS', str(LLBOT_GROUP)), LLBOT_GROUP)
REPLY_GROUP = contextvars.ContextVar('sanae_reply_group', default=LLBOT_GROUP)
REPLY_PERSON = contextvars.ContextVar('sanae_reply_person', default='')
REPLY_EXPRESSION = contextvars.ContextVar('sanae_reply_expression', default=None)
REPLY_MESSAGE_IDS = contextvars.ContextVar('sanae_reply_message_ids', default=None)
REPLY_LESSON_GENERATION = contextvars.ContextVar('sanae_reply_lesson_generation', default=None)


def _bridge_group_enabled(group_id):
    try:
        group_id = int(group_id)
        return group_id in LLBOT_GROUPS and group_id not in DISABLED_GROUPS
    except (TypeError, ValueError):
        return False


def _event_needs_bridge(event):
    """Return whether an incoming OneBot event should enter the bridge queue.

    Only group messages for groups outside ``SANAE_GROUPS`` are filtered.
    Private messages and non-message events keep their legacy handling.
    """
    if (event.get('post_type') == 'message' and
            event.get('message_type') == 'group'):
        return _shop_query.is_query(event) or _bridge_group_enabled(event.get('group_id'))
    return True


def _single_server_fallback_allowed(group_id):
    """Keep the historical one-server shortcut scoped to the old group."""
    try:
        return int(group_id) == int(LLBOT_GROUP)
    except (TypeError, ValueError):
        return False


WIKI_BOT_QQ = SANAE_QQ  # 当前早苗账号；历史机器人账号仍排除自回复和学习
# 仅限制群聊向游戏转发；按 QQ 账号匹配，昵称变更不影响。
G2S_EXCLUDED_QQ = frozenset(x.strip() for x in _os.environ.get('SANAE_G2S_EXCLUDED_QQ', '').split(',') if x.strip())
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

# 群聊功能总开关（环境变量 GROUP_CHAT_ENABLED=0 关闭；保留私聊转发）
GROUP_CHAT_ENABLED = _os.environ.get('GROUP_CHAT_ENABLED', '1') == '1'
# 群→双服全量转发开关；生产默认开启，显式 RELAY_G2S=0 才关闭。
RELAY_G2S_ENABLED = _os.environ.get('RELAY_G2S', '1') != '0'
OPS_NAST_SYNC_INTERVAL = max(60, int(_os.environ.get('OPS_NAST_SYNC_INTERVAL', '300')))

# Social Lite is scoped to the configured social group. It is opt-in
# and adds no process, account, or agent.
SOCIAL_LITE_GROUP = int(_os.environ.get('SOCIAL_LITE_GROUP', str(LLBOT_GROUP)))
SOCIAL_LITE_ENABLED = _os.environ.get('SOCIAL_LITE_ENABLED', '0') == '1'
# Conversational/model replies require an actual OneBot @ segment in
# production. Explicit bot commands keep their own deterministic routes.
SANAE_MENTION_ONLY = _os.environ.get('SANAE_MENTION_ONLY', '1') == '1'
# Explicit name calls may be enabled without enabling ambient conversation.
SANAE_NAME_CALL_ENABLED = _os.environ.get('SANAE_NAME_CALL_ENABLED', '0') == '1'
SOCIAL_LITE = SocialLite(
    _os.environ.get('SOCIAL_LITE_STATE', _default_path('state', 'social-lite.json')),
    _os.environ.get('SOCIAL_LITE_ACTIVITY', _default_path('logs', 'social-activity.log')),
    self_id=WIKI_BOT_QQ,
    max_recent=int(_os.environ.get('SOCIAL_LITE_MAX_RECENT', '30')),
    cooldown_seconds=float(_os.environ.get('SOCIAL_LITE_COOLDOWN', '12')),
    spontaneous_probability=float(_os.environ.get('SOCIAL_LITE_SPONTANEOUS', '0')),
    ambient_reply_weight=float(_os.environ.get('SOCIAL_LITE_AMBIENT_WEIGHT', '1.0')),
    reply_archive_dir=_os.environ.get('SOCIAL_REPLY_ARCHIVE') or None,
    reply_archive_days=int(_os.environ.get('SOCIAL_REPLY_ARCHIVE_DAYS', '365')),
)
QQ_ACTIONS = OneBotActions(_os.environ.get('LLBOT_API', WIKI_API),
                           token=_os.environ.get('LLBOT_API_TOKEN', ''))
FEEDBACK_STORE = FeedbackStore(
    _os.environ.get('FEEDBACK_PATH', _default_path('state', 'social-feedback.json')),
    group_id=SOCIAL_LITE_GROUP,
    max_items=int(_os.environ.get('FEEDBACK_MAX_ITEMS', '500')),
)
STICKER_CATALOG = StickerCatalog(
    _os.environ.get('STICKER_MANIFEST', _os.path.join(_BRIDGE_DIR, 'state', 'stickers', 'stickers.json')))
SOCIAL_ARCHIVE = ChatArchive(_os.environ.get(
    'SOCIAL_ARCHIVE_ROOT', _default_path('logs', 'chat-archive')),
    retention_days=int(_os.environ.get('SOCIAL_ARCHIVE_DAYS', '365')))
CONVERSATION_MEMORY = ConversationMemory(
    _os.environ.get('CONVERSATION_MEMORY_PATH', _default_path('state', 'conversation-memory.json')),
    allowed_group_ids=(SOCIAL_LITE_GROUP,), bot_user_ids=HISTORICAL_BOT_QQ)


CONVERSATION_TURNS = ConversationTurns(allowed_group_ids=(SOCIAL_LITE_GROUP,),
    ttl_seconds=_sanae_ai.limits.LONG_REQUEST_SECONDS + 10)
EXPRESSION_MEMORY = ExpressionMemory(
    _os.environ.get('EXPRESSION_MEMORY_PATH', _default_path('state', 'expression-memory.json')),
    allowed_group_ids=(SOCIAL_LITE_GROUP,), bot_user_ids=HISTORICAL_BOT_QQ)
# Learned expressions drifted toward one technique (understatement); production
# turns learning off. Stored entries stay on disk but are neither extended nor injected.
EXPRESSION_LEARNING_ENABLED = _os.environ.get('SANAE_EXPRESSION_LEARNING', '1') == '1'
REPLY_LESSONS = ReplyLessons(
    _os.environ.get('REPLY_LESSONS_PATH', _default_path('state', 'reply-lessons.json')),
    allowed_group_ids=(SOCIAL_LITE_GROUP,), bot_user_ids=HISTORICAL_BOT_QQ)
SILENT_VISION_ENABLED = _os.environ.get('SANAE_SILENT_VISION', '0') == '1'
SILENT_VISION = SilentVision(
    lambda image: _sanae_ai.observe_silent_image(image, api_url=WIKI_API),
    allowed_group_ids=(SOCIAL_LITE_GROUP,), bot_user_ids=HISTORICAL_BOT_QQ)


def _silent_image_context(event, text):
    if not SILENT_VISION_ENABLED:
        return ''
    try:
        return SILENT_VISION.context(event, text)
    except Exception as exc:
        print(f'[silent-vision] context failed: {type(exc).__name__}', flush=True)
        return ''


def _learn_expressions(batch):
    try:
        response = _sanae_ai.learn_expression_patterns(batch)
    except Exception as exc:
        print(f'[expression-memory] extraction failed: {type(exc).__name__}', flush=True)
        response = None
    try:
        EXPRESSION_MEMORY.complete_batch(batch, response)
    except Exception as exc:
        print(f'[expression-memory] save failed: {type(exc).__name__}', flush=True)


def _reply_current(event):
    turn = event.get('_conversation_turn')
    return turn is None or turn.current()


def _social_outcome(event, result, started, metadata):
    """One line per scheduled social turn, so silence and failure differ in the log."""
    metadata = metadata or {}
    print(f"[social-lite] outcome group={event.get('group_id')} user={event.get('user_id')} "
          f"result={result} elapsed={time.monotonic() - started:.1f}s "
          f"fallback={int(bool(metadata.get('text_fallback')))} "
          f"failure={metadata.get('failure') or '-'}", flush=True)


def _expression_context(gid, uid, raw):
    if not EXPRESSION_LEARNING_ENABLED:
        return ''
    try:
        context, ticket = EXPRESSION_MEMORY.reserve(gid, uid, raw)
        REPLY_EXPRESSION.set(ticket)
        return context
    except Exception as exc:
        print(f'[expression-memory] context failed: {type(exc).__name__}', flush=True)
        return ''


def _expression_sent():
    """Commit once after visible reply text succeeds, never after an error notice."""
    ticket = REPLY_EXPRESSION.get()
    if ticket is None:
        return
    REPLY_EXPRESSION.set(None)
    try:
        EXPRESSION_MEMORY.commit(ticket)
    except Exception as exc:
        # A journal failure must not turn a successful QQ send into a retry.
        print(f'[expression-memory] delivery record failed: {type(exc).__name__}', flush=True)


def _release_expression():
    ticket = REPLY_EXPRESSION.get()
    REPLY_EXPRESSION.set(None)
    if ticket is not None:
        EXPRESSION_MEMORY.release(ticket)


def _reply_scope(callback):
    @wraps(callback)
    def scoped(self, event, *args, **kwargs):
        group = REPLY_GROUP.set(int(event.get('group_id', LLBOT_GROUP)))
        person = REPLY_PERSON.set(str(event.get('user_id') or ''))
        expression = REPLY_EXPRESSION.set(None)
        receipts = REPLY_MESSAGE_IDS.set([])
        generation = REPLY_LESSON_GENERATION.set(REPLY_LESSONS.generation(
            event.get('group_id', LLBOT_GROUP), event.get('user_id')))
        turn = event.get('_conversation_turn')
        try:
            if _reply_current(event):
                return callback(self, event, *args, **kwargs)
        finally:
            try:
                _release_expression()
            finally:
                REPLY_EXPRESSION.reset(expression)
            if turn is not None:
                turn.finish()
            REPLY_PERSON.reset(person)
            REPLY_GROUP.reset(group)
            REPLY_MESSAGE_IDS.reset(receipts)
            REPLY_LESSON_GENERATION.reset(generation)
    return scoped


def _reply_id(event):
    for segment in event.get('message') or []:
        if isinstance(segment, dict) and segment.get('type') == 'reply':
            return str((segment.get('data') or {}).get('id') or '')
    match = re.search(r'\[CQ:reply,id=(-?\d+)(?:,[^\]]*)?\]', str(event.get('raw_message') or ''))
    return match.group(1) if match else ''


def _summarize_conversation_memory(batch):
    try:
        response = _sanae_ai.summarize_conversation_memory(batch)
    except Exception as exc:
        print(f'[conversation-memory] summary failed: {type(exc).__name__}', flush=True)
        response = None
    try:
        CONVERSATION_MEMORY.complete_batch(batch, response)
    except Exception as exc:
        print(f'[conversation-memory] summary save failed: {type(exc).__name__}', flush=True)


def _learn_reply_lessons(batch):
    try:
        response = _sanae_ai.summarize_conversation_memory(batch, log_label='reply-lessons')
    except Exception as exc:
        print(f'[reply-lessons] extraction failed: {type(exc).__name__}', flush=True)
        response = None
    try:
        REPLY_LESSONS.complete_batch(batch, response)
    except Exception as exc:
        print(f'[reply-lessons] save failed: {type(exc).__name__}', flush=True)


def _lesson_context(gid, uid, raw, style_override):
    current = re.sub(r'\[CQ:(?:at|reply),[^\]]*\]', '', raw).strip()
    current = re.sub(r'^' + BOT_NAME_PATTERN + r'(?:\s|[，,：:。！？!?、]|$)', '', current,
                     flags=re.I).strip()
    try:
        return REPLY_LESSONS.context(gid, uid, current, gentle=bool(style_override))
    except Exception as exc:
        print(f'[reply-lessons] context failed: {type(exc).__name__}', flush=True)
        return ''


def _forget_conversation_context(gid, uid=None, *, clear_closing=True):
    CONVERSATION_TURNS.forget(gid, uid)
    SILENT_VISION.forget(gid, uid)
    _sanae_ai.clear_conversation_history(gid, uid)
    SOCIAL_LITE.forget_recent(gid, user_id=uid, clear_closing=clear_closing)


SOCIAL_STICKER_MODE = _os.environ.get('SOCIAL_STICKER_MODE', 'recommend').strip().lower()
SOCIAL_STICKER_COOLDOWN = max(10.0, float(_os.environ.get('SOCIAL_STICKER_COOLDOWN', '45')))
_STICKER_COOLDOWN_LOCK = threading.Lock()
_LAST_AUTO_STICKER: dict[str, float] = {}
_PENDING_AUTO_STICKER: set[str] = set()


@contextmanager
def _auto_sticker(group_id, request, body, *, enabled=True):
    """Reserve one group send; commit rotation/cooldown only on its receipt."""
    key = str(group_id)
    ticket = None
    if (enabled and SOCIAL_STICKER_MODE == 'auto' and body.strip()
            and body.strip().upper() != '[SILENT]'
            and STICKER_CATALOG.allows_auto(request, body)):
        with _STICKER_COOLDOWN_LOCK:
            last = _LAST_AUTO_STICKER.get(key)
            if (key not in _PENDING_AUTO_STICKER and
                    (last is None or time.time() - last >= SOCIAL_STICKER_COOLDOWN)):
                chosen = STICKER_CATALOG.pick_for_send(body, scope=key, request=request)
                image = STICKER_CATALOG.cq_image(chosen['id']) if chosen else None
                if image:
                    ticket = {**chosen, 'image': image, 'delivered': False}
                    _PENDING_AUTO_STICKER.add(key)
    try:
        yield ticket
    finally:
        if ticket:
            with _STICKER_COOLDOWN_LOCK:
                try:
                    if ticket['delivered']:
                        _LAST_AUTO_STICKER[key] = time.time()
                        STICKER_CATALOG.mark_sent(ticket['id'], scope=key)
                except Exception as exc:
                    # A local journal failure must never resend a visible image.
                    print(f'[stickers] delivery history failed: {type(exc).__name__}', flush=True)
                finally:
                    _PENDING_AUTO_STICKER.discard(key)
            print('[sticker-send] group={} mood={} id={} delivered={}'.format(
                key, ticket['mood'], ticket['id'], bool(ticket['delivered'])), flush=True)


_FUN_ROLE_CACHE = {}
_FUN_ROLE_TTL = 600


def _fun_member_role(gid, uid):
    """Group role via OneBot ('owner'/'admin'/'member'); '' if unknown."""
    try:
        return str((QQ_ACTIONS.member_info(gid, uid) or {}).get('role') or '')
    except Exception as exc:
        print(f'[fun-mute] role lookup failed: {type(exc).__name__}', flush=True)
        return ''


def _fun_bot_can_mute(gid):
    """Cached: only a group owner/admin account can ban members."""
    cached = _FUN_ROLE_CACHE.get(gid)
    now = time.time()
    if not cached or now - cached[1] > _FUN_ROLE_TTL:
        cached = (_fun_member_role(gid, WIKI_BOT_QQ), now)
        _FUN_ROLE_CACHE[gid] = cached
    return cached[0] in {'owner', 'admin'}


# QQ group actions the model may take (round 29): checked against live QQ roles in qq_interact.
QQ_INTERACT = _qq_interact.QQInteract(QQ_ACTIONS, WIKI_BOT_QQ,
                                      audit_path=_default_path('state', 'qq-actions-audit.jsonl'))
_sanae_ai.QQ_INTERACT = QQ_INTERACT
# Group album photos are described for her on request (round 32).
QQ_INTERACT.describe = _sanae_ai.describe_images
POKE_CONTEXT = ('〖事件〗对方刚在群里用 QQ 的“戳一戳”戳了你，没有说话。用一两句自然、简短的话回应，'
                '可以小小吐槽或者问问有什么事；不要说“收到”，不要提自己是 AI，不要输出贴纸 ID。')


def _poke_reaction(gid, uid, decision):
    """Answer a poke once with a line, after that poke back (limits in qq_interact.on_poke)."""
    if _fun_recently_muted(gid, uid):
        return
    token = REPLY_GROUP.set(gid)
    try:
        if decision == 'reply':
            try:
                nick = _qq_interact.name_of(QQ_ACTIONS.member_info(gid, uid))
            except Exception:
                nick = ''
            reply = _sanae_ai.sanae_reply('（戳了戳你）', uid, nick, privileged=False, group_id=gid, force=True,
                                          social_only=True, include_usage_footer=False,
                                          explicit_interaction=True, context_reference=POKE_CONTEXT)
            if reply and reply.strip().upper() != '[SILENT]':
                if send_group_msg(strip_reply_period(f'[CQ:at,qq={uid}] {reply}'), api=WIKI_API):
                    try:
                        SOCIAL_LITE.record_outgoing(gid, f'（被戳后回应）{reply}', action='poke-reply',
                                                    reply_to_user_id=uid)
                    except Exception:
                        pass
                return
        QQ_ACTIONS.poke(uid, gid)
    except Exception as exc:
        print(f'[poke] reaction failed: {type(exc).__name__}', flush=True)
    finally:
        REPLY_GROUP.reset(token)


def _handle_notify(event):
    """QQ notify events: pokes at her in enabled groups, likes on her profile card."""
    sub = str(event.get('sub_type') or '')
    if sub == 'poke':
        gid = event.get('group_id')
        uid, target = str(event.get('user_id') or ''), str(event.get('target_id') or '')
        if not gid or not _bridge_group_enabled(gid) or target != str(WIKI_BOT_QQ):
            return
        decision = QQ_INTERACT.on_poke(int(gid), uid, target)
        print(f'[poke] group={gid} user={uid} decision={decision}', flush=True)
        if decision:
            # A reply is a model call and waits in the AI pool; a poke back is a quick OneBot call.
            (_start_ai_worker if decision == 'reply' else _start_worker)(_poke_reaction, args=(int(gid), uid, decision))
    elif sub == 'profile_like':
        operator = str(event.get('operator_id') or '')
        print(f'[profile-like] from={operator} times={event.get("times")}', flush=True)
        _start_worker(QQ_INTERACT.on_profile_like, args=(operator,))


# Private messages to the bot were forwarded to every owner; one owner is not a
# friend of the bot, so half of those sends always failed. Off in production.
PRIVATE_FORWARD_ENABLED = _os.environ.get('SANAE_PRIVATE_FORWARD', '1') == '1'
FUN_MUTE_AI_ENABLED = _os.environ.get('FUN_MUTE_AI', '0') == '1'
FUN_MUTE_COOLDOWN = 900        # at most one joke mute per member per 15 minutes
FUN_MUTE_HOURLY_CAP = 8        # and at most this many per hour in the group
_FUN_STATE_LOCK = threading.Lock()
_FUN_MUTED_UNTIL = {}          # (gid, uid) -> unix time; replies to them pause
_FUN_LAST_MUTE = {}            # uid -> unix time of the last applied mute
_FUN_MUTE_TIMES = collections.deque(maxlen=64)
_FUN_RECENT_NOTICES = collections.deque(maxlen=6)
_FUN_DORMANT_LOGGED = [0.0]
FUN_MUTE_STATE_PATH = _os.environ.get('FUN_MUTE_STATE') or _default_path('state', 'fun-mute-state.json')
_FUN_STATS = {'since': time.time(), 'batches': 0, 'messages': 0, 'dormant': 0, 'decisions': 0,
              'applied': 0, 'skipped': 0, 'errors': 0}


def _fun_load_state(path=None):
    """Restore cooldowns, the hourly window and recent notices after a restart."""
    try:
        with open(path or FUN_MUTE_STATE_PATH, encoding='utf-8') as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return False
    now = time.time()
    with _FUN_STATE_LOCK:
        _FUN_LAST_MUTE.clear()
        _FUN_LAST_MUTE.update({str(uid): float(stamp) for uid, stamp in (data.get('last_mute') or {}).items()
                               if now - float(stamp) < FUN_MUTE_COOLDOWN})
        _FUN_MUTE_TIMES.clear()
        _FUN_MUTE_TIMES.extend(float(t) for t in data.get('mute_times') or [] if now - float(t) < 3600)
        _FUN_MUTED_UNTIL.clear()
        for key, until in (data.get('muted_until') or {}).items():
            gid, _, uid = str(key).partition(':')
            if float(until) > now and gid.isdigit():
                _FUN_MUTED_UNTIL[(int(gid), uid)] = float(until)
        _FUN_RECENT_NOTICES.clear()
        _FUN_RECENT_NOTICES.extend(str(x) for x in (data.get('recent_notices') or [])[-6:])
    return True


def _fun_save_state(path=None):
    path = path or FUN_MUTE_STATE_PATH
    with _FUN_STATE_LOCK:
        data = {'last_mute': dict(_FUN_LAST_MUTE), 'mute_times': list(_FUN_MUTE_TIMES),
                'muted_until': {f'{gid}:{uid}': until for (gid, uid), until in _FUN_MUTED_UNTIL.items()},
                'recent_notices': list(_FUN_RECENT_NOTICES)}
    try:
        _os.makedirs(_os.path.dirname(_os.path.abspath(path)), exist_ok=True)
        temp = path + '.tmp'
        with open(temp, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
        _os.replace(temp, path)
    except OSError as exc:
        print(f'[fun-mute] state save failed: {type(exc).__name__}', flush=True)


def _fun_stat(key, amount=1):
    """Count judge work; one summary line per hour, so call volume stays visible."""
    with _FUN_STATE_LOCK:
        _FUN_STATS[key] = _FUN_STATS.get(key, 0) + amount
        now = time.time()
        if now - _FUN_STATS['since'] < 3600:
            return
        line = ' '.join(f'{name}={_FUN_STATS[name]}' for name in
                        ('batches', 'messages', 'dormant', 'decisions', 'applied', 'skipped', 'errors'))
        _FUN_STATS.update(since=now, batches=0, messages=0, dormant=0, decisions=0,
                          applied=0, skipped=0, errors=0)
    print('[fun-mute] last hour: ' + line, flush=True)


def _fun_recently_muted(gid, uid):
    with _FUN_STATE_LOCK:
        return _FUN_MUTED_UNTIL.get((int(gid or 0), str(uid)), 0) > time.time()


def _fun_group_muted_lately(gid, window=600):
    """Any joke mute in this group still running or ended within `window` seconds."""
    now = time.time()
    with _FUN_STATE_LOCK:
        return any(g == int(gid or 0) and until > now - window for (g, _uid), until in _FUN_MUTED_UNTIL.items())


# A reply may talk about a real mute; only one with no mute behind it is stopped.
_sanae_ai.RECENT_MUTE_FN = _fun_group_muted_lately


def _fun_judge(context, batch):
    """Judge thread: skip the model entirely while the bot cannot mute."""
    if not _fun_bot_can_mute(FUN_MUTE_GROUP):
        _fun_stat('dormant')
        now = time.time()
        if now - _FUN_DORMANT_LOGGED[0] >= 3600:
            _FUN_DORMANT_LOGGED[0] = now
            print('[fun-mute] dormant: bot is not a group admin; messages are not judged', flush=True)
        return []
    started = time.monotonic()
    _fun_stat('batches')
    _fun_stat('messages', len(batch))
    try:
        decisions = _sanae_ai.judge_fun_mute(context, batch)
    except Exception as exc:
        _fun_stat('errors')
        print(f'[fun-mute] judge failed: {type(exc).__name__}', flush=True)
        return []
    _fun_stat('decisions', len(decisions))
    if decisions:
        print(f'[fun-mute] judged n={len(batch)} mutes={len(decisions)} '
              f'elapsed={time.monotonic() - started:.1f}s', flush=True)
    return decisions


def _fun_apply(item, decision):
    """Hard limits stay here; the model only proposes scene, duration and words."""
    gid, uid = FUN_MUTE_GROUP, str(item.get('uid') or '')
    scene = str(decision.get('scene') or 'other')
    seconds = _fun.clamp(decision.get('seconds'), scene)
    label = f'[fun-mute] group={gid} user={uid} scene={scene} duration={seconds}'
    now = time.time()
    with _FUN_STATE_LOCK:
        limited = ('cooldown' if now - _FUN_LAST_MUTE.get(uid, 0) < FUN_MUTE_COOLDOWN else
                   'hourly-cap' if sum(1 for stamp in _FUN_MUTE_TIMES if now - stamp < 3600) >= FUN_MUTE_HOURLY_CAP
                   else '')
        if not limited:
            # Mark first so a reply already being generated for them is not sent.
            _FUN_MUTED_UNTIL[(gid, uid)] = now + seconds
    if limited:
        _fun_stat('skipped')
        print(label + ' skipped=' + limited, flush=True)
        return

    def release():
        with _FUN_STATE_LOCK:
            _FUN_MUTED_UNTIL.pop((gid, uid), None)

    if _fun_member_role(gid, uid) in {'owner', 'admin'}:
        release()
        _fun_stat('skipped')
        print(label + ' skipped=target-is-admin', flush=True)
        return
    try:
        with _FUN_STATE_LOCK:
            recent = list(_FUN_RECENT_NOTICES)
        notice = _fun.clean_notice(_sanae_ai.write_fun_mute_notice(
            item.get('text', ''), scene, _fun.duration_text(seconds),
            decision.get('reason', ''), recent, scope=str(gid)), rejects=_sanae_ai._leaked_tool_markup)
    except Exception as exc:
        print(label + f' notice-failed={type(exc).__name__}', flush=True)
        notice = ''
    if not notice:
        release()
        _fun_stat('skipped')
        print(label + ' skipped=no-notice', flush=True)
        return
    try:
        QQ_ACTIONS.mute(gid, uid, seconds)
    except Exception as exc:
        _FUN_ROLE_CACHE.pop(gid, None)
        release()
        _fun_stat('errors')
        print(label + f' ok=False error={type(exc).__name__} detail={str(exc)[:80]}', flush=True)
        return
    with _FUN_STATE_LOCK:
        _FUN_LAST_MUTE[uid] = now
        _FUN_MUTE_TIMES.append(now)
        _FUN_RECENT_NOTICES.append(notice)
    _fun_stat('applied')
    _fun_save_state()
    token = REPLY_GROUP.set(gid)
    try:
        send_group_msg(notice, api=WIKI_API)
    finally:
        REPLY_GROUP.reset(token)
    try:
        # She should know what she just did when someone asks about it.
        SOCIAL_LITE.record_outgoing(gid, f'（禁言 {_fun.duration_text(seconds)}）{notice}',
                                    action='mute', reply_to_user_id=uid)
    except Exception as exc:
        print(f'[fun-mute] context record failed: {type(exc).__name__}', flush=True)
    print(label + f' ok=True reason={str(decision.get("reason") or "")[:40]} notice={notice}', flush=True)


FUN_MUTE_JUDGE = _fun.FunMuteJudge(_fun_judge, _fun_apply,
    log=lambda line: print(line, flush=True))
if FUN_MUTE_AI_ENABLED:
    _fun_load_state()


# ===== 玩家问题反馈（AI 从聊天里识别 + !bug 手动提交）=====
BUG_REPORT_GROUPS = _group_ids(_os.environ.get('BUG_REPORT_GROUPS', ''), 0) - {0}
BUG_DIGEST_GROUPS = _group_ids(_os.environ.get('BUG_DIGEST_GROUPS', ''), 0) - {0}
BUG_DIGEST_HOUR = int(_os.environ.get('BUG_DIGEST_HOUR', '21'))


def _notify_admin_groups(text):
    """notify_admins: one message to each admin group (the bug digest groups)."""
    ok = False
    for admin_gid in sorted(BUG_DIGEST_GROUPS):
        token = REPLY_GROUP.set(admin_gid)
        try:
            ok = bool(send_group_msg(text, api=WIKI_API)) or ok
        finally:
            REPLY_GROUP.reset(token)
    return ok


_sanae_ai.ADMIN_NOTIFY_FN = _notify_admin_groups if BUG_DIGEST_GROUPS else None
BUG_LEDGER = _bugs.BugLedger(
    _os.environ.get('BUG_REPORTS_PATH') or _default_path('state', 'bug-reports.json'),
    inventory_path=_os.path.join(str(OPS_STATE_BASE), 'legacy', 'mod-inventory.json'),
    events_path=_os.path.join(str(OPS_STATE_BASE), 'legacy', 'events.jsonl'))
_BUG_MESSAGES = collections.OrderedDict()     # message id -> who said what, for evidence
_BUG_LOCK = threading.Lock()
_BUG_STATS = collections.Counter()
_BUG_STATS_SINCE = [time.time()]


def _bug_stat(key, amount=1):
    with _BUG_LOCK:
        _BUG_STATS[key] += amount
        if time.time() - _BUG_STATS_SINCE[0] >= 3600:
            if _BUG_STATS.get('batches'):
                print('[bug-report] last hour: ' + ' '.join(
                    f'{k}={_BUG_STATS.get(k, 0)}' for k in ('batches', 'messages', 'new', 'merged', 'errors')),
                    flush=True)
            _BUG_STATS.clear()
            _BUG_STATS_SINCE[0] = time.time()


def _bug_judge(gid, context, batch):
    """Judge thread: write found reports straight into the ledger; nothing per-item to apply."""
    started = time.monotonic()
    _bug_stat('batches')
    _bug_stat('messages', len(batch))
    try:
        reports = _sanae_ai.judge_bug_reports(context, batch, BUG_LEDGER.open_items(),
                                              BUG_LEDGER.installed_ids())
    except Exception as exc:
        _bug_stat('errors')
        print(f'[bug-report] judge failed group={gid}: {type(exc).__name__}', flush=True)
        return []
    for report in reports:
        with _BUG_LOCK:
            evidence = [dict(_BUG_MESSAGES[mid], message_id=mid) for mid in report['ids'] if mid in _BUG_MESSAGES]
        try:
            item, merged = BUG_LEDGER.record(report, evidence, gid)
        except Exception as exc:
            _bug_stat('errors')
            print(f'[bug-report] save failed: {type(exc).__name__}', flush=True)
            continue
        _bug_stat('merged' if merged else 'new')
        print(f'[bug-report] {"merged" if merged else "new"} #{item["id"]} group={gid} '
              f'mod={item["mod"] or "-"} category={item["category"]} title={item["title"]} '
              f'elapsed={time.monotonic() - started:.1f}s', flush=True)
    return []


_BUG_JUDGES = {gid: _fun.FunMuteJudge(lambda context, batch, gid=gid: _bug_judge(gid, context, batch),
                                      lambda item, decision: None, quiet=30.0, max_wait=180.0,
                                      context_size=15, batch_size=20, max_pending=60,
                                      log=lambda line: print(line, flush=True), label='bug-report')
               for gid in BUG_REPORT_GROUPS}


def _bug_observe(event, raw):
    """Every text message is context; non-command messages of 6+ characters get judged."""
    try:
        gid = int(event.get('group_id') or 0)
        judge = _BUG_JUDGES.get(gid)
        if judge is None:
            return
        text = _normalize(raw if isinstance(raw, str) else str(raw or ''))[:300]
        uid = str(event.get('user_id') or '')
        if not text or uid == WIKI_BOT_QQ or uid in HISTORICAL_BOT_QQ:
            return
        sender = event.get('sender') or {}
        mid = str(event.get('message_id') or f'{uid}-{time.time():.3f}')
        name = str(sender.get('card') or sender.get('nickname') or '群友')[:24]
        stamp = time.strftime('%H:%M:%S')
        with _BUG_LOCK:
            _BUG_MESSAGES[mid] = {'uid': uid, 'name': name, 'text': text,
                                  'time': _bugs._clock(time.time())}
            while len(_BUG_MESSAGES) > 600:
                _BUG_MESSAGES.popitem(last=False)
        judge.observe({'id': mid, 'uid': uid, 'name': name, 'time': stamp, 'text': text,
                       'sender': __import__('hashlib').sha256(uid.encode('utf-8')).hexdigest()[:6]},
                      eligible=len(text) >= 6 and not text.startswith(('!', '！', '/')))
    except Exception as exc:
        print(f'[bug-report] observe failed: {type(exc).__name__}', flush=True)


def _bug_digest_once(now=None):
    """Send today's digest when due; returns True when one was delivered."""
    if not BUG_DIGEST_GROUPS:
        return False
    text, ids = BUG_LEDGER.digest_due(now, hour=BUG_DIGEST_HOUR)
    if not text:
        return False
    ok = True
    for gid in sorted(BUG_DIGEST_GROUPS):
        token = REPLY_GROUP.set(gid)
        try:
            ok = send_group_msg(text, api=WIKI_API) and ok
        finally:
            REPLY_GROUP.reset(token)
    BUG_LEDGER.mark_digest(ids, ok, now)
    print(f'[bug-report] digest items={len(ids)} ok={ok}', flush=True)
    return ok


def _bug_digest_loop():
    while True:
        try:
            _bug_digest_once()
        except Exception as exc:
            print(f'[bug-report] digest failed: {type(exc).__name__}', flush=True)
        time.sleep(300)


def _forward_summary(read_result):
    """Compact 'speaker：text / ...' from qq_chat_reader JSON; '' if unreadable."""
    try:
        data = json.loads(read_result)
    except (TypeError, ValueError):
        return ''
    if not isinstance(data, dict) or data.get('error'):
        return ''
    parts = [_sanae_ai.chat_recall.compact_forward(message.get('text'), limit=4000)
             for message in data.get('messages') or [] if isinstance(message, dict)]
    return ' / '.join(part for part in parts if part)


def _ingest_forward(event):
    """Expand a forwarded chat record once, so replies and the archive can read it."""
    gid, mid = event.get('group_id'), event.get('message_id')
    try:
        text = _forward_summary(_sanae_ai.qq_chat_reader.read_qq_message(
            _sanae_ai.LLBOT_API, int(gid), int(mid)))
        if not text:
            print(f'[forward] group={gid} message={mid} unreadable', flush=True)
            return
        SOCIAL_LITE.record_forward(event, text)
        sender = event.get('sender') or {}
        SOCIAL_ARCHIVE.append(gid, event.get('group_name') or str(gid),
                              sender.get('card') or sender.get('nickname') or event.get('user_id'),
                              '[聊天记录] ' + text[:1500])
        print(f'[forward] group={gid} message={mid} chars={len(text)}', flush=True)
    except Exception as exc:
        print(f'[forward] ingest failed: {type(exc).__name__}', flush=True)


def _refresh_nast_ops():
    """Best-effort restricted snapshot refresh; never expose payload or credentials."""
    try:
        result = sync_if_stale(OPS_STATE_BASE, max_age=OPS_NAST_SYNC_INTERVAL)
        if not result.get('cached'):
            print('[nast] [ops-sync] refreshed', flush=True)
        return True
    except Exception as exc:
        print(f'[nast] [ops-sync] failed {type(exc).__name__}', flush=True)
        return False


def _nast_ops_sync_loop():
    while True:
        _refresh_nast_ops()
        time.sleep(OPS_NAST_SYNC_INTERVAL)
PLAYER_BINDINGS_ENABLED = _os.environ.get('PLAYER_BINDINGS_ENABLED', '') == '1'
PLAYER_BINDINGS = PlayerBindings(_os.environ.get(
    'PLAYER_BINDINGS_PATH', _default_path('logs', 'qq-player-binds.json')))
# Round 30: a bound game name is who "我" is in her answers, and a target name for QQ actions.
PLAYER_BINDINGS_SERVER = _os.environ.get('PLAYER_BINDINGS_SERVER', 'legacy')
if PLAYER_BINDINGS_ENABLED:
    _sanae_ai.PLAYER_NAME_FN = lambda uid: (PLAYER_BINDINGS.get_qq(uid) or {}).get('name', '')
    QQ_INTERACT.bindings = PLAYER_BINDINGS


def _binding_known_names():
    online = ()
    server = resolve_server(PLAYER_BINDINGS_SERVER)
    if server:
        try:
            online = (_parse_player_list(query_server(server, 'list')) or {}).get('players') or ()
        except Exception:
            online = ()
    return _known_player_names(_os.path.join(str(OPS_STATE_BASE), PLAYER_BINDINGS_SERVER), online)
CHAT_RELAY_STATE = _default_path('logs', 'qq-chat-relay.json')
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


def _fun_mentions_someone_else(event):
    """An @ of another member makes "禁言我"-style text ambiguous; skip it."""
    raw = str(event.get('raw_message') or '')
    targets = set(re.findall(r'\[CQ:at,qq=(\d+|all)', raw))
    for seg in event.get('message') or []:
        if isinstance(seg, dict) and seg.get('type') == 'at':
            targets.add(str((seg.get('data') or {}).get('qq') or ''))
    return bool(targets - {WIKI_BOT_QQ, ''})


def _fun_mute_eligible(event, uid):
    """Ordinary members of the configured group only; the model never sees this list."""
    if int(event.get('group_id') or 0) != FUN_MUTE_GROUP:
        return False
    if not uid or uid == WIKI_BOT_QQ or uid in HISTORICAL_BOT_QQ or uid in OWNERS or uid in ADMINS:
        return False
    return FUN_MUTE_USER in ('', '*', 'all') or uid == FUN_MUTE_USER


def _fun_mute_observe(event, raw, addressed):
    """Queue one text message for the AI judge; never blocks or raises."""
    if not FUN_MUTE_AI_ENABLED:
        return
    try:
        if int(event.get('group_id') or 0) != FUN_MUTE_GROUP:
            return
        text = _normalize(raw if isinstance(raw, str) else str(raw or ''))[:300]
        if not text or text.startswith(('!', '！', '/')):
            return
        uid = str(event.get('user_id') or '')
        sender = event.get('sender') or {}
        FUN_MUTE_JUDGE.observe({
            'id': str(event.get('message_id') or f'{uid}-{time.time():.3f}'),
            'uid': uid,
            # The model tells people apart by this key; names may collide.
            'sender': __import__('hashlib').sha256(uid.encode('utf-8')).hexdigest()[:6],
            'name': str(sender.get('card') or sender.get('nickname') or '群友')[:24],
            'time': time.strftime('%H:%M:%S'), 'text': text,
            'addressed_bot': bool(addressed),
            'mentions_others': _fun_mentions_someone_else(event),
        }, eligible=_fun_mute_eligible(event, uid))
    except Exception as exc:
        print(f'[fun-mute] observe failed: {type(exc).__name__}', flush=True)


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
        return ('操作目标已清除。在线查询按当前启用的服务器汇总；'
                '普通群消息转发仍按现有配置。')
    if show:
        selected, _ = get_selected(group_id, user_id)
        if not selected:
            selected, _ = get_group_default(group_id)
        current = (f'{server_prefix(selected)} {selected["name"]}'
                   if selected else '未设置（在线查询按当前启用的服务器汇总）')
        names = [f'{server_prefix(item)} {item["name"]}' for item in list_servers()]
        return ('当前操作目标：' + current + '\n可选：' + '；'.join(names)
                + '\n切换：!服 <名称>；本条指定：服务器标签 + 命令；清除：!服 自动'
                + '\n说明：只影响查询和管理，普通群消息转发仍按现有配置。')
    if choice:
        server = resolve_server(choice.group(1))
    if not server:
        return f'{server_hint()} 目标不在允许清单。发送 !服 查看可选项。'
    selected = select_server(group_id, user_id, server['id'])
    return (f'{server_prefix(selected)} 操作目标已设为：{selected["name"]}\n'
            '只影响查询和管理；普通群消息转发仍按现有配置。')


def _command_target(raw, group_id, user_id):
    explicit, cleaned = extract_server_selector(_normalize(raw))
    if explicit:
        return explicit, cleaned, True, ''
    selected, error = get_selected(group_id, user_id)
    if selected:
        return selected, cleaned, False, error or ''
    default, default_error = get_group_default(group_id)
    if default:
        return default, cleaned, False, ''
    enabled = list_servers()
    if len(enabled) == 1 and _single_server_fallback_allowed(group_id):
        return enabled[0], cleaned, False, ''
    return None, cleaned, False, default_error or error or ''


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
    # 10-26 22:20: with the group default resolved first, a lone server never counted as
    # unambiguous, so request_console_command refused natural requests in that group.
    single = len(list_servers()) == 1
    selected, _error = get_selected(group_id, user_id)
    if selected:
        return selected, raw, single
    default, _default_error = get_group_default(group_id)
    if default:
        return default, raw, single
    enabled = list_servers()
    if len(enabled) == 1 and _single_server_fallback_allowed(group_id):
        return enabled[0], raw, True
    return None, raw, False


def _server_query_backend(server):
    def routed(command):
        return query_server(server, command)
    routed.server_id = server['id']
    return routed


# Roots whose arguments are free text, so Chinese there still means an exact command.
_CONSOLE_TEXT_ROOTS = {'say', 'tellraw', 'msg', 'tell', 'w', 'me', 'teammsg', 'tm', 'title',
                       'kick', 'ban', 'ban-ip', 'bossbar', 'team', 'scoreboard', 'trigger'}
_CONSOLE_CJK = re.compile(r'[\u3400-\u9fff\uff00-\uffef]')


def _console_plan(target, rest):
    """Route one admin `!cmd`: (kind, command, detail).

    usage   - empty or malformed; the old handler prints the usage line.
    legacy  - no scanned catalog for this server; the old confirmation path.
    direct  - a registered read/low command; runs at once (detail = risk class).
    confirm - a registered command that needs a code (detail = registered usage).
    ai      - free text, an unknown root, or Chinese where an ID belongs; she works it out.
    """
    text = str(rest or '').strip()
    if text.startswith('<') and text.endswith('>'):
        text = text[1:-1].strip()
    try:
        command = _command_catalog.normalize_console_command(text)
    except ValueError:
        return 'usage', '', ''
    if not _command_catalog.summary(target['id'])['count']:
        return 'legacy', command, ''
    supported, usage, _reason = _command_catalog.validate(target['id'], command)
    root = command.split(None, 1)[0].lstrip('/').casefold()
    if not supported or (_CONSOLE_CJK.search(command) and root not in _CONSOLE_TEXT_ROOTS):
        return 'ai', text, ''
    risk = _command_catalog.classify(command)
    if risk in ('read', 'low'):
        return 'direct', command, risk
    return 'confirm', command, usage


_SOCIAL_SERVER_RE = re.compile(
    r'服务器|服务端|开服|炸服|崩服|崩了|掉线|进不去|连不上|在线|几个人|多少人|人多|有人在玩|有人上|'
    r'卡不卡|好卡|很卡|太卡|有点卡|卡死|卡顿|卡爆|延迟|TPS|MSPT', re.I)


def _social_server_status(gid, raw):
    """'〖服务器现状〗[怀旧] 在线 6/20，TPS 19.5…' when the message is about the server."""
    text = re.sub(r'\[CQ:[^\]]*\]', '', str(raw or ''))
    if not _SOCIAL_SERVER_RE.search(text):
        return ''
    try:
        server, _error = get_group_default(gid)
        if not server:
            enabled = list_servers()
            server = enabled[0] if len(enabled) == 1 else None
        if not server:
            return ''
        ops_root = _os.path.join(str(OPS_STATE_BASE), str(server['id']))
        status = _server_insight.live_status(ops_root)
        verdict = _server_insight.live_verdict(ops_root)
    except Exception as exc:
        print(f'[social-lite] server status skipped: {type(exc).__name__}', flush=True)
        return ''
    if not status:
        return ''
    # TPS 20 with MSPT in the 40s reads as "busy" to a model; state the verdict.
    return (f'〖服务器现状〗{server_prefix(server)} {status}'
            + (f'；按 TPS/MSPT 判断：{verdict}（TPS≥18 且 MSPT<50 算流畅）' if verdict else ''))

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


def _natural_server_operation_candidate(raw, privileged, direct_call=False, at_call=False,
                                        group_id=None, user_id=''):
    """A server topic is not a request to the bot, even when an admin says it."""
    if not (direct_call or at_call):
        return False
    if _sanae_ai._has_server_query_intent(raw) or _sanae_ai._asks_to_notify_admins(raw):
        return True
    # 10-27 00:23 a follow-up by name ("早苗，把上述……丢给管理员") went to tool-free chat, which
    # then said "上面没有分析过坐标呀".
    if group_id is not None and _sanae_ai._server_followup(group_id, user_id, raw):
        return True
    return bool(privileged and _sanae_ai._has_operation_request(raw))


def _is_ai_operation_turn(raw, privileged, user_id=''):
    """运维意图必须只使用当前消息，不能混入社交记忆和风格提示。"""
    return bool(
        _sanae_ai._has_server_query_intent(raw) or _sanae_ai._asks_to_notify_admins(raw) or
        (privileged and (
            _sanae_ai._has_operation_request(raw) or
            ((_sanae_ai._has_confirmation_intent(raw) or _sanae_ai._has_cancellation_intent(raw))
             and _sanae_ai._has_pending_operation(user_id)))))


# "确认" / "取消" alone, as she asks for after a confirmation request.
_SHORT_OPERATION_REPLY = re.compile(
    r'^(?:好的?[，,\s]*)?(?:确认|确定|确认执行|执行|执行吧|批准|就这么做|可以执行|动手|'
    r'取消|取消吧|算了|算了吧|别执行|不要执行|不用了)[。！!~～\s]*$')


def _short_operation_reply(raw):
    text = _normalize(raw)
    text = re.sub(r'^@?' + BOT_NAME_PATTERN + r'[\s，,：:]*', '', text, flags=re.I).strip()
    return bool(_SHORT_OPERATION_REPLY.match(text))


def _pending_operation_reply_thread(event):
    """Confirm or cancel the caller's own pending command without a model call.

    The 90-second window is short and the relay can be slow or fail; a plain "确认" needs no
    reasoning. The code, the owner check and the server check stay in rcon_ops.
    """
    uid = str(event.get('user_id') or '')
    gid = event.get('group_id', LLBOT_GROUP)
    sender = event.get('sender') or {}
    nick = str(sender.get('card') or sender.get('nickname') or uid)
    raw = str(event.get('raw_message') or '')
    if uid not in OWNERS and uid not in ADMINS:
        return
    pending = _rcon_ops.pending_confirmation(uid)
    if not pending:
        send_group_msg(f'[CQ:at,qq={uid}] 刚才那条已经过了 90 秒，没有执行；要做的话再说一次吧', api=WIKI_API)
        return
    target = pending.get('server')
    if _sanae_ai._has_cancellation_intent(raw):
        action = 'cancel'
        reply = _rcon_ops.dispatch('取消确认', nick, uid, True)
    elif not target:
        action = 'refused'
        reply = '待确认命令缺少服务器目标，已拒绝执行。'
    else:
        action = 'confirm'
        reply = _rcon_ops.dispatch('确认 ' + pending['code'], nick, uid, True,
                                   query_fn=_server_query_backend(target), server=target)
    prefix = f'{server_prefix(target)} ' if target else ''
    send_group_msg(strip_reply_period(f'[CQ:at,qq={uid}] {prefix}{reply}'), api=WIKI_API)
    print(f'[rcon] 自然确认: user={uid} group={gid} action={action} label={pending.get("label", "")[:60]}', flush=True)

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


# mod_info (round 30) asks MC 百科 through the same search as !wiki.
_sanae_local_ops.WIKI_FETCH = _fetch_wiki


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
    other = re.fullmatch(r'解绑\s+([A-Za-z0-9_]{1,16})', body)
    if other:
        if not privileged:
            return '只有管理员能解除别人的绑定；解绑自己的发 !解绑。'
        item = PLAYER_BINDINGS.get_name(other.group(1))
        if not item:
            return f'{other.group(1)} 没有被任何 QQ 绑定。'
        PLAYER_BINDINGS.unbind(item['qq'])
        return f'已解除 {item["name"]} 和 QQ {item["qq"]} 的绑定。'
    match = re.fullmatch(r'绑定\s+([A-Za-z0-9_]{1,16})', body)
    if match:
        known = _binding_known_names()
        if not known:
            return '现在读不到服务器的登录记录，核对不了这个游戏 ID，稍后再绑。'
        real = known.get(match.group(1).casefold())
        if not real:
            return (f'服务器的登录记录里没有 {match.group(1)}。名字要和游戏里一模一样；'
                    '新玩家先进一次服再绑定。')
        ok, message = PLAYER_BINDINGS.bind(uid, real)
        return message + ('之后问“我的游戏时长”“我的研究进度”这类问题时，我就按这个名字查。' if ok else '')
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


def _explicit_media_command(raw):
    text = re.sub(r'\[CQ:[^\]]*\]', '', str(raw)).strip()
    return bool(re.match(r'^[!！]\s*(?:转写|语音转写|听语音)(?:\s|$)', text))


def _event_mentions_sanae(event, raw=None):
    """Only an at segment for this bot counts; @all and other people do not."""
    self_id = str(event.get('self_id') or WIKI_BOT_QQ)
    message = event.get('message')
    if isinstance(message, list):
        # Structured segments are authoritative: quoted text resembling CQ
        # markup must not become a mention or override the actual recipient.
        return any(isinstance(seg, dict) and seg.get('type') == 'at' and
                   isinstance(seg.get('data'), dict) and
                   str(seg['data'].get('qq', '')) == self_id for seg in message)
    current = str(event.get('raw_message', '') if raw is None else raw)
    pattern = r'\[CQ:at,qq=' + re.escape(self_id) + r'(?:,|\])'
    return bool(re.search(pattern, current))


def _event_direct_name_call(event):
    raw = str(event.get('raw_message', '') or '')
    if not raw or _event_mentions_sanae(event):
        return False
    text = re.sub(r'\[CQ:[^\]]*\]', '', raw, flags=re.I).strip()
    # Any message whose cleaned text starts with the bot name is an explicit call.
    # Keep the gate prefix-only; do not enable ambient replies for names appearing later.
    return bool(re.match(BOT_NAME_PATTERN, text, re.I))


def _sanae_capability_question(event):
    if not (_event_mentions_sanae(event) or _event_direct_name_call(event)):
        return False
    text = re.sub(r'\[CQ:[^\]]*\]', '', str(event.get('raw_message') or '')).strip()
    text = re.sub(r'^@?' + BOT_NAME_PATTERN + r'[\s，,：:]*', '', text, flags=re.I).strip(' ？?！!。')
    return bool(re.fullmatch(
        r'(?:你(?:都|还)?(?:会|能)(?:做|干)什么|你(?:都|还)?(?:有|会|支持)(?:什么|哪些)(?:指令|命令|功能)|'
        r'(?:你的|你有|你有哪些|有|有哪些|有什么|看看|查看|列出|帮我列一下)?(?:可用的)?'
        r'(?:指令|命令|功能)(?:列表|帮助|有哪些|有什么|是什么|介绍|怎么用)?|(?:你)?(?:怎么用|能干嘛|能做啥|会什么|会啥))[呀啊呢吗]?', text))


def _ai_reply_trigger(event):
    """Recheck explicit invocation at both queue and worker boundaries.

    force bypasses model-side name matching, never the bridge invocation gate.
    Ordinary named chat stays in Social Lite; named ops/recall are explicit asks.
    """
    raw = str(event.get('raw_message', '') or '')
    uid = str(event.get('user_id') or '')
    privileged = uid in OWNERS or uid in ADMINS
    if _event_mentions_sanae(event):
        return 'mention-self'
    if privileged and re.match(r'^[!！]\s*问\s+(.+)$', raw, re.S):
        return 'ask-command'
    if SANAE_NAME_CALL_ENABLED and _event_direct_name_call(event):
        return 'direct-name'
    if SANAE_MENTION_ONLY:
        return ''
    direct = _event_direct_name_call(event)
    if direct and _natural_server_operation_candidate(raw, privileged, direct_call=True,
                                                      group_id=event.get('group_id'), user_id=uid):
        return 'direct-name-operation'
    if direct and _sanae_ai.chat_recall.is_recall(raw):
        return 'direct-name-recall'
    if _pending_natural_operation_reply(raw, uid, privileged):
        return 'pending-confirmation'
    return ''


def _queue_sanae_reply(callback, event, force=False):
    trigger = _ai_reply_trigger(event)
    if not trigger:
        return False
    if _coalescible_chat(event):
        queued = CONVERSATION_TURNS.submit(event,
            lambda merged: _start_ai_worker(callback, args=(merged, force)), chat_only=True)
        if not queued:
            queued = _start_ai_worker(callback, args=(event, force))
    else:
        queued = _start_ai_worker(callback, args=(event, force))
    print('[sanae-route] channel=ai trigger={} message_id={} user={} queued={}'.format(
        trigger, event.get('message_id', ''), event.get('user_id', ''), bool(queued)), flush=True)
    return queued


def _coalescible_chat(event):
    raw = str(event.get('raw_message') or '')
    text = re.sub(r'\[CQ:[^\]]*\]', '', raw).strip()
    uid = str(event.get('user_id') or '')
    privileged = uid in OWNERS or uid in ADMINS
    if (not text or len(text) > 600 or re.match(r'^[!！/]', text)
            or _is_ai_operation_turn(raw, privileged, uid)
            or _sanae_ai.chat_recall.is_recall(raw)
            or media_segments(event, ('image', 'record', 'audio', 'video', 'file', 'forward'))):
        return False
    # These stay on the existing immediate QA/tool/media route.
    return not re.search(r'搜索|搜一下|查一下|查询|查资料|联网|网址|https?://|分析|详细|推导|写代码|编程|翻译|总结|读取|截图|查看|执行|命令|配置|重启|关机|备份|下载|上传|权限|服务器|TPS|RCON|什么是|是什么|定义|原理|计算|求解|方程|公式|解释|(?:Python|JavaScript|SQL|C\+\+)|怎么(?:做|用|设置|解决|安装)|如何', text, re.I)


def _schedule_social_reply(callback, event):
    SOCIAL_LITE.record_incoming(event)
    allowed, reason = SOCIAL_LITE.should_reply(event)
    if not allowed:
        return False, reason
    def dispatch(merged):
        context = SOCIAL_LITE._context(merged.get('group_id'), [merged])
        return _start_ai_worker(callback, args=(merged, context))
    if _coalescible_chat(event) and SOCIAL_LITE.is_explicit_interaction(event):
        if CONVERSATION_TURNS.submit(event, dispatch, chat_only=True):
            return True, reason
    return dispatch(event), reason


def _media_request_reason(raw, event):
    text = re.sub(r'\[CQ:[^\]]*\]', '', str(raw)).strip()
    if _sanae_ai.chat_recall.is_recall(raw) and not _explicit_media_command(raw) and not re.search(r'图片|看图|视频|语音|音频', text):
        return ''
    if not media_segments(event, ('record', 'audio', 'video', 'file', 'reply', 'forward')):
        return ''
    if _explicit_media_question(raw):
        return 'media-command'
    if _event_mentions_sanae(event, raw):
        return 'mention-self'
    if (SANAE_NAME_CALL_ENABLED or not SANAE_MENTION_ONLY) and _event_direct_name_call(event):
        return 'direct-name'
    return ''


def _media_ai_requested(raw, event):
    return bool(_media_request_reason(raw, event))


def _explicit_media_question(raw):
    text = re.sub(r'\[CQ:[^\]]*\]', '', str(raw)).strip()
    return _explicit_media_command(raw) or bool(re.match(r'^[!！]\s*问(?:\s|$)', text))


def _media_reply_thread(event, raw, uid, gid, privileged, text_reply=None):
    started_at = time.monotonic()
    budget = Budget(_sanae_ai.limits.LONG_REQUEST_SECONDS)
    preprocess_budget = Budget(120, deadline=budget.deadline, parent=budget)
    try:
        result = bounded_call(lambda: _analyze_media(event, WIKI_API, raw, preprocess_budget),
                              budget=preprocess_budget, seconds=120)
        if result['status'] == 'no media' and not _explicit_media_command(raw) and text_reply is not None:
            # Only a quoted or forwarded text: answer like any other @ message. The bare call
            # below had no server, nickname, chat context or short-reply mode, so on 10-26 she
            # said the market "could not be reached" (its tool refused without a server),
            # lectured "我也要" as a 涩图 request and called herself "群里的 AI 角色".
            text_reply(event, True)
            return
        if result['status'] == 'no media' and not _explicit_media_command(raw):
            answer = _sanae_ai.sanae_reply(raw, uid, privileged=privileged, group_id=gid,
                force=True, request_budget=budget, request_started_at=started_at)
        elif result['evidence']:
            selected, _, explicit, _ = _command_target(raw, gid, uid)
            answer = _sanae_ai.sanae_reply(raw, uid, privileged=privileged, group_id=gid, force=True,
                selected_server=selected, explicit_server=explicit, request_budget=budget,
                request_started_at=started_at,
                evidence=result['evidence'], media_fingerprints=result['fingerprints'])
        else:
            answer = ('没有找到可处理的语音或视频。' if result['status'] == 'no media' else result['status'])
        if answer:
            send_group_msg(strip_reply_period(f'[CQ:at,qq={uid}] {answer}'), api=WIKI_API)
    except (RequestTimeout, RequestBusy):
        budget.cancelled.set()
        send_group_msg(f'[CQ:at,qq={uid}] 本次媒体处理超时或队列繁忙，请稍后再试。', api=WIKI_API)
    except Exception as exc:
        print(f'[bridge-media] processing failed: {type(exc).__name__}', flush=True)
        send_group_msg(f'[CQ:at,qq={uid}] 本次媒体暂不可用。', api=WIKI_API)


def _knowledge_reply_thread(event, raw, uid, privileged):
    budget = Budget(45)
    fingerprints = ()
    has_media = bool(media_segments(event, ('record', 'audio', 'video', 'reply', 'forward')))
    clean = re.sub(r'\[CQ:[^\]]*\]', '', raw).strip()
    wants_media = bool(re.match(r'^[!！]\s*知识库\s*(?:记住|查询)', clean))
    if wants_media and has_media and (privileged or '查询' in clean):
        try:
            fingerprints = bounded_call(lambda: media_fingerprints(event, WIKI_API), budget=budget, seconds=45)
        except Exception:
            pass
    reply = _sanae_ai.knowledge_command(raw, privileged, fingerprints)
    if reply:
        if wants_media and has_media and not fingerprints:
            reply += '\n媒体指纹未取得；本次仅按文本处理。'
        send_group_msg(reply, api=WIKI_API)



def _chat_relay_enabled():
    try:
        with open(CHAT_RELAY_STATE, encoding='utf-8') as stream:
            return bool(json.load(stream).get('enabled', True))
    except (OSError, ValueError):
        return True


def _set_chat_relay(enabled, uid):
    _os.makedirs(_os.path.dirname(CHAT_RELAY_STATE), exist_ok=True)
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
        payload = json_bytes({"group_id": int(REPLY_GROUP.get()), "message": text})
        req = urllib.request.Request(
            api + "/send_group_msg", data=payload,
            headers=onebot_headers(), method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = utf8_text(resp.read())[:500]
        ok, _ = onebot_success(raw)
        if not ok:
            print(f"[回执失败] OneBot业务失败: {raw[:200]}", flush=True)
        elif REPLY_PERSON.get() and str(REPLY_GROUP.get()) == str(SOCIAL_LITE_GROUP):
            # Use the actual OneBot receipt, never a fabricated reply ID.
            receipt = json.loads(raw)
            mid = (receipt.get('data') or {}).get('message_id')
            if mid is not None:
                ids = REPLY_MESSAGE_IDS.get()
                if ids is not None:
                    ids.append(str(mid))
                try:
                    FEEDBACK_STORE.remember_reply(mid, REPLY_PERSON.get(), group_id=REPLY_GROUP.get())
                except Exception as exc:
                    print(f'[feedback] reply receipt save failed: {type(exc).__name__}', flush=True)
        return ok
    except Exception as e:
        print(f"[回执失败] {e}", flush=True)
        return False

def send_private_msg(qq, text):
    try:
        payload = json_bytes({"user_id": int(qq), "message": text})
        req = urllib.request.Request(
            WIKI_API + "/send_private_msg", data=payload,
            headers=onebot_headers(), method="POST")
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



def _send_web_answer(event, body, reply, group_id, *, prefix=''):
    """Deliver the complete sourced answer in at most eight semantic bubbles."""
    if not _reply_current(event):
        return False
    if not body or not reply.startswith(body):
        return _send_ai_text(prefix + reply, group_id)
    parts = split_informative_reply(body)
    if not parts:
        return False
    suffix = reply[len(body):]
    messages = [(prefix if index == 0 else '') + part +
                (suffix if index == len(parts) - 1 else '')
                for index, part in enumerate(parts)]
    messages = [strip_reply_period(message) for message in messages]
    if len(messages) > 8 or any(len(message) > 3500 for message in messages):
        return _send_ai_text(prefix + reply, group_id)
    for index, message in enumerate(messages):
        if not _reply_current(event):
            return False
        try:
            if not send_group_msg(message, api=WIKI_API):
                return False
        except Exception as exc:
            # Delivery may already have happened: stop without replaying parts.
            print(f'[web-reply] delivery stopped: {type(exc).__name__}', flush=True)
            return False
        _expression_sent()
        if index < len(messages) - 1:
            time.sleep(min(1.2, 0.4 + len(parts[index]) / 600))
    return True


def _send_social_answer(event, raw, body, reply, group_id, *, prefix='', learn=False, stickers=True):
    """Send at most eight bubbles including a sticker; never resend a prefix."""
    lines = split_social_reply(body)
    if not lines:
        return False
    suffix = reply[len(body):] if reply.startswith(body) else ''
    # If presentation metadata does not match, preserve the complete reply.
    if not reply.startswith(body):
        lines, body = [reply], reply
    receipts = REPLY_MESSAGE_IDS.get()
    first_receipt = len(receipts) if receipts is not None else 0
    with _auto_sticker(group_id, raw, body, enabled=stickers) as sticker:
        if sticker and len(lines) > 7:
            lines = lines[:6] + ['\n'.join(lines[6:])]
        lines = [strip_reply_period(line) for line in lines]
        for index, line in enumerate(lines):
            message = (prefix if index == 0 else '') + line
            last = index == len(lines) - 1
            if last:
                message += suffix
            if not _reply_current(event):
                return False
            delivered = send_group_msg(message, api=WIKI_API)
            if not delivered:
                return False
            _expression_sent()
            if not last:
                time.sleep(0.35)
        if learn and _reply_current(event):
            def remember():
                REPLY_LESSONS.record_reply(event, '\n'.join(lines),
                    reply_message_ids=tuple(receipts[first_receipt:]) if receipts is not None else (),
                    expected_generation=REPLY_LESSON_GENERATION.get())
            try:
                turn = event.get('_conversation_turn')
                if turn is None:
                    remember()
                else:
                    turn.commit_if_current(remember)
            except Exception as exc:
                # Text is already visible; a failed journal write must not resend it.
                print(f'[reply-lessons] delivery record failed: {type(exc).__name__}', flush=True)
        if sticker and _reply_current(event):
            try:
                sticker['delivered'] = bool(send_group_msg(sticker['image'], api=WIKI_API))
            except Exception as exc:
                print(f'[stickers] send failed: {type(exc).__name__}', flush=True)
    return True


# Questions she is still answering, per group (round 31). 10-28 07:36, 54 s into a 79 s answer:
# "糟糕给早苗问宕机了" got a chat reply "刚才没看见你叫我，要问什么再跟我说一遍吧".
_AI_PENDING = {}
_AI_PENDING_LOCK = threading.Lock()


# The newest AI turn per (group, person): a bare "@早苗" followed by the real question a few seconds
# later gets one answer, not two (10-28 17:50/17:51: two long reports on the same freeze).
_AI_LATEST = {}


def _bare_call(raw):
    text = re.sub(r'\[CQ:[^\]]*\]', '', str(raw or ''))
    text = re.sub(BOT_NAME_PATTERN, '', text, flags=re.I)
    return not re.sub(r'[\s，,。.!！?？~～、：:]+', '', text)


def _superseded_bare_call(event):
    """True when this turn was only a call of her name and the same person has asked something since."""
    if not _bare_call(event.get('raw_message')):
        return False
    with _AI_PENDING_LOCK:
        latest = _AI_LATEST.get((str(event.get('group_id')), str(event.get('user_id'))))
    return bool(latest and latest != str(event.get('message_id')))


def _ai_pending_start(event):
    with _AI_PENDING_LOCK:
        _AI_LATEST[(str(event.get('group_id')), str(event.get('user_id')))] = str(event.get('message_id'))
        if len(_AI_LATEST) > 500:
            _AI_LATEST.pop(next(iter(_AI_LATEST)))
    sender = event.get('sender') or {}
    text = re.sub(r'\[CQ:[^\]]*\]', '', str(event.get('raw_message') or '')).strip()
    row = {'user_id': str(event.get('user_id') or ''), 'name': str(sender.get('card') or sender.get('nickname') or '')[:24],
           'text': text[:80], 'started': time.time()}
    with _AI_PENDING_LOCK:
        _AI_PENDING.setdefault(str(event.get('group_id')), {})[str(event.get('message_id'))] = row


def _ai_pending_done(event):
    with _AI_PENDING_LOCK:
        rows = _AI_PENDING.get(str(event.get('group_id'))) or {}
        rows.pop(str(event.get('message_id')), None)


def _ai_pending_note(group_id, exclude_message_id=None, now=None):
    """What she is still working on, for a chat reply in the same group; '' when nothing is."""
    now = time.time() if now is None else now
    with _AI_PENDING_LOCK:
        rows = [row for key, row in (_AI_PENDING.get(str(group_id)) or {}).items()
                if key != str(exclude_message_id) and now - row['started'] < 300]
    if not rows:
        return ''
    asks = '；'.join(f'{row["name"] or "QQ " + row["user_id"]}（QQ {row["user_id"]}）{int(now - row["started"])} 秒前问的'
                    f'“{row["text"]}”' for row in rows[:3])
    return ('〖你手上正在处理的问题〗' + asks + '。这个问题你已经在另一条回复里查着（不是这一轮），'
            '查好会自动发出来，不用对方再叫你；这是“做不到等会儿再告诉你”那条的例外。'
            '有人说你宕机、卡住、没反应时，就说还在查那个问题、马上好；不要说没看见、没叫你，也不要让对方再问一次。')


def _commit_social_outgoing(event, text):
    """Commit local history only while this reply's sources are still live."""
    source_ids = list(event.get('_conversation_source_ids') or [event.get('message_id')])
    source_ids.extend(REPLY_MESSAGE_IDS.get() or ())
    def remember():
        return REPLY_LESSONS.commit_if_generation(
            event.get('group_id'), event.get('user_id'), REPLY_LESSON_GENERATION.get(),
            lambda: SOCIAL_LITE.record_outgoing(event.get('group_id'), text,
                reply_to_user_id=event.get('user_id')),
            source_ids=source_ids)
    turn = event.get('_conversation_turn')
    return remember() if turn is None else turn.commit_if_current(remember)


def _send_ai_text(text, group_id):
    """Show the brief directly and fold the optional complete timeline."""
    text = strip_reply_period(text)
    detail_marker = '\n\n' + _sanae_ai.chat_summary.DETAIL_HEADER
    if (_sanae_ai.chat_summary.BRIEF_HEADER in text and detail_marker in text):
        overview, detail = text.split(detail_marker, 1)
        if not send_group_msg(overview + '\n完整时间线见下方展开', api=WIKI_API):
            return False
        _expression_sent()
        return _send_folded_ai_text(_sanae_ai.chat_summary.DETAIL_HEADER + detail, group_id)
    if len(text) <= 3500:
        ok = send_group_msg(text, api=WIKI_API)
    else:
        ok = _send_folded_ai_text(text, group_id)
    if ok:
        _expression_sent()
    return ok


def _send_folded_ai_text(text, group_id):
    """One folded message; uncertain delivery never triggers automatic replay."""
    # Text segments prevent model-produced CQ markup becoming executable media.
    mention = re.match(r'^\[CQ:at,qq=(\d+)\]\s*', text)
    if mention:
        text = text[mention.end():]
    pages = [text[i:i+3000] for i in range(0, len(text), 3000)]
    payload = {'group_id': int(group_id), 'source': BOT_NAME + ' · 长文回答',
               'summary': '分析与总结，点击展开',
               'messages': [{'type': 'node', 'data': {'name': BOT_NAME, 'uin': WIKI_BOT_QQ,
                    'content': [{'type': 'text', 'data': {'text': page}}]}} for page in pages]}
    if mention:
        payload['messages'][0]['data']['content'].insert(0, {'type':'at','data':{'qq':mention[1]}})
    try:
        req = urllib.request.Request(WIKI_API.rstrip('/')+'/send_group_forward_msg',
            data=json_bytes(payload), headers=onebot_headers(), method='POST')
        with urllib.request.urlopen(req, timeout=15) as response:
            result = utf8_text(response.read(1024*1024))
        ok, _ = onebot_success(result)
        return ok
    except Exception as exc:
        # Receipt may be lost after delivery: never replay the answer on timeout.
        print('[sanae] long answer receipt unavailable: '+type(exc).__name__, flush=True)
        return False


def _send_inventory_reply(reply, group_id):
    """One folded message, or a single navigable page on unsupported OneBot builds."""
    pages = reply.pages
    if len(pages) <= 100 and sum(map(len, pages)) <= 200000:
        nodes = [{'type': 'node', 'data': {'name': BOT_NAME, 'uin': WIKI_BOT_QQ,
                 'content': [{'type': 'text', 'data': {'text': page}}]}} for page in pages]
        payload = {'group_id': int(group_id), 'messages': nodes,
                   'source': '服务器模组清单', 'summary': '完整分类清单，点击展开'}
        try:
            req = urllib.request.Request(WIKI_API.rstrip('/') + '/send_group_forward_msg',
                data=json_bytes(payload), headers=onebot_headers(), method='POST')
            with urllib.request.urlopen(req, timeout=15) as response:
                body = json.loads(response.read(1024 * 1024).decode('utf-8'))
            if onebot_success(body):
                return True
        except Exception as exc:
            print(f'[ops-query] folded list unavailable: {type(exc).__name__}', flush=True)
    text = pages[0] + '\n合并转发暂不可用，完整清单共 %d 页；发送 %s 2 等页码继续查看。' % (len(pages), reply.page_command)
    return send_group_msg(text, api=WIKI_API)


class BridgeHandler(BaseHTTPRequestHandler):
    timeout = 5.0
    def _read_body(self, max_bytes=2 * 1024 * 1024):
        """支持 Content-Length 和 chunked 两种编码。"""
        if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
            body = b''
            while True:
                line = self.rfile.readline(4097)
                if not line or len(line) > 4096:
                    raise ValueError('invalid chunk header')
                try:
                    size = int(line.strip().split(b';', 1)[0], 16)
                except ValueError:
                    raise ValueError('invalid chunk header') from None
                if size < 0:
                    raise ValueError('negative chunk size')
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(4097)
                        if not trailer or len(trailer) > 4096:
                            raise ValueError('invalid chunk trailer')
                        if trailer in (b'\r\n', b'\n'):
                            break
                    break
                if len(body) + size > max_bytes:
                    raise ValueError('request body too large')
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b'\r\n':
                    raise ValueError('incomplete chunk')
                body += chunk
            return body
        length = int(self.headers.get('Content-Length', 0))
        if length < 0 or length > max_bytes:
            raise ValueError('request body too large')
        body = self.rfile.read(length) if length else b''
        if len(body) != length:
            raise ValueError('incomplete body')
        return body

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
            if not isinstance(event, dict):
                raise ValueError('event must be an object')
        except (ValueError, UnicodeError):
            self._respond(400, '{"status":"error","retcode":400}')
            return
        # Acknowledge events which have no Sanae route without putting them
        # into the bounded single-worker inbox.  This preserves OneBot's
        # successful HTTP delivery while preventing unrelated group traffic
        # from starving the configured groups.
        if not _event_needs_bridge(event):
            self._respond(200, '{"status":"ok"}')
            return
        result = self.server.inbox.submit(event, lambda: self._process_event(event))
        if result == 'full':
            self._respond(503, '{"status":"error","retcode":503}')
        else:
            self._respond(200, '{"status":"ok"}')

    def _process_event(self, event):
        print('[recv] post={} type={} group={}'.format(
            event.get('post_type', ''), event.get('message_type', ''),
            event.get('group_id', '')), flush=True)

        # Route all asynchronous replies spawned by this event back to its
        # originating group.  bridge_runtime propagates this context into
        # worker threads.
        if event.get('message_type') == 'group':
            try:
                REPLY_GROUP.set(int(event.get('group_id')))
            except (TypeError, ValueError):
                REPLY_GROUP.set(LLBOT_GROUP)

        if (event.get('post_type') == 'notice' and event.get('notice_type') == 'group_recall'
                and str(event.get('group_id')) == str(SOCIAL_LITE_GROUP)):
            try:
                CONVERSATION_MEMORY.retract(event.get('group_id'), event.get('message_id'))
                EXPRESSION_MEMORY.retract(event.get('group_id'), event.get('message_id'))
                REPLY_LESSONS.retract(event.get('group_id'), event.get('message_id'))
                SOCIAL_LITE.retract(event.get('group_id'), event.get('message_id'))
                SILENT_VISION.retract(event.get('group_id'), event.get('message_id'))
                CONVERSATION_TURNS.retract(event.get('group_id'), event.get('message_id'))
                FEEDBACK_STORE.retract(event.get('message_id'), group_id=event.get('group_id'))
                # Continuity over privacy (owner decision 2026-10-25): a recall no
                # longer wipes the group's recent excerpts or the sender's chat
                # history; only records sourced from this message go.  !忘记 still clears.
            except Exception as exc:
                print(f'[conversation-memory] recall failed: {type(exc).__name__}', flush=True)
            return

        if event.get('post_type') == 'notice' and event.get('notice_type') == 'notify':
            try:
                _handle_notify(event)
            except Exception as exc:
                print(f'[notify] failed: {type(exc).__name__}', flush=True)
            return

        # Shop queries stay scoped to their configured groups and explicit mentions.
        if _shop_query.is_query(event):
            _start_worker(_shop_query.reply, args=(event,))
            return

        # 私聊消息：转发给狗蛋（生产环境已关闭，SANAE_PRIVATE_FORWARD=0）
        if event.get('post_type') == 'message' and event.get('message_type') == 'private':
            uid = str(event.get('user_id', ''))
            if (PRIVATE_FORWARD_ENABLED and uid != str(event.get('self_id', ''))
                    and uid not in HISTORICAL_BOT_QQ):
                _start_worker(self._forward_private, args=(event,))
            return

        # 群消息
        if event.get('post_type') == 'message' and event.get('message_type') == 'group':
            gid = event.get('group_id')
            is_bot_self = (str(event.get('user_id', '')) == str(event.get('self_id', '')))
            is_wiki_bot = (str(event.get('user_id', '')) in HISTORICAL_BOT_QQ)
            if _bridge_group_enabled(gid) and not is_bot_self and not is_wiki_bot:
                raw = str(event.get('raw_message', ''))
                uid = str(event.get('user_id', ''))
                privileged = uid in OWNERS or uid in ADMINS

                if str(gid) == str(SOCIAL_LITE_GROUP):
                    try:
                        SOCIAL_LITE.observe_closing(event)
                    except Exception as exc:
                        print(f'[social-lite] closing state failed: {type(exc).__name__}', flush=True)

                # 0) 重复回收。明确点名是用户主动召唤，不应被普通消息去重吞掉。
                direct_name_candidate = self._is_direct_name_call(event)
                at_candidate = self._is_at_bot(event)
                # An explicit @ is always allowed; without it only the
                # legacy opt-out mode may use natural-name/ambient routes.
                implicit_ai_allowed = (not SANAE_MENTION_ONLY) or at_candidate
                pending_operation_candidate = (implicit_ai_allowed and
                    _pending_natural_operation_reply(raw, uid, privileged))
                natural_operation_candidate = _natural_server_operation_candidate(
                    raw, privileged,
                    direct_name_candidate and not SANAE_MENTION_ONLY, at_candidate,
                    group_id=gid, user_id=uid)
                # The AI judge sees every text message, duplicates included,
                # before any routing below can return early.
                _fun_mute_observe(event, raw, at_candidate or direct_name_candidate)
                _bug_observe(event, raw)
                # Passive images have their own bounded worker. They neither
                # wake a reply nor enter the archive or long-term memory.
                # Explicit image questions retain their existing vision route.
                # Run before text dedup: two different pure images both have
                # empty normalized text. SilentVision dedups source/image IDs.
                clean_text = re.sub(r'\[CQ:[^\]]*\]', '', raw).strip()
                if (SILENT_VISION_ENABLED and str(gid) == str(SOCIAL_LITE_GROUP)
                        and media_segments(event, ('image',))
                        and not at_candidate
                        and not (direct_name_candidate and
                                 (SANAE_NAME_CALL_ENABLED or not SANAE_MENTION_ONLY))
                        and not re.match(r'^[!！/]', clean_text)):
                    try:
                        SILENT_VISION.observe(event)
                    except Exception as exc:
                        print(f'[silent-vision] enqueue failed: {type(exc).__name__}', flush=True)

                if (_is_duplicate(raw, uid) and
                        not (direct_name_candidate or at_candidate or
                             pending_operation_candidate or natural_operation_candidate)):
                    return

                # Host network diagnostics run independently of server selection and AI.
                if _network_commands.handle(raw, lambda text: send_group_msg(text, api=WIKI_API)):
                    return

                # TPS/在线查询必须独立于 AI、记忆和贴纸状态。
                # 即使社交持久化故障，也应能直接走只读 RCON 并回群。
                qk = _match_query(raw)
                if qk:
                    try:
                        target, _cleaned, _explicit, _error = _command_target(raw, gid, uid)
                    except Exception as exc:
                        print(f'[rcon] 读取操作目标失败: {type(exc).__name__}', flush=True)
                        target = None
                    # With more than one enabled backend, never let a missing
                    # group default turn a new-group query into an all-server
                    # aggregate (which could expose the old production host).
                    if target is None and (len(list_servers()) > 1 or
                                           not _single_server_fallback_allowed(gid)):
                        reply = (f'{server_hint()} 尚未设置操作目标。发送 !服 查看当前可用服；'
                                 f'也可以在本条命令中加 {server_hint()}。')
                        _start_worker(lambda t=reply: send_group_msg(t, api=WIKI_API))
                        print(f"[rcon] 查询拒绝: user={uid} type={qk} target=none", flush=True)
                        return
                    _start_worker(self._rcon_query_and_reply, args=(qk, target))
                    print(f"[rcon] 查询: user={uid} type={qk} target="
                          f"{target['id'] if target else 'all'}", flush=True)
                    return

                # Memory commands operate only on this sender in this group.
                try:
                    memory_reply = CONVERSATION_MEMORY.command(event)
                    if memory_reply:
                        if re.match(r'^\s*[!！]忘记(?:\s|$)', re.sub(r'\[CQ:at,[^\]]*\]', '', raw)):
                            REPLY_LESSONS.forget(gid, uid)
                            _forget_conversation_context(gid, uid)
                            EXPRESSION_MEMORY.forget(gid, uid)
                            FEEDBACK_STORE.forget(uid, group_id=gid)
                        _start_worker(lambda t=memory_reply: send_group_msg(t, api=WIKI_API))
                        return
                except Exception as exc:
                    print(f'[conversation-memory] command failed: {type(exc).__name__}', flush=True)
                    if (str(gid) == str(SOCIAL_LITE_GROUP) and re.match(
                            r'^\s*[!！](?:记忆|忘记|记住)(?:\s|$)',
                            re.sub(r'\[CQ:at,[^\]]*\]', '', raw))):
                        _start_worker(lambda: send_group_msg('这次记忆操作没有完成，请稍后再试。', api=WIKI_API))
                        return

                if not _is_ai_operation_turn(raw, privileged, uid):
                    try:
                        REPLY_LESSONS.observe(event, directed=(at_candidate or direct_name_candidate))
                        lesson_batch = REPLY_LESSONS.prepare_batch(gid)
                        if lesson_batch:
                            threading.Thread(target=_learn_reply_lessons, args=(lesson_batch,),
                                daemon=True, name='sanae-reply-lessons').start()
                    except Exception as exc:
                        print(f'[reply-lessons] observe failed: {type(exc).__name__}', flush=True)
                    try:
                        if CONVERSATION_MEMORY.observe(event):
                            batch = CONVERSATION_MEMORY.prepare_batch(gid)
                            if batch:
                                threading.Thread(target=_summarize_conversation_memory, args=(batch,),
                                                 daemon=True, name='sanae-memory-summary').start()
                    except Exception as exc:
                        print(f'[conversation-memory] observe failed: {type(exc).__name__}', flush=True)

                if EXPRESSION_LEARNING_ENABLED and not _is_ai_operation_turn(raw, privileged, uid):
                    try:
                        if EXPRESSION_MEMORY.observe(event):
                            expression_batch = EXPRESSION_MEMORY.prepare_batch(gid)
                            if expression_batch:
                                threading.Thread(target=_learn_expressions, args=(expression_batch,),
                                    daemon=True, name='sanae-expression-learning').start()
                    except Exception as exc:
                        print(f'[expression-memory] observe failed: {type(exc).__name__}', flush=True)

                # Long-term archive stores sanitized group activity.
                sender = event.get('sender') or {}
                nick = sender.get('card') or sender.get('nickname') or uid
                archive_text = _normalize(raw)
                if re.match(r'^[!！](?:qq(?:私聊|发群|引用)|cmd|rcon|配置|改配置|知识库记住)\b', archive_text):
                    archive_text = '[管理员 QQ 动作]'
                try:
                    SOCIAL_ARCHIVE.append(gid, event.get('group_name') or str(gid), nick, archive_text)
                except Exception as exc:
                    print(f'[chat-archive] append failed: {type(exc).__name__}', flush=True)
                if str(gid) == str(SOCIAL_LITE_GROUP) and media_segments(event, ('forward',)):
                    # A forwarded chat record shows up as an empty line otherwise.
                    _start_worker(_ingest_forward, args=(event,))

                feedback_text = re.sub(r'\[CQ:(?:at|reply),[^\]]*\]', '', raw).strip()
                if str(gid) == str(SOCIAL_LITE_GROUP):
                    try:
                        feedback_reply = FEEDBACK_STORE.command(feedback_text, uid,
                            privileged=privileged, message_id=_reply_id(event) or None, group_id=gid,
                            source_message_id=event.get('message_id'))
                        if feedback_reply:
                            _start_worker(lambda t=feedback_reply: send_group_msg(t, api=WIKI_API))
                            return
                        if at_candidate or direct_name_candidate:
                            FEEDBACK_STORE.observe_style_request(feedback_text, uid,
                                group_id=gid, source_message_id=event.get('message_id'))
                    except Exception as exc:
                        print(f'[feedback] state failed: {type(exc).__name__}', flush=True)
                        if re.match(r'^[!！]反馈(?:\s|$)', feedback_text):
                            _start_worker(lambda: send_group_msg('这次反馈没有保存，请稍后再试。', api=WIKI_API))
                            return

                sticker_reply = STICKER_CATALOG.command(raw, privileged=privileged)
                if sticker_reply:
                    _start_worker(lambda t=sticker_reply: send_group_msg(t, api=WIKI_API))
                    return

                qq_capability = _qq_capability_command(raw)
                if qq_capability:
                    action, args = qq_capability
                    _start_worker(_qq_capability_thread, args=(event, action, args, privileged))
                    return

                # 操作目标只约束查询和管理；普通群文本始终 fan-out。
                selection_reply = _selection_reply(raw, gid, uid)
                if selection_reply:
                    send_group_msg(selection_reply, api=WIKI_API)
                    return
                if int(gid or 0) in BUG_REPORT_GROUPS and _bugs.COMMAND.match(_normalize(raw)):
                    sender = event.get('sender') or {}
                    bug_reply = _bugs.command(BUG_LEDGER, _normalize(raw), uid,
                                              str(sender.get('card') or sender.get('nickname') or uid)[:24],
                                              int(gid), privileged, event.get('message_id'))
                    if bug_reply:
                        _start_worker(lambda t=bug_reply: send_group_msg(t, api=WIKI_API))
                        print(f'[bug-report] command user={uid} privileged={privileged}', flush=True)
                        return
                if match_ops_command(raw):
                    _start_worker(self._ops_reply_thread, args=(raw, gid, uid, privileged))
                    return
                screenshot_request = _sanae_ai.parse_bluemap_request(raw)
                if screenshot_request:
                    _start_worker(self._screenshot_reply_thread, args=(raw,))
                    return
                # Shared AI knowledge is deterministic and does not invoke a model.
                knowledge_text = re.sub(r'\[CQ:[^\]]*\]', '', raw).strip()
                if re.match(r'^[!！]\s*知识库(?:\s|$|查询|记住|删除)', knowledge_text):
                    _start_ai_worker(_knowledge_reply_thread, args=(event, raw, uid, privileged))
                    return

                binding_reply = _binding_command(raw, uid, privileged)
                if binding_reply:
                    _start_worker(lambda t=binding_reply: send_group_msg(t, api=WIKI_API))
                    return

                media_reply = _media_command(raw, event, uid, privileged)
                if media_reply:
                    _start_worker(lambda t=media_reply: send_group_msg(t, api=WIKI_API))
                    return

                if _normalize(raw) in ('!转图床', '!上传图床'):
                    def image_host_worker():
                        reply = _image_host_command(raw, event, uid, privileged)
                        if reply:
                            send_group_msg(reply, api=WIKI_API)
                    _start_worker(image_host_worker)
                    return

                media_reason = _media_request_reason(raw, event)
                if media_reason:
                    queued = _start_ai_worker(_media_reply_thread,
                                              args=(event, raw, uid, gid, privileged, self._sanae_reply_thread))
                    print(f'[sanae-route] channel=media trigger={media_reason} queued={bool(queued)}', flush=True)
                    return

                if privileged and _normalize(raw) in ('!转发 开', '!转发 关'):
                    enabled = _normalize(raw).endswith('开')
                    _set_chat_relay(enabled, uid)
                    send_group_msg('群到服聊天转发已' + ('开启。' if enabled else '关闭。'), api=WIKI_API)
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
                            console_plan = None
                            if (privileged and target
                                    and _rcon_ops.command_word(cmd_text) in {'cmd', '控制台'}):
                                parts = cmd_text.split(None, 1)
                                console_plan = _console_plan(target, parts[1] if len(parts) > 1 else '')
                            if console_plan and console_plan[0] in {'direct', 'confirm', 'ai'}:
                                start = _start_worker if console_plan[0] == 'direct' else _start_ai_worker
                                if start(self._console_command_thread,
                                         args=(event, console_plan, target, uid, gid)) is False:
                                    send_group_msg(f'{server_prefix(target)} 现在排队的请求太多，'
                                                   '稍后再发一次 !cmd。', api=WIKI_API)
                                print(f"[rcon] 智能控制台: user={uid} kind={console_plan[0]} "
                                      f"target={target['id']}", flush=True)
                                return
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
                        _start_worker(lambda t=reply: send_group_msg(t, api=WIKI_API))
                        print(f"[rcon] 反控命令: user={uid} cmd={cmd_text[:40]} "
                              f"target={target['id'] if target else 'none'} privileged={privileged}", flush=True)
                        return

                # 1.2) !问 <问题> → 早苗 AI（仅群主/管理员，对齐工具包）
                m_ask = re.match(r'^[!！]\s*问\s+(.+)$', raw, re.S)
                if m_ask:
                    priv = (str(uid) in OWNERS or str(uid) in ADMINS)
                    if not priv:
                        send_group_msg('[CQ:at,qq={}] AI 问答仅群主/管理员可用，群友可以 @我 聊天哦~'.format(uid), api=WIKI_API)
                        return
                    _queue_sanae_reply(self._sanae_reply_thread, event, True)
                    print(f"[sanae] !问: user={uid}", flush=True)
                    return

                # 2) MC 百科查询（!wiki/!百科）
                kw = _match_wiki(raw)
                if kw:
                    _start_worker(self._wiki_query_and_reply, args=(kw,))
                    print(f"[wiki] 查询: user={uid} kw={kw}", flush=True)
                    return

                # 3) 模组查询（!mod/!模组 → Modrinth；!cf → CurseForge 官方）
                mk = _match_mod(raw)
                if mk:
                    _start_worker(self._mod_query_and_reply, args=(mk, 'modrinth'))
                    print(f"[mod] 查询: user={uid} kw={mk}", flush=True)
                    return
                ck = _match_cf(raw)
                if ck:
                    _start_worker(self._mod_query_and_reply, args=(ck, 'cf'))
                    print(f"[mod] CF查询: user={uid} kw={ck}", flush=True)
                    return

                # 4) @早苗 → 独立 AI 模块（不走 agent）。生产默认要求
                # OneBot 的真实 at 段；自然点名和普通群聊不再唤醒模型。
                direct_name_call = direct_name_candidate
                if pending_operation_candidate:
                    if _short_operation_reply(raw):
                        _start_worker(_pending_operation_reply_thread, args=(event,))
                        print(f"[sanae] 待确认操作直接处理: user={uid}", flush=True)
                        return
                    _queue_sanae_reply(self._sanae_reply_thread, event, True)
                    print(f"[sanae] 待确认操作自然回复: user={uid}", flush=True)
                    return
                if natural_operation_candidate:
                    _queue_sanae_reply(self._sanae_reply_thread, event, True)
                    print(f"[sanae] 自然服务器操作: user={uid}", flush=True)
                    return
                if (at_candidate or not SANAE_MENTION_ONLY) and _sanae_capability_question(event):
                    help_reply = _rcon_ops.cmd_help(privileged)
                    _start_worker(lambda t=help_reply: send_group_msg(t, api=WIKI_API))
                    return
                if at_candidate:
                    _queue_sanae_reply(self._sanae_reply_thread, event)
                    print(f"[sanae] @早苗: user={uid}", flush=True)
                    return
                if (not SANAE_MENTION_ONLY and direct_name_call and
                        _sanae_ai.chat_recall.is_recall(raw)):
                    _queue_sanae_reply(self._sanae_reply_thread, event, True)
                    return
                # 恢复旧的自动路由：362 群里“早苗……”点名进入社交上下文，
                # 不强制套用 @用户 的直答格式。真实 @ 仍在上方走 AI 通道。
                if (direct_name_call and SANAE_NAME_CALL_ENABLED
                        and media_segments(event, ('image',))):
                    _queue_sanae_reply(self._sanae_reply_thread, event, True)
                    return
                if (direct_name_call and SOCIAL_LITE_ENABLED and
                        gid == SOCIAL_LITE_GROUP):
                    try:
                        scheduled, reason = _schedule_social_reply(self._social_reply_thread, event)
                    except Exception as exc:
                        # 状态文件权限/短暂 I/O 故障不能让明确点名变成无响应。
                        print(f'[sanae] 点名社交调度失败: {type(exc).__name__}', flush=True)
                        _start_worker(lambda: send_group_msg(
                                f'[CQ:at,qq={uid}] 我在，刚才没接稳。把要接的话再发一次，我马上接上。',
                                api=WIKI_API))
                        return
                    print(f"[sanae] 点名早苗转社交上下文: user={uid} scheduled={scheduled} reason={reason}", flush=True)
                    return

                # 其他启用点名的群保留原有 AI 直答兜底；362 群已由上面的
                # Social Lite 自动路由优先接管。
                if SANAE_NAME_CALL_ENABLED and direct_name_call:
                    _queue_sanae_reply(self._sanae_reply_thread, event, True)
                    return

                # A short explicit correction can amend this person's pending
                # directed chat, without waking on unrelated ambient messages.
                if (str(gid) == str(SOCIAL_LITE_GROUP) and _coalescible_chat(event)
                        and CONVERSATION_TURNS.submit(event,
                            lambda merged: _start_ai_worker(self._sanae_reply_thread, (merged, True)),
                            chat_only=True, directed=False)):
                    return

                # Social Lite 只在配置的群启用；本地门控后再单轮唤醒模型。
                if (not SANAE_MENTION_ONLY and SOCIAL_LITE_ENABLED and
                        gid == SOCIAL_LITE_GROUP):
                    try:
                        scheduled, reason = _schedule_social_reply(self._social_reply_thread, event)
                        if scheduled:
                            print(f'[social-lite] scheduled group={gid} reason={reason}', flush=True)
                    except Exception as exc:
                        print(f'[social-lite] state write/read failed: {type(exc).__name__}', flush=True)

                # 5) 群→服 G2S 转发；排除指定账号，不影响其他群聊功能。
                if (RELAY_G2S_ENABLED and str(gid) in {str(x) for x in RELAY_GROUPS}
                        and uid not in G2S_EXCLUDED_QQ
                        and _chat_relay_enabled()):
                    _start_worker(self._relay_g2s, args=(event,))

            return

        # 其他事件（通知等）：静默

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
                print('[private-forward] delivered=True', flush=True)
            else:
                print('[private-forward] delivered=False', flush=True)

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
            msg = f'模组查询出错了（{public_failure_label(e)}），稍后再试~'
        if not msg:
            msg = f'模组站没找到「{keyword}」，换个关键词试试~'
        send_group_msg(msg, api=WIKI_API)

    @_reply_scope
    def _sanae_reply_thread(self, event, force=False):
        """@早苗 / !问 独立回复：正则匹配 → DeepSeek API（带工具）→ 早苗 API 发回（完全不走 agent）。"""
        started = time.monotonic()
        trigger = _ai_reply_trigger(event)
        if not trigger:
            print('[sanae-route] channel=ai blocked=no-invocation message_id={} user={}'.format(
                event.get('message_id', ''), event.get('user_id', '')), flush=True)
            return
        _ai_pending_start(event)
        try:
            raw = str(event.get('raw_message', ''))
            uid = event.get('user_id')
            nick = ((event.get('sender') or {}).get('card') or (event.get('sender') or {}).get('nickname') or '')
            gid = event.get('group_id', LLBOT_GROUP)
            privileged = str(uid) in OWNERS or str(uid) in ADMINS

            def _interim(m):
                if not _reply_current(event):
                    return
                try:
                    send_group_msg(strip_reply_period(m), api=WIKI_API)
                except Exception as e:
                    print(f'[sanae] interim 失败: {e}', flush=True)

            selected_server, raw, target_unambiguous = _ai_operation_target(raw, gid, uid)
            chat_requested = (trigger in {'mention-self', 'direct-name'}
                              and not _is_ai_operation_turn(raw, privileged, uid))
            context_reference = ''
            social_style_override = ''
            # 运维工具的意图识别必须基于干净的当前消息。把长期记忆或
            # 风格策略拼进来，会让其中的“解释/说明”等词误关掉 run_rcon。
            if gid == SOCIAL_LITE_GROUP and not _is_ai_operation_turn(raw, privileged, uid):
                social_style_override = FEEDBACK_STORE.style_override(uid, group_id=gid)
                extras = ['\n〖回复策略〗' + FEEDBACK_STORE.strategy_hint(uid, group_id=gid)]
                try:
                    personal_context = CONVERSATION_MEMORY.context(gid, uid, raw,
                        message_id=event.get('message_id'))
                    if personal_context:
                        extras.append(personal_context)
                except Exception as exc:
                    print(f'[conversation-memory] context failed: {type(exc).__name__}', flush=True)
                style_hint = SOCIAL_LITE.expression_style_hint(gid, event)
                if style_hint:
                    extras.append(style_hint)
                closing_hint = SOCIAL_LITE.closing_hint(event)
                if closing_hint:
                    extras.append(closing_hint)
                sticker_rows = STICKER_CATALOG.recommend(raw, limit=3)
                if sticker_rows:
                    extras.append('〖可选贴纸语义〗' + '、'.join(dict.fromkeys(x['mood'] for x in sticker_rows)) + '（仅供内部选择）')
                learned = '' if social_style_override else _expression_context(gid, uid, raw)
                if learned:
                    extras.append(learned)
                profile = _sanae_ai.request_profile(raw, short_reply=chat_requested,
                    context_reference='\n'.join(extras),
                    has_images=bool(media_urls(event, kinds=('image',))),
                    selected_server=selected_server, privileged=privileged,
                    explicit_server=target_unambiguous, user_id=uid)
                lesson = (_lesson_context(gid, uid, raw, social_style_override)
                          if profile['tier'] == 'chat' or social_style_override else '')
                if lesson:
                    extras.append(lesson)
                visual_context = _silent_image_context(event, raw)
                if visual_context:
                    extras.append(visual_context)
                server_status = _social_server_status(gid, raw)
                if server_status:
                    extras.append(server_status)
                context_reference = '\n'.join(extras)
            def _stream(part):
                return _reply_current(event) and _send_ai_text(f'[CQ:at,qq={uid}] {part}', gid)

            reply_metadata = {}
            reply = _sanae_ai.sanae_reply(raw, uid, nick, privileged=privileged,
                                          group_id=gid, send_cb=_interim, force=force,
                                          selected_server=selected_server,
                                          explicit_server=target_unambiguous,
                                          short_reply=chat_requested,
                                          explicit_interaction=True,
                                          context_reference=context_reference,
                                          stream_cb=_stream,
                                          image_urls=media_urls(event, kinds=('image',)),
                                          source_message_id=event.get('message_id'),
                                          conversation_turn=event.get('_conversation_turn'),
                                          social_style_override=social_style_override,
                                          reply_metadata=reply_metadata)
            if not reply_metadata.get('model_answer'):
                _release_expression()
            if reply and _fun_recently_muted(gid, uid):
                print(f'[sanae] 已被玩笑禁言，不回复: user={uid}', flush=True)
                return
            if reply and _superseded_bare_call(event):
                print(f'[sanae] 只叫了名字、之后又问了新问题，这条不发: user={uid} '
                      f"message_id={event.get('message_id', '')}", flush=True)
                return
            if reply and _reply_current(event):
                msg = strip_reply_period(f'[CQ:at,qq={uid}] {reply}')
                body = reply_metadata.get('reply_body', reply)
                enabled = (reply_metadata.get('model_answer') is True and len(msg) <= 3500
                           and _sanae_ai.chat_summary.BRIEF_HEADER not in msg)
                if reply_metadata.get('web_reply') is True:
                    ok = _send_web_answer(event, body, reply, gid,
                        prefix=f'[CQ:at,qq={uid}] ')
                elif reply_metadata.get('social_reply') is True:
                    ok = _send_social_answer(event, raw, body, reply, gid,
                        prefix=f'[CQ:at,qq={uid}] ', learn=True, stickers=enabled)
                else:
                    with _auto_sticker(gid, raw, body, enabled=enabled) as sticker:
                        attached = bool(sticker and len(msg) + len(sticker['image']) + 1 <= 3500)
                        if attached:
                            msg += '\n' + sticker['image']
                        ok = _reply_current(event) and _send_ai_text(msg, gid)
                        if attached:
                            sticker['delivered'] = bool(ok)
                if ok:
                    # Her chat replies only knew what chat replies had said (10-27 20:21: right after
                    # her server-status answer, "给早苗干宕机了" got "你只丢了个聊天记录又没叫我").
                    try:
                        _commit_social_outgoing(event, reply.strip())
                    except Exception as exc:
                        print(f'[social-lite] answer save failed: {type(exc).__name__}', flush=True)
                print(f"[sanae] AI回复: trigger={trigger} message_id={event.get('message_id', '')} "
                      f"user={uid} ok={ok} len={len(reply)} elapsed={time.monotonic() - started:.1f}s "
                      f"fallback={int(bool(reply_metadata.get('text_fallback')))} "
                      f"failure={reply_metadata.get('failure') or '-'}", flush=True)
            else:
                print(f'[sanae] 非 @早苗 或应忽略，不回复: user={uid} msg={raw[:40]} '
                      f"outcome={reply_metadata.get('outcome') or '-'} elapsed={time.monotonic() - started:.1f}s", flush=True)
        except Exception as e:
            print(f'[sanae] 回复异常: {e}', flush=True)
            if not _reply_current(event):
                return
            try:
                send_group_msg(f'[CQ:at,qq={event.get("user_id")}] 嗯？好像有点卡，稍等一下再问我~', api=WIKI_API)
            except Exception:
                pass
        finally:
            _ai_pending_done(event)

    @_reply_scope
    def _social_reply_thread(self, event, context):
        """Social Lite callback: one text-only model turn, no server tools."""
        started = time.monotonic()
        reply_metadata = {}
        try:
            uid = str(event.get('user_id') or '')
            gid = event.get('group_id', LLBOT_GROUP)
            nick = ((event.get('sender') or {}).get('card') or
                    (event.get('sender') or {}).get('nickname') or uid)
            raw = str(event.get('raw_message') or event.get('message') or '').strip()
            feedback_hint = FEEDBACK_STORE.strategy_hint(uid, group_id=gid)
            social_style_override = FEEDBACK_STORE.style_override(uid, group_id=gid)
            style_hint = SOCIAL_LITE.expression_style_hint(gid, event)
            sticker_rows = STICKER_CATALOG.recommend(raw, limit=3)
            # She may quote it when asked about the chat; otherwise it is background.
            prompt = (f'【最近群聊上下文（用于理解语境；被问到聊天记录或刚才聊了什么时可以据此回答，平时不要复述）】\n'
                      f'{context[-3600:]}')
            visual_context = _silent_image_context(event, raw)
            if visual_context:
                prompt += '\n\n' + visual_context
            server_status = _social_server_status(gid, raw)
            if server_status:
                prompt += '\n\n' + server_status
            pending_note = _ai_pending_note(gid, event.get('message_id'))
            if pending_note:
                prompt += '\n\n' + pending_note
            try:
                personal_context = CONVERSATION_MEMORY.context(gid, uid, raw,
                    message_id=event.get('message_id'))
                if personal_context:
                    prompt += '\n\n' + personal_context
            except Exception as exc:
                print(f'[conversation-memory] context failed: {type(exc).__name__}', flush=True)
            learned = '' if social_style_override else _expression_context(gid, uid, raw)
            if learned:
                prompt += '\n\n' + learned
            lesson = _lesson_context(gid, uid, raw, social_style_override)
            if lesson:
                prompt += '\n\n' + lesson
            prompt += '\n\n〖回复策略〗' + feedback_hint
            if style_hint:
                prompt += '\n\n' + style_hint
            closing_hint = SOCIAL_LITE.closing_hint(event)
            if closing_hint:
                prompt += '\n\n' + closing_hint
            if sticker_rows:
                moods = '、'.join(dict.fromkeys(x['mood'] for x in sticker_rows))
                prompt += f'\n〖可选贴纸语义〗{moods}（仅在确有必要时考虑，不要输出贴纸 ID）'
            reply = _sanae_ai.sanae_reply(
                raw, uid, nick, privileged=False, group_id=gid,
                force=True, social_only=True, include_usage_footer=False, context_reference=prompt,
                explicit_interaction=SOCIAL_LITE.is_explicit_interaction(event),
                conversation_turn=event.get('_conversation_turn'), social_style_override=social_style_override,
                reply_metadata=reply_metadata)
            if not reply_metadata.get('model_answer'):
                _release_expression()
            if not _reply_current(event):
                _social_outcome(event, 'stale', started, reply_metadata)
                return
            if not reply or reply.strip().upper() == '[SILENT]':
                _social_outcome(event, reply_metadata.get('outcome') or 'silent', started, reply_metadata)
                return
            if _fun_recently_muted(gid, uid):
                _social_outcome(event, 'muted', started, reply_metadata)
                return
            body = reply_metadata.get('reply_body', reply)
            # The same cadence as direct chat, without a mention or bot label.
            delivered = _send_social_answer(event, raw, body, reply, gid,
                learn=reply_metadata.get('social_reply') is True,
                stickers=reply_metadata.get('model_answer') is True)
            _social_outcome(event, 'replied' if delivered else 'send-failed', started, reply_metadata)
            if delivered:
                try:
                    _commit_social_outgoing(event, reply.strip())
                except Exception as exc:
                    print(f'[social-lite] outgoing save failed: {type(exc).__name__}', flush=True)
                print(f'[social-lite] replied group={gid} user={uid} len={len(reply)}', flush=True)
        except Exception as exc:
            print(f'[social-lite] callback failed: {type(exc).__name__}', flush=True)
            _social_outcome(event, 'error', started, dict(reply_metadata, failure=type(exc).__name__))
            if not _reply_current(event):
                return
            try:
                explicit = SOCIAL_LITE.is_explicit_interaction(event)
            except Exception:
                explicit = False
            if not explicit:
                # An uninvited chime-in that failed is simply dropped.
                return
            try:
                send_group_msg(
                    f'[CQ:at,qq={event.get("user_id")}] 我在，刚才没接稳。把要接的话再发一次，我马上接上。',
                    api=WIKI_API)
            except Exception:
                pass

    def _console_command_thread(self, event, plan, target, uid, gid):
        """Smart `!cmd`: read/low run at once, the rest gets a code, free text goes to her."""
        kind, command, detail = plan
        started = time.monotonic()
        prefix = server_prefix(target)
        backend = _server_query_backend(target)
        mention = ''
        try:
            if kind == 'direct':
                reply = _rcon_ops.run_console_direct(command, uid, query_fn=backend,
                                                     server=target, risk=detail)
            elif kind == 'confirm':
                try:
                    online = query_server(target, 'list')
                except Exception:
                    online = ''
                # Reviewed before the code exists, so the 90 s window is not spent waiting.
                note = _sanae_ai.console_review_note(command, detail, online)
                reply = _rcon_ops.dispatch('cmd ' + command, uid, uid, True,
                                           query_fn=backend, server=target)
                if note and '[高危确认]' in str(reply):
                    reply = f'八千代复核：{note}\n{reply}'
            else:
                mention = f'[CQ:at,qq={uid}] '
                sender = event.get('sender') or {}
                nick = sender.get('card') or sender.get('nickname') or ''

                def _interim(message):
                    try:
                        send_group_msg(strip_reply_period(mention + message), api=WIKI_API)
                    except Exception as exc:
                        print(f'[rcon] 智能控制台 interim 失败: {type(exc).__name__}', flush=True)
                reply = _sanae_ai.sanae_reply(
                    command, uid, nick, privileged=True, group_id=gid, send_cb=_interim,
                    force=True, selected_server=target, explicit_server=True,
                    console_request=True)
                reply = reply or '这条没能理解成可以执行的命令，换个说法或直接写完整命令再发一次 !cmd。'
            reply = f'{mention}{prefix} {reply}'
            if '[高危确认]' in reply and len(list_servers()) > 1:
                reply = reply.replace('发送：!确认', f'发送：{prefix} !确认')
        except Exception as exc:
            print(f'[rcon] 智能控制台异常: {type(exc).__name__}: {exc}', flush=True)
            reply = f'{mention}{prefix} [控制台] 这次没处理完（{public_failure_label(exc)}），不会自动重试。'
        _rcon_ops.record_command_result('cmd ' + command, uid, reply)
        ok = _send_ai_text(reply, gid) if kind == 'ai' else send_group_msg(reply, api=WIKI_API)
        print(f"[rcon] 智能控制台完成: user={uid} kind={kind} cmd={command[:40]} "
              f"target={target['id']} ok={ok} elapsed={time.monotonic() - started:.1f}s", flush=True)

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
        if not selected:
            selected, _ = get_group_default(group_id)
        if selected and selected.get('id') == 'nast':
            _refresh_nast_ops()
        reply = dispatch_ops_command(raw, group_id, user_id, privileged,
                                     query_fn=query_server)
        if not reply:
            return
        if reply.pages:
            _send_inventory_reply(reply, group_id)
            return
        message = reply.text
        if reply.image_path:
            message += f'\n[CQ:image,file={reply.image_path}]'
        ok = send_group_msg(message, api=WIKI_API)
        print(f'[ops-query] command={str(raw)[:40]} ok={ok} image={bool(reply.image_path)}',
              flush=True)

    def _relay_g2s(self, event):
        """群→双服逐服 best-effort 转发；一服失败不影响另一服。"""
        # 工作者再次检查，避免排队或直接调用绕过入口过滤。
        if str(event.get("user_id", "")) in G2S_EXCLUDED_QQ:
            return
        raw = str(event.get('raw_message', ''))
        text = re.sub(r'\[CQ:[^\]]*\]', '', raw).strip()
        if not text:
            return
        sender = event.get('sender', {}) or {}
        nick = sender.get('card') or sender.get('nickname') or str(event.get('user_id', ''))
        relay_target, relay_error = get_group_default(int(event.get('group_id', 0) or 0))
        if not relay_target:
            print(f'[relay-g2s] skipped: no group default ({relay_error or "unknown"})', flush=True)
            return
        for result in fanout_group_message(nick, text, servers=[relay_target]):
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
        """消息是否明确 @ 了早苗本人。"""
        return _event_mentions_sanae(event)

    def _is_direct_name_call(self, event):
        """识别不带 CQ at 的自然点名，例如“早苗，看看这个”。"""
        return _event_direct_name_call(event)

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
    print(f'[sanae-fusion] persona=1.1 sourced-memory=1 turns=1 scoped-feedback=1 expression-learning={int(EXPRESSION_LEARNING_ENABLED)} batch=24 cooldown=600s', flush=True)
    print(f'[silent-vision] enabled={int(SILENT_VISION_ENABLED)} group={SOCIAL_LITE_GROUP} ttl=900s worker=1', flush=True)
    _sanae_ai.start_pricing_refresh()
    if FUN_MUTE_AI_ENABLED:
        FUN_MUTE_JUDGE.start()
        print(f'[fun-mute] AI judge enabled group={FUN_MUTE_GROUP}', flush=True)
    for _bug_judge_thread in _BUG_JUDGES.values():
        _bug_judge_thread.start()
    if BUG_REPORT_GROUPS:
        threading.Thread(target=_bug_digest_loop, name='bug-digest', daemon=True).start()
        print(f'[bug-report] collector groups={",".join(map(str, sorted(BUG_REPORT_GROUPS)))} '
              f'digest={",".join(map(str, sorted(BUG_DIGEST_GROUPS))) or "off"} at {BUG_DIGEST_HOUR}:00', flush=True)
    if _os.environ.get('SANAE_ENABLE_BACKUP_WATCH', '1') == '1':
        _backup_watch.start()
    if _os.environ.get('SANAE_ENABLE_NAST_SYNC', '1') == '1':
        threading.Thread(target=_nast_ops_sync_loop, name='nast-ops-sync', daemon=True).start()
    if _os.environ.get('PLAYER_TRAIL_ENABLED', '0') == '1':
        # Owner decision 10-27: online players' positions every 2 minutes, kept 24 hours, admins only.
        _trail_server = resolve_server(_os.environ.get('PLAYER_TRAIL_SERVER', 'legacy'))
        if _trail_server:
            threading.Thread(target=_player_trail.sampler_loop, name='player-trail', daemon=True, args=(
                _trail_server, lambda command, server=_trail_server: query_server(server, command),
                _os.path.join(str(OPS_STATE_BASE), _trail_server['id'], _player_trail.FILE_NAME))).start()
    if _os.environ.get('SANAE_ENABLE_LEGACY_MONITOR', '0') == '1':
        # Keep the Windows production deployment single-process: the existing
        # monitor relays player chat and join/advancement/crash/ops events
        # to the configured Legacy groups, with QQ echo filtering.
        import legacy_monitor as _legacy_monitor
        _legacy_monitor.UPDATE_SUMMARY_FN = _sanae_ai.summarize_mod_update
        threading.Thread(target=_legacy_monitor.main,
                         name='legacy-monitor', daemon=True).start()
    elif _os.environ.get('SANAE_ENABLE_WEEKLY_REPORT', '0') == '1':
        # Relay stays off: this thread only samples the server and posts the
        # scheduled weekly report; chat, joins and alerts are not forwarded.
        import legacy_monitor as _legacy_monitor
        _legacy_monitor.UPDATE_SUMMARY_FN = _sanae_ai.summarize_mod_update
        threading.Thread(target=_legacy_monitor.collect_main,
                         name='weekly-collector', daemon=True).start()
    print(f"桥接服务启动: 监听 0.0.0.0:{BRIDGE_PORT} → 早苗独立模块体系（!wiki/!mod/RCON/@早苗/G2S/私聊），完全不走 agent", flush=True)
    CallbackServer(('0.0.0.0', BRIDGE_PORT), BridgeHandler, source_ips=CALLBACK_SOURCE_IPS).serve_forever()
