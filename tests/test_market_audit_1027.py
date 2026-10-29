"""Round 21: the SI 0.1.10.29 exchange audit commands as a read-only tool. Offline.

Fixtures are the server's real output on 10-27 11:20 (machine time).
"""
import unittest
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
import command_catalog
import market_audit
import sanae_local_ops

s = runtime_tests.s
NAMES = {'minecraft:pumpkin': {'zh': '南瓜', 'en': 'Pumpkin'},
         'minecraft:golden_apple': {'zh': '金苹果', 'en': 'Golden Apple'},
         'minecraft:enchanted_golden_apple': {'zh': '附魔金苹果', 'en': 'Enchanted Golden Apple'},
         'mio_icif:resource/item_iridium': {'zh': '铱', 'en': 'Iridium'}}
OUT = {
    'mio_icif market audit 5': ('Exchange, day 294: 0 player listings, 0 resting orders, 3 commissions, 0 open futures (collateral 0 coins)\n'
                                'Last 7 days: 0 coins created, 0 destroyed, net 0\nNo trades between players in the last 7 days.'),
    'mio_icif market ledger 3': ('Day 292: created 0, destroyed 0, net 0\nDay 293: created 0, destroyed 0, net 0\n'
                                 'Day 294: created 0, destroyed 0, net 0\n3 days: created 0, destroyed 0, net 0; sell pressure 0.0 items; auction orders 0'),
    'mio_icif market simulate minecraft:pumpkin 64': (
        'minecraft:pumpkin: price 40, bid without pressure 38.8, depth 256 (player) / 1024 (server) items, sell pressure 0.0, sold today 0\n'
        'Selling 64 now pays 2,132.0 coins (average 33.3, last bid 28.4); alone at the auction 2132; one sale pays at most 7,946.2'),
    'mio_icif bounty list': ('Market day 294. Left this week: advanced 16 of 16 items, special 1 of 1 items, UU-matter 4000 of 4000 mB; vault 0 kinds\n'
                             'No open bounty.'),
    'mio_icif market arbitrage 10': (
        '28349 recipes, 59 commodities buyable now: 23 loops, 4532 items on recipe cycles that multiply them\n'
        'minecraft:enchanted_golden_apple: bid 4,446.5, make 450.0, profit 3,996.5, EU per item 1,296,000.0 (EU per coin 324.3), '
        'via kaleidoscope_nether:star_dust/golden_apple from golden_apple'),
    'mio_icif reactor audit': ('Reactor incidents (1), newest first. Click a line to open its report:\n'
                               '#3  10-27 10:02:11  minecraft:overworld  [-1520, 64, 2210]  meltdown'),
}


def query(command):
    return OUT.get(command, 'Unknown or incomplete command, see below for error')


class CommandTests(unittest.TestCase):
    def test_fixed_commands_and_bounds(self):
        self.assertEqual(['mio_icif market audit 5'], market_audit.commands('overview', {}, False))
        self.assertEqual(['mio_icif market ledger 30'], market_audit.commands('ledger', {'days': 400}, False))
        self.assertEqual(['mio_icif market simulate minecraft:pumpkin 64'],
                         market_audit.commands('simulate', {'item': '南瓜'}, False, NAMES))
        self.assertEqual(['mio_icif market simulate minecraft:pumpkin 10 Frozen9'],
                         market_audit.commands('simulate', {'item': 'pumpkin', 'quantity': 10, 'player': 'Frozen9'}, True))
        # A member cannot look at another player's allowance.
        self.assertEqual(['mio_icif market simulate minecraft:pumpkin 10'],
                         market_audit.commands('simulate', {'item': 'pumpkin', 'quantity': 10, 'player': 'Frozen9'}, False))
        self.assertIn('对不上', market_audit.commands('simulate', {'item': '不存在的东西'}, False, NAMES))
        self.assertEqual(['mio_icif reactor audit 3'], market_audit.commands('reactor', {'id': 3}, True))
        self.assertEqual(['mio_icif reactor audit'], market_audit.commands('reactor', {'id': 3}, False))
        self.assertEqual(['mio_icif bounty settings', 'mio_icif bounty errors'], market_audit.commands('settings', {}, True))

    def test_read_and_write_classes(self):
        for command in ('mio_icif market audit 5', 'mio_icif market ledger 7', 'mio_icif market arbitrage 10',
                        'mio_icif market calibrate', 'mio_icif market simulate minecraft:pumpkin 64',
                        'mio_icif market simulate minecraft:pumpkin 64 Frozen9', 'mio_icif bounty list',
                        'mio_icif bounty tables', 'mio_icif bounty errors', 'mio_icif bounty settings',
                        'mio_icif reactor audit', 'mio_icif reactor audit 3'):
            with self.subTest(command=command):
                self.assertEqual('read', command_catalog.classify(command))
        for command in ('mio_icif bounty post mio_icif:gold', 'mio_icif bounty cancel 2', 'mio_icif reactor audit clear'):
            with self.subTest(command=command):
                self.assertEqual('confirm', command_catalog.classify(command))


