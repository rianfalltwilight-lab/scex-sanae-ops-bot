#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RCON 反控命令模块（2026-08-16 按 upstream QQConsoleBridge 功能实现）
- 命令集对齐工具包 QQConsoleBridge：help/list/day/rules/version/uptime/ip/roll/运势/id（全体）
  + tps/backup/save/seed/weather/say/stop/restart/cmd（群主/管理员）
- RCON 走 rcon_client（NeoForge 自动适配 neoforge tps）
"""
import os
import re
import json
import random
import datetime
import hashlib
import secrets
import subprocess
import sys
import threading
import time
from contextvars import ContextVar
from pathlib import Path

from rcon_client import query, query_tps, query_list
from ops_contract import append_jsonl, command_result
from server_registry import active_server, query_server, server_context
from command_catalog import normalize_console_command

# 服务器根目录（Mac 侧）
SERVER_DIR = Path(os.environ.get("MC_SERVER_DIR", "/Users/goudanli/Downloads/NAST Server1.9.9"))
ADDR = os.environ.get("MC_SERVER_ADDR", "example.invalid:25565")

# 命令权限：普通 vs 管理员
PUBLIC_CMDS = {"help", "帮助", "list", "在线", "day", "date", "time", "日期", "rules", "规则",
               "version", "版本", "uptime", "运行时长", "在线时长", "ip", "地址", "address",
               "roll", "运势", "id", "whoami"}
ADMIN_CMDS = {"tps", "性能", "backup", "备份", "save", "存盘", "seed", "种子",
              "weather", "天气", "say", "公告", "broadcast", "stop", "restart",
              "cmd", "控制台", "确认", "取消确认", "ai", "模型"}
COMMAND_ALIASES = {
    "帮助": "help", "在线": "list", "日期": "day", "规则": "rules", "版本": "version",
    "运行时长": "uptime", "在线时长": "uptime", "地址": "ip", "性能": "tps",
    "备份": "backup", "存盘": "save", "种子": "seed", "天气": "weather",
    "公告": "say", "模型": "ai",
}
HIGH_RISK_CMDS = {"stop", "restart", "cmd", "控制台", "确认", "confirm", "取消确认", "cancel"}
TARGETLESS_CMDS = {"help", "帮助", "roll", "运势", "id", "whoami"}

RISK_TTL_SECONDS = 90
AUDIT_PATH = Path(__file__).with_name("logs") / "ops-audit.jsonl"
RESULT_PATH = Path(__file__).with_name("logs") / "command-results.jsonl"
_PENDING_RISK = {}
_RISK_LOCK = threading.Lock()
_AUDIT_LOCK = threading.Lock()
_QUERY_FN = ContextVar("rcon_ops_query_fn", default=None)


def command_word(command):
    cmd = str(command or '').strip()
    word = cmd.split()[0].lower() if cmd.split() else cmd.lower()
    return COMMAND_ALIASES.get(word, word)


def is_known_command(command):
    word = command_word(command)
    known = {COMMAND_ALIASES.get(value, value) for value in PUBLIC_CMDS | ADMIN_CMDS}
    known.update({"confirm", "cancel"})
    return word in known


def is_high_risk_command(command):
    return command_word(command) in HIGH_RISK_CMDS


def is_targetless_command(command):
    return command_word(command) in {COMMAND_ALIASES.get(value, value) for value in TARGETLESS_CMDS}


def record_command_result(command, uid, reply):
    """Persist a machine-readable result alongside the existing QQ text."""
    text = str(reply or "")
    failed = bool(re.search(r"(失败|不可用|不在线|拒绝|已过期|不存在)", text, re.I))
    server = active_server() or {}
    append_jsonl(RESULT_PATH, command_result(
        command, state="failed" if failed else "success",
        output="" if failed else text, error=text if failed else None,
        actor=str(uid), started_at=None, finished_at=time.time(),
        server=str(server.get("id") or "unknown")))


def _run(cmd):
    query_fn = _QUERY_FN.get()
    server = active_server()
    if query_fn:
        return query_fn(cmd).strip()
    return (query_server(server, cmd) if server else query(cmd)).strip()


def _audit(event, uid, command, result=""):
    """追加带 SHA-256 前向哈希的本地审计记录。"""
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOCK:
        prev_hash = "0" * 64
        if AUDIT_PATH.exists():
            try:
                last = AUDIT_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-1]
                prev_hash = json.loads(last).get("hash", prev_hash)
            except Exception:
                pass
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(),
            "event": event,
            "uid": str(uid),
            "command": command,
            "result": result[:500],
            "prevHash": prev_hash,
        }
        canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        entry["hash"] = hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()
        with AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def _expire_pending(now=None):
    now = now or time.time()
    for code, item in list(_PENDING_RISK.items()):
        if now > item["expires"]:
            _PENDING_RISK.pop(code, None)


def pending_confirmation(uid, code=''):
    """Resolve one caller-owned pending operation without exposing other users."""
    wanted = str(code or '').strip().upper()
    with _RISK_LOCK:
        _expire_pending()
        matches = [
            (item_code, item) for item_code, item in _PENDING_RISK.items()
            if item['uid'] == str(uid) and (not wanted or item_code == wanted)
        ]
        if len(matches) != 1:
            return None
        item_code, item = matches[0]
        return {'code': item_code, 'server': item.get('server'),
                'server_id': item.get('server_id'), 'label': item.get('label', '')}


def _request_risk(uid, label, command, executor):
    query_fn = _QUERY_FN.get()
    server = active_server()

    def bound_executor():
        token = _QUERY_FN.set(query_fn)
        try:
            with server_context(server):
                return executor()
        finally:
            _QUERY_FN.reset(token)

    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4))
    now = time.time()
    with _RISK_LOCK:
        _expire_pending(now)
        for old_code, item in list(_PENDING_RISK.items()):
            if item["uid"] == str(uid):
                _PENDING_RISK.pop(old_code, None)
        _PENDING_RISK[code] = {
            "uid": str(uid), "label": label, "command": command,
            "executor": bound_executor, "expires": now + RISK_TTL_SECONDS,
            "server_id": getattr(query_fn, 'server_id', None),
            "server": server,
        }
    _audit("risk_request", uid, command, "pending")
    return (f"[高危确认] 即将{label}。\n"
            f"请在 {RISK_TTL_SECONDS} 秒内发送：!确认 {code}\n"
            "如需取消：!取消确认")


def _confirm_risk(uid, code):
    code = code.strip().upper()
    if not code:
        return "[高危确认] 用法：!确认 四位确认码"
    with _RISK_LOCK:
        _expire_pending()
        item = _PENDING_RISK.get(code)
        if not item:
            return "[高危确认] 确认码不存在或已过期。"
        if item["uid"] != str(uid):
            _audit("risk_confirm_rejected", uid, item["command"], "wrong_user")
            return "[高危确认] 该确认码只能由原发起人使用。"
        current_server = getattr(_QUERY_FN.get(), 'server_id', None)
        current_context = active_server()
        wrong_query_target = item.get("server_id") and current_server != item["server_id"]
        wrong_context_target = (item.get("server") and
                                (not current_context or
                                 current_context.get('id') != item['server'].get('id')))
        if wrong_query_target or wrong_context_target:
            _audit("risk_confirm_rejected", uid, item["command"], "wrong_server")
            return "[高危确认] 确认目标与原操作服务器不一致，已拒绝执行。"
        _PENDING_RISK.pop(code, None)
    try:
        result = item["executor"]()
        result_text = str(result or "")
        failed = bool(re.search(
            r"(incorrect argument|unknown or incomplete command|unknown command|执行失败|error:)",
            result_text, re.I))
        audit_result = ("rejected: " if failed else "executed: ") + result_text
        _audit("risk_confirm", uid, item["command"], audit_result)
        return result
    except Exception as e:
        _audit("risk_confirm", uid, item["command"], "failed: " + str(e))
        return "[控制台] 执行失败：" + str(e)


def _cancel_risk(uid):
    cancelled = []
    with _RISK_LOCK:
        _expire_pending()
        for code, item in list(_PENDING_RISK.items()):
            if item["uid"] == str(uid):
                cancelled.append(item["command"])
                _PENDING_RISK.pop(code, None)
    for command in cancelled:
        _audit("risk_cancel", uid, command, "cancelled")
    return "[高危确认] 已取消待执行操作。" if cancelled else "[高危确认] 当前没有待确认操作。"


def _translate_difficulty(d):
    return {"peaceful": "和平", "easy": "简单", "normal": "普通", "hard": "困难"}.get(d.lower(), d)


def _on_off(v):
    return "开" if str(v).strip().lower() in ("true", "on") else "关"


def _read_prop(key, fallback=""):
    try:
        server = active_server()
        root = Path(server['server_dir']) if server and server.get('server_dir') else SERVER_DIR
        for line in (root / "server.properties").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return fallback


def _gamerule(rule):
    try:
        out = _run("gamerule " + rule)
        m = re.search(r"currently set to:\s*(\w+)", out)
        return _on_off(m.group(1)) if m else "未知"
    except Exception:
        return "未知（RCON 不可用）"


def cmd_help(privileged):
    from server_registry import server_hint
    from media_pipeline import stage_config
    media_status = '；'.join(label + '：' + ('已启用' if stage_config(kind)[0] == 'ok' else stage_config(kind)[0])
                            for kind, label in [('ASR', '转写'), ('Omni', '声音理解'), ('视频', '视频理解')])
    common = (
        '【早苗帮助】!help / !帮助\n'
        f'可用服务器：{server_hint()}\n'
        '!服 查看当前目标；!服 名称 选择；!服 自动 清除选择\n'
        '服务器查询先选服，或在命令前加对应服务器标签。\n'
        '\n◆ 模组与资料\n'
        '!mods / !模组 / !mod清单 已安装模组完整分类清单\n'
        '包含模组 ID、依赖及反向依赖；长清单折叠，无法折叠时用 !mods 2 等页码翻页。\n'
        '!mod 关键词 搜 Modrinth；!cf 关键词 搜 CurseForge\n'
        '!wiki 关键词 / !百科 关键词 查百科\n'
        '!配方 物品名或ID 查配方索引\n'
        '!周报 / !运行报告 查近7天统计；!周报 详情 / !体检 查统计口径与数据覆盖\n'
        '周报每周一20:00（北京时间）自动发玩家群；包含性能、在线、启停、备份和磁盘。\n'
        '\n◆ AI 与共享知识\n'
        '@早苗 问题 进行问答；已保存的相关通用知识会自动用于回答。\n'
        '可回复合并转发后 @早苗 分析一下；或 @早苗 总结最近群聊。\n'
        '认真问答支持长文与分点；多轮20轮/2小时，聊天总结最多1000条/9.6万字符。\n'
        '聊天总结先给240字内简短总结，完整时间线另附一条折叠消息。\n'
        '只要短版：@早苗 简短总结；刚总结完可说“@早苗 太长了，简短点”。\n'
        '图片/音视频另走对应媒体能力；缺失、截断和实际读取范围会注明。\n'
        '!知识库 查看条目数；!知识库查询 关键词 查询\n'
        '知识保存与删除仅管理员可用。\n'
        '\n◆ 语音与视频\n'
        '引用媒体（也支持合并转发）后发送：\n'
        '!转写 / !语音转写 只转写文字\n'
        '!听语音 理解说话、音乐或环境声音\n'
        '引用视频后 @早苗 提问，分析画面及声音。\n'
        f'当前服务：{media_status}\n'
        '未启用或未配置的媒体服务不会调用。\n'
        '\n◆ 服务器与趣味\n'
        '!list 在线列表；!day 游戏天数与时间；!rules 规则\n'
        '!version 版本；!uptime 运行状态；!ip 连接地址\n'
        '服务器启动完成会通知玩家群，包含总耗时、Minecraft内部阶段耗时和连接地址。\n'
        '!roll 面数 掷骰子；!运势 今日运势；!id 自己的QQ号'
    )
    if not privileged:
        return common
    return common + (
        '\n\n◆ 管理员命令\n'
        '!问 问题 AI 问答；!ai 查看模型状态\n'
        '!知识库记住 主题 => 结论 保存通用短结论\n'
        '!知识库删除 关键词 删除匹配知识\n'
        '记住/查询时可引用媒体，关联媒体指纹；不保存原始音视频。\n'
        'AI 工具任务只返回计划时会继续核对一次；认真问答和总结最多约四分钟；媒体任务约两分钟。\n'
        '超时后已开始的操作需核实结果，不会自动重放。\n'
        '!错误摘要 6h / !时间线 6h / !复盘 24h 查询运维记录\n'
        '时间窗口：1h / 6h / 24h / 7d\n'
        '!卡顿取证 后台取证，完成发玩家群；!卡顿取证 最新 查看结果；!验备份 校验已有备份\n'
        '!tps 性能；!backup 创建备份；!backup 状态 查看进度\n'
        '!save 存盘；!seed 种子；!weather 晴/雨/雷 改天气\n'
        '!say 内容 发游戏公告；!restart 确认后重启\n'
        '!stop 查看持续停服限制；!cmd 命令 申请执行控制台命令\n'
        '!确认 验证码 确认待执行操作；!取消确认 取消\n'
        '重启/停服须在本条命令加服务器标签；其他高危操作按确认提示执行。'
    )


def cmd_list():
    try:
        return "[在线]\n" + _run("list")
    except Exception as e:
        return f"[在线] 查询失败：{e}"


def cmd_day():
    try:
        out = _run("time query gametime")
        ticks = int(re.search(r"\d+", out).group())
        total_days = ticks // 24000
        day_ticks = ticks % 24000
        hour = (day_ticks // 1000 + 6) % 24
        minute = (day_ticks % 1000) * 60 // 1000
        return f"游戏天数：第 {total_days} 天\n游戏时间：{hour:02d}:{minute:02d}"
    except Exception as e:
        return f"[日期] 查询失败：{e}"


def cmd_tps():
    try:
        output = _run("neoforge tps")
        parts = []
        for line in (item.strip() for item in output.splitlines() if item.strip()):
            if ':' not in line or 'TPS' not in line:
                continue
            name, rest = line.split(':', 1)
            match = re.search(r'([\d.]+) TPS \(([\d.]+) ms/tick\)', rest)
            if match:
                parts.append(f"{name.strip()}: {float(match.group(1)):.1f} TPS "
                             f"({float(match.group(2)):.1f}ms)")
        return "[性能]\n" + ('\n'.join(parts) if parts else output)
    except Exception as e:
        return f"[性能] 查询失败：{e}"


def cmd_seed():
    try:
        return "[种子] " + _run("seed")[:200]
    except Exception as e:
        return f"[种子] 查询失败：{e}"


def cmd_save():
    try:
        _run("save-all flush")
        return "[存盘] 世界已保存到磁盘。"
    except Exception as e:
        return f"[存盘] 保存失败：{e}"


def cmd_weather(arg):
    target = {"clear": "clear", "晴": "clear", "晴天": "clear",
              "rain": "rain", "雨": "rain", "下雨": "rain", "雨天": "rain",
              "thunder": "thunder", "雷": "thunder", "雷雨": "thunder", "打雷": "thunder"}.get(arg.strip().lower(), "")
    if not target:
        return "[天气] 用法：!weather 晴/雨/雷"
    try:
        _run("weather " + target)
        label = {"clear": "晴天", "rain": "雨天", "thunder": "雷雨"}[target]
        return f"[天气] 已切换为{label}。"
    except Exception as e:
        return f"[天气] 切换失败：{e}"


def cmd_say(text):
    text = text.strip()
    if not text:
        return "[公告] 用法：!say 公告内容"
    try:
        _run("say " + text.replace("\n", " ").replace("\r", " "))
        return f"[公告] 已发送到游戏公屏：{text[:200]}"
    except Exception as e:
        return f"[公告] 发送失败：{e}"


def cmd_stop():
    return ("[控制台] 当前 MCSManager 已启用 autoRestart，RCON stop 会自动重新拉起，"
            "无法实现持续停服。请在 MCSManager 面板先关闭自动重启后再停服。")


def _execute_restart():
    _run("stop")
    return "[控制台] 已安全发送 stop；MCSManager 将按 autoRestart 设置重新拉起服务器。"


def cmd_restart(uid):
    return _request_risk(uid, "重启服务器", "restart", _execute_restart)


def _execute_cmd(command):
    result = _run(command)
    if not result:
        result = "(命令已执行，无返回内容)"
    return "[控制台返回] " + command[:120] + "\n" + result[:3500]


def cmd_cmd(command, uid):
    command = command.strip()
    if command.startswith("<") and command.endswith(">"):
        command = command[1:-1].strip()
    try:
        # Preserve WorldEdit's meaningful double slash while removing the one
        # chat-style slash used by ordinary Minecraft commands.
        command = normalize_console_command(command)
    except ValueError:
        return "[控制台] 用法：!cmd <RCON命令>"
    return _request_risk(uid, "执行 RCON 命令：" + command[:120], "cmd " + command,
                         lambda: _execute_cmd(command))


def cmd_rules():
    try:
        diff = ""
        try:
            m = re.search(r"The difficulty is (\w+)", _run("difficulty"))
            if m:
                diff = _translate_difficulty(m.group(1))
        except Exception:
            pass
        if not diff:
            diff = _translate_difficulty(_read_prop("difficulty", ""))
        lines = [f"游戏难度：{diff or '未知'}",
                 f"PVP玩家互伤：{_on_off(_read_prop('pvp', 'true'))}",
                 f"白名单：{_on_off(_read_prop('white-list', 'false'))}",
                 f"死亡不掉落：{_gamerule('keepInventory')}",
                 f"生物/爆炸破坏方块：{_gamerule('mobGriefing')}",
                 f"火焰蔓延：{_gamerule('doFireTick')}"]
        return "【服务器规则】\n" + "\n".join(lines)
    except Exception as e:
        return f"[规则] 查询失败：{e}"


def cmd_version():
    """Return version metadata for the active server; never use another server's fallback."""
    server = active_server()
    if server:
        label = str(server.get("version_label") or "").strip()
        if label:
            return "[版本] " + label[:200]
    try:
        # 先试 RCON（部分服务端/代理支持 version）
        out = _run("version")
        if out and "Unknown" not in out and "incomplete" not in out:
            return "[版本] " + out.strip()[:200]
    except Exception:
        pass
    name = str((server or {}).get("name") or "当前服务器").strip()
    return f"[版本] {name}（详细版本未在服务器注册表登记）"


