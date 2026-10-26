"""Player bug reports collected from the QQ groups, kept in one JSON ledger.

Two ways in:
  - the AI judge (sanae_ai.judge_bug_reports) picks reports out of ordinary chat;
  - anyone can file one directly with `!bug <描述>`.
Admins list and close items with `!bug`, and a daily digest goes to the admin group.
Each new item records the installed version of the mod it names and, for
crashes, the crash reports seen in the hours before it.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
CATEGORY_LABELS = {'crash': '崩溃', 'visual': '贴图/模型', 'recipe': '配方', 'function': '功能',
                   'item_loss': '物品丢失/刷物品', 'performance': '卡顿', 'quest': '任务',
                   'translation': '翻译', 'other': '其他'}
STATUS_LABELS = {'open': '未处理', 'resolved': '已解决', 'ignored': '已忽略'}
MAX_EVIDENCE = 8
MANUAL_PER_HOUR = 5
COMMAND = re.compile(r'^[!！]\s*(?:bug|问题反馈|报bug)(?:\s+(.*))?$', re.I | re.S)
USAGE = ('用法：!bug 问题描述（说清楚在哪、做了什么、出了什么问题）。'
         '管理员：!bug 查看未处理；!bug 详情 编号；!bug 解决 编号；!bug 忽略 编号；!bug 重开 编号；!bug 全部')


def _clock(value):
    return datetime.fromtimestamp(float(value), TZ).strftime('%m-%d %H:%M')


def _read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


class BugLedger:
    def __init__(self, path, inventory_path=None, events_path=None, clock=time.time):
        self.path = Path(path)
        self.inventory_path = Path(inventory_path) if inventory_path else None
        self.events_path = Path(events_path) if events_path else None
        self._clock = clock
        self._lock = threading.RLock()
        self._inventory = (None, {})
        self._manual = defaultdict(deque)

    # ----- storage -----
    def _load(self):
        data = _read_json(self.path, None)
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            data = {'schema': 1, 'next_id': 1, 'items': [], 'digest': {}}
        data.setdefault('digest', {})
        return data

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=self.path.name + '.', suffix='.tmp', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, indent=1)
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    # ----- mods -----
    def _mods(self):
        """{id_casefold: (name, version)} from the mod inventory, cached by mtime."""
        if not self.inventory_path:
            return {}
        try:
            mtime = self.inventory_path.stat().st_mtime
        except OSError:
            return {}
        if self._inventory[0] != mtime:
            rows = {}
            for mod in (_read_json(self.inventory_path, {}) or {}).get('mods') or []:
                if isinstance(mod, dict) and mod.get('id') and mod.get('metadata') != 'filename':
                    rows[str(mod['id']).casefold()] = (str(mod.get('name') or mod['id']),
                                                       str(mod.get('version') or ''))
            self._inventory = (mtime, rows)
        return self._inventory[1]

    def installed_ids(self):
        return sorted(self._mods())

    def guess_mod(self, text):
        """A mod id or name written in the text (manual reports); '' if none."""
        value = str(text or '').casefold()
        for mod_id, (name, _version) in sorted(self._mods().items(), key=lambda r: -len(r[0])):
            if len(mod_id) >= 4 and re.search(r'(?<![a-z0-9_])' + re.escape(mod_id) + r'(?![a-z0-9_])', value):
                return mod_id
            if len(name) >= 5 and name.casefold() in value:
                return mod_id
        return ''

    def _recent_crashes(self, now, hours=6):
        if not self.events_path:
            return []
        names = []
        try:
            with open(self.events_path, 'rb') as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 2 * 1024 * 1024))
                for raw in stream:
                    try:
                        row = json.loads(raw.decode('utf-8'))
                    except (ValueError, UnicodeError):
                        continue
                    stamp = row.get('timestamp') if isinstance(row, dict) else None
                    if (row.get('type') == 'crash' and isinstance(stamp, (int, float))
                            and now - hours * 3600 <= stamp <= now + 60):
                        name = str((row.get('details') or {}).get('report') or '')
                        if name and name not in names:
                            names.append(name)
        except OSError:
            return []
        return names[-3:]

    # ----- items -----
    def items(self):
        with self._lock:
            return self._load()['items']

    def open_items(self):
        return [item for item in self.items() if item['status'] == 'open']

    def get(self, item_id):
        return next((item for item in self.items() if item['id'] == int(item_id)), None)

    def record(self, report, evidence, group, source='ai'):
        """Add a new item, or merge into report['duplicate_of'] when that one is open.

        Returns (item, merged). `evidence` is [{message_id, uid, name, time, text}].
        """
        now = self._clock()
        evidence = [dict(row, text=str(row.get('text') or '')[:300]) for row in evidence if row]
        with self._lock:
            data = self._load()
            target = next((item for item in data['items']
                           if item['id'] == int(report.get('duplicate_of') or 0) and item['status'] == 'open'),
                          None)
            if target is not None:
                known = {row.get('message_id') for row in target['evidence']}
                fresh = [row for row in evidence if row.get('message_id') not in known]
                target['evidence'] = (target['evidence'] + fresh)[-MAX_EVIDENCE:]
                for row in fresh:
                    if row.get('uid') and row['uid'] not in {r['uid'] for r in target['reporters']}:
                        target['reporters'].append({'uid': row['uid'], 'name': row.get('name') or ''})
                target['reports'] += 1 if fresh else 0
                if group not in target['groups']:
                    target['groups'].append(group)
                target['updated_at'] = now
                target['merged_since_digest'] = target.get('merged_since_digest', 0) + (1 if fresh else 0)
                self._save(data)
                return target, True
            mod = str(report.get('mod') or '')
            name, version = self._mods().get(mod.casefold(), ('', ''))
            reporters = []
            for row in evidence:
                if row.get('uid') and row['uid'] not in {r['uid'] for r in reporters}:
                    reporters.append({'uid': row['uid'], 'name': row.get('name') or ''})
            item = {'id': data['next_id'], 'status': 'open', 'source': source,
                    'created_at': now, 'updated_at': now,
                    'title': str(report.get('title') or '')[:40],
                    'summary': str(report.get('summary') or '')[:300],
                    'category': report.get('category') if report.get('category') in CATEGORY_LABELS else 'other',
                    'mod': mod, 'mod_name': name, 'mod_version': version,
                    'crashes': self._recent_crashes(now) if report.get('category') == 'crash' else [],
                    'groups': [group], 'reporters': reporters, 'reports': 1,
                    'evidence': evidence[-MAX_EVIDENCE:], 'notified': False, 'merged_since_digest': 0}
            data['next_id'] += 1
            data['items'].append(item)
            self._save(data)
            return item, False

    def manual(self, text, uid, name, group, message_id=None):
        """`!bug <描述>`: a direct report, rate limited per person. Returns (item or None, reason)."""
        text = re.sub(r'\s+', ' ', str(text or '')).strip()
        if len(text) < 4:
            return None, 'short'
        now = self._clock()
        with self._lock:
            stamps = self._manual[str(uid)]
            while stamps and now - stamps[0] >= 3600:
                stamps.popleft()
            if len(stamps) >= MANUAL_PER_HOUR:
                return None, 'limit'
            stamps.append(now)
        mod = self.guess_mod(text)
        item, _ = self.record({'title': text[:30], 'summary': text[:300], 'mod': mod,
                               'category': 'crash' if re.search(r'崩|crash', text, re.I) else 'other'},
                              [{'message_id': str(message_id or ''), 'uid': str(uid), 'name': name,
                                'time': _clock(now), 'text': text}], group, source='manual')
        return item, 'ok'

    def set_status(self, item_id, status, by, note=''):
        with self._lock:
            data = self._load()
            item = next((row for row in data['items'] if row['id'] == int(item_id)), None)
            if item is None:
                return None
            item['status'] = status
            item['updated_at'] = self._clock()
            if status == 'open':
                item.pop('closed_by', None)
                item.pop('closed_at', None)
                item.pop('close_note', None)
            else:
                item.update(closed_by=str(by), closed_at=self._clock(), close_note=str(note or '')[:100])
            self._save(data)
            return item

    # ----- rendering -----
    @staticmethod
    def _line(item):
        mod = item.get('mod_name') or item.get('mod') or ''
        tag = f'[{mod} {item.get("mod_version") or ""}]'.replace(' ]', ']') if mod else ''
        people = len(item.get('reporters') or []) or 1
        who = (item.get('reporters') or [{}])[0].get('name') or '群友'
        more = f'，{people} 人' if people > 1 else ''
        return f'#{item["id"]} {tag}{item["title"]}（{who}{more}，{_clock(item["created_at"])}）'

    def render_list(self, include_closed=False, limit=15):
        rows = self.items() if include_closed else self.open_items()
        if not rows:
            return '目前没有未处理的问题反馈。' if not include_closed else '还没有问题反馈记录。'
        rows = sorted(rows, key=lambda r: r['updated_at'], reverse=True)[:limit]
        head = (f'问题反馈（最近 {len(rows)} 条）：' if include_closed else
                f'未处理的问题反馈（{len(self.open_items())} 条，显示最近 {len(rows)} 条）：')
        lines = [head]
        for item in rows:
            status = '' if item['status'] == 'open' else f'【{STATUS_LABELS[item["status"]]}】'
            lines.append(status + self._line(item))
        lines.append('!bug 详情 编号 查看原话；!bug 解决 编号 / !bug 忽略 编号 处理')
        return '\n'.join(lines)

    def render_item(self, item_id):
        item = self.get(item_id)
        if item is None:
            return f'没有编号 #{item_id} 的问题反馈。'
        lines = [f'#{item["id"]} {item["title"]}【{STATUS_LABELS[item["status"]]}】',
                 f'分类：{CATEGORY_LABELS.get(item["category"], "其他")}｜来源：'
                 + ('AI 从聊天中识别' if item['source'] == 'ai' else '群友 !bug 提交')
                 + f'｜反馈 {item["reports"]} 次，{len(item["reporters"])} 人',
                 f'记录：{_clock(item["created_at"])}；最近更新 {_clock(item["updated_at"])}']
        if item.get('mod'):
            lines.append(f'模组：{item.get("mod_name") or item["mod"]}（{item["mod"]}）'
                         + (f' 版本 {item["mod_version"]}' if item.get('mod_version') else '（版本未知）'))
        if item.get('summary'):
            lines.append('摘要：' + item['summary'])
        if item.get('crashes'):
            lines.append('之前 6 小时的崩溃报告：' + '、'.join(item['crashes']))
        lines.append('原话：')
        lines.extend(f'- {row.get("time") or ""} {row.get("name") or "群友"}：{row.get("text") or ""}'
                     for row in item['evidence'])
        if item['status'] != 'open':
            lines.append(f'处理：{_clock(item["closed_at"])} {item.get("close_note") or ""}'.rstrip())
        return '\n'.join(lines)

    # ----- daily digest -----
    def digest_due(self, now=None, hour=21):
        """(text, ids) when today's digest is due and there is something new; else (None, [])."""
        now = self._clock() if now is None else now
        local = datetime.fromtimestamp(now, TZ)
        day = local.strftime('%Y-%m-%d')
        with self._lock:
            data = self._load()
            digest = data['digest']
            if local.hour < hour or digest.get('day') == day or float(digest.get('retry_after') or 0) > now:
                return None, []
            new = [item for item in data['items'] if item['status'] == 'open' and not item.get('notified')]
            merged = [item for item in data['items'] if item['status'] == 'open' and item.get('notified')
                      and item.get('merged_since_digest')]
            if not new and not merged:
                digest['day'] = day       # nothing to say today
                self._save(data)
                return None, []
            total = sum(1 for item in data['items'] if item['status'] == 'open')
            lines = [f'【玩家问题汇总】{local.strftime("%m-%d")}：新增 {len(new)} 条'
                     + (f'，补充 {len(merged)} 条' if merged else '') + f'（未处理共 {total} 条）']
            lines.extend(self._line(item) for item in new[:20])
            if len(new) > 20:
                lines.append(f'……另有 {len(new) - 20} 条，!bug 查看')
            for item in merged[:10]:
                lines.append(f'补充：#{item["id"]} {item["title"]} 又有 {item["merged_since_digest"]} 次反馈')
            lines.append('在玩家群发 !bug 详情 编号 看原话；!bug 解决 编号 / !bug 忽略 编号 处理')
            return '\n'.join(lines), [item['id'] for item in new + merged]

    def mark_digest(self, ids, ok, now=None):
        now = self._clock() if now is None else now
        with self._lock:
            data = self._load()
            if ok:
                for item in data['items']:
                    if item['id'] in ids:
                        item['notified'] = True
                        item['merged_since_digest'] = 0
                data['digest'] = {'day': datetime.fromtimestamp(now, TZ).strftime('%Y-%m-%d'), 'sent_at': now}
            else:
                data['digest']['retry_after'] = now + 1800
            self._save(data)