class ReportTests(unittest.TestCase):
    def test_member_topics(self):
        text = market_audit.report(query, '[怀旧]', {'topic': 'overview'}, False, NAMES)
        self.assertTrue(text.startswith('[怀旧] 奇点迭代交易所概况（实时'))
        self.assertIn('3 commissions', text)
        sale = market_audit.report(query, '[怀旧]', {'topic': 'simulate', 'item': '南瓜'}, False, NAMES)
        self.assertIn('minecraft:pumpkin（南瓜）: price 40', sale)
        self.assertEqual(1, sale.count('（南瓜）'))
        self.assertIn('No open bounty', market_audit.report(query, '[怀旧]', {'topic': 'bounties'}, False))

    def test_admin_topics_and_reactor_coordinates(self):
        refused = market_audit.report(query, '[怀旧]', {'topic': 'arbitrage'}, False, NAMES)
        self.assertIn('套利与增殖循环只有管理员能查', refused)
        loops = market_audit.report(query, '[怀旧]', {'topic': 'arbitrage'}, True, NAMES)
        self.assertIn('minecraft:enchanted_golden_apple（附魔金苹果）: bid 4,446.5', loops)
        member = market_audit.report(query, '[怀旧]', {'topic': 'reactor'}, False)
        self.assertIn('[坐标只对管理员显示]', member)
        self.assertNotIn('-1520', member)
        self.assertIn('meltdown', member)
        self.assertIn('[-1520, 64, 2210]', market_audit.report(query, '[怀旧]', {'topic': 'reactor'}, True))

    def test_old_mod_versions_and_long_output(self):
        self.assertIn('没有认这条命令', market_audit.report(query, '[怀旧]', {'topic': 'calibrate'}, True))
        long = market_audit.report(lambda c: '\n'.join(f'row {i}' for i in range(2000)), '[怀旧]', {'topic': 'calibrate'}, True)
        self.assertIn('太长没列出', long)
        self.assertLess(len(long), market_audit.MAX_CHARS + 200)


class WiringTests(unittest.TestCase):
    def test_tool_is_offered_and_routed(self):
        self.assertIn(s.MARKET_AUDIT, s.TOOLS_MEMBER)
        self.assertIn('market_audit', s.READ_QUERY_TOOL_NAMES)
        self.assertIn('market_audit', sanae_local_ops.INSIGHT_TOOLS)
        server = {'id': 'legacy', 'prefix': '[怀旧]', 'server_dir': ''}
        with mock.patch.object(sanae_local_ops, '_resolved_server', return_value=server), \
                mock.patch.object(sanae_local_ops.server_registry, 'query_server', side_effect=lambda srv, c: query(c)), \
                mock.patch.object(sanae_local_ops.world_records, 'item_names', return_value=NAMES):
            text = sanae_local_ops.execute_insight('market_audit', {'topic': 'ledger', 'days': 3}, server, False)
        self.assertIn('3 days: created 0, destroyed 0', text)

    def test_questions_route_to_the_tools(self):
        for text in ('早苗，查一下交易所的账', '这周发行了多少币', '南瓜现在卖64个能得多少', '现在有什么悬赏',
                     '有没有反应堆事故', '有哪些套利路线'):
            with self.subTest(text=text):
                self.assertTrue(s._has_server_query_intent(text))
        self.assertFalse(s._has_server_query_intent('今天去银行对一下信用卡'))


if __name__ == '__main__':
    unittest.main()
