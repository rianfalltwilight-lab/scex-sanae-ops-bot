"""Live checks on the Singularity Iteration exchange (mio_icif 0.1.10.29+), read-only over RCON.

The commands are the mod's own audit subcommands; only the reading ones are used here:
  mio_icif market audit [pairs]        exchange counts, coins created/destroyed in 7 days, top player pairs
  mio_icif market ledger [days]        coins created and destroyed per market day, sell pressure, auction orders
  mio_icif market simulate <item> <n> [player]   what selling n items now would pay (nothing is sold)
  mio_icif market arbitrage [count]    craft-and-sell loops and recipe cycles that multiply items   (admins)
  mio_icif market calibrate            coins per bucket of UU-matter for each commodity            (admins)
  mio_icif bounty list|tables|settings|errors    open bounties and weekly budget / tables / settings (admins)
  mio_icif reactor audit [id]          reactor incidents; coordinates for admins only
`bounty post|cancel` and `reactor audit clear` change state and are not used; they go through the
confirmation path like any other console command.
"""
from __future__ import annotations

import re

TOPICS = ('overview', 'ledger', 'simulate', 'bounties', 'reactor',
          'arbitrage', 'calibrate', 'bounty_tables', 'settings')
ADMIN_TOPICS = frozenset({'arbitrage', 'calibrate', 'bounty_tables', 'settings'})
LABELS = {'overview': '交易所概况', 'ledger': '货币发行与销毁', 'simulate': '卖出估算', 'bounties': '悬赏',
          'reactor': '反应堆事故记录', 'arbitrage': '套利与增殖循环', 'calibrate': 'UU 物质与货币换算',
          'bounty_tables': '悬赏表', 'settings': '经济设置'}
# How to read each English report (from the mod's zh_cn strings). 10-27 replay: "Left this week:
# advanced 12 of 16" was read as "12 of 16 done" and "UU-matter 4000 of 4000" as "used up".
GLOSSARY = {
    'overview': 'player listings 玩家上市品，resting orders 挂单，commissions 收购委托，open futures 未平仓期货（collateral 保证金）；'
                'coins created/destroyed 是新发行/销毁的币，net 净额；最后几行是玩家之间成交最多的组合。',
    'ledger': '每行一个交易日：created 发行、destroyed 销毁、net 净额；sell pressure 卖压（近期卖出堆积的数量，越大卖价越低）；'
              'auction orders 集中清算订单。',
    'simulate': 'price 现价；bid without pressure 没有卖压时的收购价；depth 每天的卖出深度（个人/全服）；sold today 今天已卖；'
                'Selling N now pays X coins 是现在卖 N 个能得 X 币（average 平均单价，last bid 最后一个的价）；'
                'alone at the auction 单独参加集中清算能得多少；one sale pays at most 单次最多能得多少。只是估算，没有真的卖。',
    'bounties': 'Left this week 是本周还剩多少奖励额度可发（剩余 of 总数，不是完成数）：advanced 高级奖励物品、special 特别奖励、'
                'UU-matter 是 UU 物质 mB；vault 宝库里的物品种类。每条悬赏：#编号 档次 [悬赏表]：要交的物品 已交/需要 | 奖励'
                '（minted 新发行的币，issue/treasury/vault 是系统发放、国库、宝库出的奖励）| 剩余天数 | 分配方式（first 先交齐的人拿，'
                'contribution 按贡献分）| 贡献人数。',
    'reactor': '每行一起事故：#编号 时间 维度 [坐标] 主因，按时间从新到旧。',
    'arbitrage': 'bid 交易所收购价，make 用原料做一个的成本，profit 每个的差价，EU per item/coin 能耗，via 配方，from 原料；'
                 'Recipe cycles that multiply items 是会让物品越做越多的配方循环，可能是刷物品的漏洞。',
    'calibrate': '第一行是 1 桶 UU 物质折合多少币；每行：base 基础价，UU N buckets = M coins 是做一个要 N 桶 UU、折合 M 币，'
                 'ratio = 基础价 ÷ UU 折合价，大于 1 说明用 UU 复制再卖是赚的。',
    'bounty_tables': '每行一个悬赏表：档次（normal/advanced/special），weight 抽中权重，requirements 要求数，pool 奖励池，'
                     'can be posted now 现在可以发布，waiting 在等日数、冷却或同时数量。',
    'settings': '经济设置：值，以及来自配置、数据包还是默认；最后是 si_exchange 数据包里的错误数。',
}
MAX_CHARS = 3500
_ITEM_ID = re.compile(r'(?<![\w:/.-])([a-z0-9_.-]+:[a-z0-9_./-]+)')
_UNKNOWN = re.compile(r'(?i)unknown or incomplete command|incorrect argument|unknown command')
_PLAYER = re.compile(r'[A-Za-z0-9_]{1,16}')