def cmd_uptime():
    """RCON 可达即视为在线（uptime 精确值需读 Mac 日志，跨机器不可行）。"""
    try:
        _run("list")
        return "[运行时长] 服务器当前在线（RCON 可达）。精确开服时长需在服务端查 logs/latest.log。"
    except Exception as e:
        return f"[运行时长] 服务器可能不在线（RCON 不可达）：{e}"


def cmd_ip():
    server = active_server()
    name = server['name'] if server else 'Minecraft Server'
    address = server.get('address', ADDR) if server else ADDR
    return f"[地址] {name}\n连接地址：{address}"


def cmd_roll(maxv=100):
    try:
        maxv = max(1, min(int(maxv), 10000))
    except Exception:
        maxv = 100
    return f"[骰子] 掷出了 {random.randint(1, maxv)} 点（1~{maxv}）"


def cmd_fortune(nickname, uid):
    seed = int(uid) * 1000003 + datetime.date.today().toordinal()
    rnd = random.Random(seed)
    levels = ["大吉", "中吉", "小吉", "吉", "末吉", "凶", "大凶"]
    weights = [14, 20, 18, 18, 14, 10, 6]
    level = rnd.choices(levels, weights=weights)[0]
    good = ["下矿挖矿，钻石在向你招手", "远行探险，说不定能捡到好东西", "盖房子搞装修，灵感爆棚",
            "钓鱼，感觉要出附魔书", "找村民交易，全是好价", "附魔，今天手感火热",
            "打怪练级，掉落物丰厚", "整理箱子，强迫症大满足", "约群友联机，人多力量大"]
    bad = ["裸手撸苦力怕", "带着全部家当出远门", "在岩浆边上秀走位", "不带火把就下矿",
           "挖自己脚下的方块", "直视末影人", "半血不吃东西硬刚", "忘记备份就开搞大工程"]
    tip = rnd.choice(good) if level in ("大吉", "中吉", "小吉", "吉") else rnd.choice(bad)
    return f"【今日运势】{nickname}：{level}\n宜：{tip}"