def command(ledger, raw_text, uid, name, group, privileged, message_id=None):
    """Reply text for a `!bug` message, or None when the text is not a `!bug` command."""
    match = COMMAND.match(str(raw_text or '').strip())
    if not match:
        return None
    body = (match.group(1) or '').strip()
    if not body:
        return ledger.render_list() if privileged else USAGE
    if privileged:
        detail = re.fullmatch(r'(?:详情|查看)\s*#?(\d+)', body)
        if detail:
            return ledger.render_item(int(detail.group(1)))
        change = re.fullmatch(r'(解决|已解决|修复|已修复|忽略|关闭|重开)\s*#?(\d+)\s*(.*)', body, re.S)
        if change:
            word, item_id, note = change.groups()
            status = {'解决': 'resolved', '已解决': 'resolved', '修复': 'resolved', '已修复': 'resolved',
                      '忽略': 'ignored', '关闭': 'ignored', '重开': 'open'}[word]
            item = ledger.set_status(int(item_id), status, uid, note.strip())
            if item is None:
                return f'没有编号 #{item_id} 的问题反馈。'
            return f'#{item["id"]} {item["title"]} 已标为{STATUS_LABELS[status]}。'
        if body in ('全部', '所有', '历史'):
            return ledger.render_list(include_closed=True)
    elif re.fullmatch(r'(?:详情|查看|解决|已解决|修复|已修复|忽略|关闭|重开)\s*#?\d+.*|全部|所有|历史', body, re.S):
        return '查看和处理反馈仅管理员可用；要报告问题请直接写描述：!bug 问题描述'
    item, reason = ledger.manual(body, uid, name, group, message_id)
    if reason == 'short':
        return USAGE
    if reason == 'limit':
        return f'一小时内最多提交 {MANUAL_PER_HOUR} 条，稍后再发吧。'
    mod = f'，关联模组 {item["mod_name"] or item["mod"]} {item["mod_version"]}'.rstrip() if item['mod'] else ''
    return f'记下了，编号 #{item["id"]}{mod}。开发者会在汇总里看到。'