def _int(value, default, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _label(names, item_id):
    entry = (names or {}).get(item_id) or {}
    label = (entry.get('zh') or entry.get('en') or '') if isinstance(entry, dict) else ''
    return str(label).replace('<MISSING>', '').strip()


def resolve_item(query, names):
    """An item id from an id, or from an exact (then unique partial) Chinese/English name."""
    value = str(query or '').strip().strip('"')
    if re.fullmatch(r'[a-z0-9_.-]+:[a-z0-9_./-]+', value):
        return value
    if re.fullmatch(r'[a-z0-9_./-]+', value):
        return 'minecraft:' + value
    wanted = value.casefold()
    if not wanted:
        return None
    exact, partial = [], []
    for item, entry in (names or {}).items():
        if not isinstance(entry, dict):
            continue
        labels = [str(entry.get('zh') or '').casefold(), str(entry.get('en') or '').casefold()]
        if wanted in labels:
            exact.append(item)
        elif any(wanted in label for label in labels if label):
            partial.append(item)
    if exact:
        return sorted(exact, key=lambda item: (not item.startswith('minecraft:'), item))[0]
    return partial[0] if len(partial) == 1 else None


def annotate(text, names):
    """Add the Chinese name after the first mention of each item id."""
    seen = set()

    def named(match):
        item = match.group(1)
        label = _label(names, item)
        if not label or item in seen:
            return item
        seen.add(item)
        return f'{item}（{label}）'
    return _ITEM_ID.sub(named, str(text or ''))


def commands(topic, args, privileged, names=None):
    """The fixed commands for a topic, or a string explaining why there are none."""
    args = args or {}
    if topic == 'overview':
        return [f'mio_icif market audit {_int(args.get("count"), 5, 1, 20)}']
    if topic == 'ledger':
        return [f'mio_icif market ledger {_int(args.get("days"), 7, 1, 30)}']
    if topic == 'simulate':
        item = resolve_item(args.get('item'), names)
        if not item:
            return f'要估算卖出，得说清是哪种商品（中文名或物品 ID），「{args.get("item") or ""}」对不上。'
        command = f'mio_icif market simulate {item} {_int(args.get("quantity"), 64, 1, 100000)}'
        player = str(args.get('player') or '').strip()
        if player and privileged:
            if not _PLAYER.fullmatch(player):
                return '玩家名格式不对。'
            command += ' ' + player
        return [command]
    if topic == 'bounties':
        return ['mio_icif bounty list']
    if topic == 'reactor':
        incident = args.get('id')
        if privileged and incident not in (None, ''):
            return [f'mio_icif reactor audit {_int(incident, 0, 0, 10 ** 9)}']
        return ['mio_icif reactor audit']
    if topic == 'arbitrage':
        return [f'mio_icif market arbitrage {_int(args.get("count"), 10, 1, 20)}']
    if topic == 'calibrate':
        return ['mio_icif market calibrate']
    if topic == 'bounty_tables':
        return ['mio_icif bounty tables']
    if topic == 'settings':
        return ['mio_icif bounty settings', 'mio_icif bounty errors']
    return 'topic 只能是 ' + '、'.join(TOPICS)


def _mask_reactor(text):
    """Members see when and what, not where: the bracketed field of each entry is the position."""
    lines = []
    for line in str(text or '').splitlines():
        if re.match(r'\s*#\d+', line):
            line = re.sub(r'\[[^\]]*\]', '[坐标只对管理员显示]', line)
            line = re.sub(r'-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?', '…', line)
        lines.append(line)
    return '\n'.join(lines)


def report(query, prefix, args, privileged, names=None):
    topic = str((args or {}).get('topic') or 'overview')
    if topic not in TOPICS:
        return f'{prefix} topic 只能是 ' + '、'.join(TOPICS) + '。'
    if topic in ADMIN_TOPICS and not privileged:
        return (f'{prefix} {LABELS[topic]}只有管理员能查（里面是经济平衡和刷物品相关的细节）。'
                '普通群友可以查交易所概况、货币发行与销毁、卖出估算、悬赏和反应堆事故记录。')
    planned = commands(topic, args, privileged, names)
    if isinstance(planned, str):
        return f'{prefix} {planned}'
    outputs = []
    for command in planned:
        text = str(query(command) or '').strip()
        if _UNKNOWN.search(text):
            return (f'{prefix} 服务器上的奇点迭代没有认这条命令（{command}），'
                    '可能还是旧版本或者参数不对。')
        outputs.append(text or '（没有返回内容）')
    text = '\n'.join(outputs)
    if topic == 'reactor' and not privileged:
        text = _mask_reactor(text)
    text = annotate(text, names)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit('\n', 1)[0] + '\n…（后面还有，太长没列出）'
    return (f'{prefix} 奇点迭代{LABELS[topic]}（实时，服务器原文是英文；币就是交易所货币，mB 是毫桶）：\n' + text
            + f'\n怎么读：{GLOSSARY[topic]}')