def cmd_id(nickname, uid):
    return f"[QQ控制台] {nickname} 的 QQ 号：{uid}"


def cmd_ai_status():
    try:
        import sanae_ai as _s
        return _s.ai_status()
    except Exception:
        return "[AI] 当前模型：deepseek-v4-flash（DeepSeek API 直连，不走 agent）"


def _cmd_backup():
    """!backup：启动独立可靠流水线，立即返回，不阻塞 bridge 请求线程。"""
    pipeline = Path(__file__).with_name("backup_pipeline.py")
    log_dir = Path(__file__).with_name("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log = None
    try:
        log = (log_dir / "backup-pipeline-launch.log").open("a", encoding="utf-8")
        subprocess.Popen(
            [sys.executable, str(pipeline)],
            cwd=str(pipeline.parent), stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
        )
    except Exception as e:
        return f"[备份] 无法启动备份流水线：{e}"
    finally:
        # Popen 已复制文件描述符；父 bridge 必须关闭自己的句柄，避免每次 !backup 泄漏一个 FD。
        if log is not None:
            log.close()
    return ("[备份] 已开始创建手动备份。\n"
            "流程：保存世界 → 等待新 ZIP 完成 → 结构验证 → 异地同步校验。\n"
            "存档约 3.6GB，完成或失败后会自动回群通知。")


def _cmd_backup_status():
    status_path = Path(__file__).with_name("logs") / "backup-pipeline-status.json"
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "[备份状态] 暂无手动备份流水线记录。"
    except Exception as e:
        return f"[备份状态] 状态文件读取失败：{e}"
    stage = data.get("stage", data.get("state", "unknown"))
    message = data.get("message", "")
    lines = [f"[备份状态] {stage}：{message}"]
    if data.get("file"):
        lines.append("文件：" + str(data["file"]))
    verify = data.get("verify") or {}
    if verify.get("size"):
        lines.append(f"本地：{verify['size']} bytes，结构验证={'通过' if verify.get('ok') else '失败'}")
    sync = data.get("sync") or {}
    if sync:
        lines.append(f"异地：{sync.get('message', sync.get('state', '未知'))}")
        if sync.get("localSize"):
            lines.append(f"字节校验：Mac {sync.get('localSize')} / Monarch {sync.get('remoteSize', 0)}")
    return "\n".join(lines)


def _dispatch(command, nickname, uid, privileged):
    """命令分发：返回回复文本。command 已去前缀。"""
    cmd = command.strip()
    if not cmd:
        return None
    word = command_word(cmd)

    if word in ("help",):
        return cmd_help(privileged)
    if word in ("list",):
        return cmd_list()
    if word in ("day", "date", "time"):
        return cmd_day()
    if word in ("rules",):
        return cmd_rules()
    if word in ("version",):
        return cmd_version()
    if word in ("uptime",):
        return cmd_uptime()
    if word in ("ip", "address"):
        return cmd_ip()
    if word in ("roll",):
        arg = cmd[4:].strip() if cmd.lower().startswith("roll") else ""
        return cmd_roll(arg)
    if word in ("运势",):
        return cmd_fortune(nickname, uid)
    if word in ("id", "whoami"):
        return cmd_id(nickname, uid)

    # 以下需要管理员
    if not privileged:
        return None
    if word in ("确认", "confirm"):
        return _confirm_risk(uid, cmd[len(cmd.split()[0]):].strip())
    if word in ("取消确认", "cancel"):
        return _cancel_risk(uid)
    if word in ("tps",):
        return cmd_tps()
    if word in ("backup",):
        rest = cmd[len(cmd.split()[0]):].strip().lower()
        if rest in ("status", "状态", "进度"):
            return _cmd_backup_status()
        return _cmd_backup()
    if word in ("save",):
        return cmd_save()
    if word in ("seed",):
        return cmd_seed()
    if word in ("weather",):
        return cmd_weather(cmd[len(word):])
    if word in ("say",):
        return cmd_say(cmd[len(word):])
    if word == "stop":
        return cmd_stop()
    if word == "restart":
        return cmd_restart(uid)
    if word in ("cmd", "控制台"):
        rest = cmd[len(word):].strip()
        return cmd_cmd(rest, uid)
    if word in ("ai",):
        return cmd_ai_status()
    return None


def dispatch(command, nickname, uid, privileged, query_fn=None, server=None):
    """Dispatch against an optional per-request RCON backend."""
    token = _QUERY_FN.set(query_fn)
    try:
        if server is not None:
            with server_context(server):
                return _dispatch(command, nickname, uid, privileged)
        return _dispatch(command, nickname, uid, privileged)
    finally:
        _QUERY_FN.reset(token)
