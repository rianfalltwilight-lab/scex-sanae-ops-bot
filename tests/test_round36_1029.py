"""Round 36 (10-29 morning review): "服务器有多少在运行的水车和大型水车" could only be answered from the
16-row top list ("都不满 200 个"); server_world topic=machines now takes block and reads every page.
Also the bridge log names each tool call's arguments and time. Offline."""
import os
import tempfile
import unittest
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
import sanae_local_ops
import world_status

s = runtime_tests.s
NAMES = {'create:water_wheel': {'zh': '水车', 'en': 'Water Wheel'},
         'create:large_water_wheel': {'zh': '大型水车', 'en': 'Large Water Wheel'},
         'minecraft:spawner': {'zh': '刷怪笼', 'en': 'Monster Spawner'},
         'minecraft:hopper': {'zh': '漏斗', 'en': 'Hopper'}}


def page(number, total, rows, start=1):
    body = '\n'.join(f'{start + i}. {label} {count}' for i, (label, count) in enumerate(rows))
    return f'Block Entities by Type\n{body}\n<< Page {number} of {total} >>'


class Pages:
    """Two pages; the two types tied at 6 swap across the page boundary on the second pass."""
    def __init__(self):
        self.calls = []
        self.passes = [
            {1: [('minecraft:hopper', 166), ('create:large_water_wheel', 57), ('mod:a', 6)],
             2: [('create:water_wheel', 6), ('<lithium_sleeping>', 4), ('minecraft:mob_spawner', 2)]},
            {1: [('minecraft:hopper', 166), ('create:large_water_wheel', 57), ('create:water_wheel', 6)],
             2: [('create:water_wheel', 6), ('<lithium_sleeping>', 4), ('minecraft:mob_spawner', 2)]},
        ]

    def __call__(self, command):
        self.calls.append(command)
        number = int(command.rsplit(' ', 1)[1])
        current = self.passes[min(1, (len(self.calls) - 1) // 2)]      # two pages a pass
        if number not in current:
            return "Page doesn't exist!"
        return page(number, 2, current[number], 1 + 3 * (number - 1))


class BlockCountTests(unittest.TestCase):
    def test_passes_are_merged_and_paging_stops(self):
        pages = Pages()
        scan = world_status.scan_statistics(pages)
        labels = [label for label, _ in scan['rows']]
        self.assertIn('mod:a', labels)                      # only in the first pass
        self.assertEqual(1, labels.count('create:water_wheel'))
        self.assertTrue(scan['complete'])
        self.assertEqual(2, scan['total'])
        self.assertEqual(4, len(pages.calls))               # "Page 2 of 2" ends each pass

    def test_names_find_every_matching_kind(self):
        text = world_status.format_block_count('[怀旧]', '水车', world_status.scan_statistics(Pages()), NAMES)
        self.assertIn('大型水车（create:large_water_wheel） 57 个（第 2 名）', text)
        self.assertIn('水车（create:water_wheel） 6 个', text)
        self.assertIn('翻完了全部 2 页', text)
        self.assertIn('Lithium 暂停的休眠方块实体（此刻 4 个）', text)
        spawner = world_status.format_block_count('[怀旧]', '刷怪笼', world_status.scan_statistics(Pages()), NAMES)
        self.assertIn('刷怪笼（minecraft:mob_spawner） 2 个', spawner)   # the block is minecraft:spawner
        by_id = world_status.format_block_count('[怀旧]', 'create:water_wheel', world_status.scan_statistics(Pages()))
        self.assertIn('create:water_wheel 6 个', by_id)

    def test_nothing_found_and_partial_scans_say_so(self):
        none = world_status.format_block_count('[怀旧]', '风车', world_status.scan_statistics(Pages()), NAMES)
        self.assertIn('已加载的区块里没有正在运算的「风车」', none)
        self.assertIn('换成方块 ID', none)

        def broken(command):
            if command.endswith(' 2'):
                raise TimeoutError()
            return page(1, 2, [('minecraft:hopper', 166)])
        partial = world_status.format_block_count('[怀旧]', '漏斗', world_status.scan_statistics(broken), NAMES)
        self.assertIn('漏斗（minecraft:hopper） 166 个', partial)
        self.assertIn('只翻到第 1 页（共 2 页）', partial)

    def test_the_tool_reads_every_page_when_block_is_given(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        server = {'id': 'legacy', 'prefix': '[怀旧]', 'server_dir': temp.name}
        pages = Pages()
        with mock.patch.dict(os.environ, {'OPS_STATE_BASE': temp.name}), \
                mock.patch.object(sanae_local_ops, '_resolved_server', return_value=server), \
                mock.patch.object(sanae_local_ops.server_traffic, 'recover'), \
                mock.patch.object(sanae_local_ops.server_registry, 'query_server',
                                  side_effect=lambda _server, command: pages(command)), \
                mock.patch.object(sanae_local_ops.world_records, 'item_names', return_value=NAMES):
            text = sanae_local_ops.execute_insight('server_world', {'topic': 'machines', 'block': '水车'}, server, False)
        self.assertIn('水车（create:water_wheel） 6 个', text)
        self.assertTrue(all(command.startswith('statistics block-entities byType ') for command in pages.calls))
        schema = s.SERVER_WORLD['function']['parameters']['properties']
        self.assertIn('block', schema)


class ToolLogTests(unittest.TestCase):
    def test_arguments_without_free_text(self):
        shown = s._tool_log_args({'topic': 'files', 'look': True, 'message': '私下的话', 'items': [1, 2],
                                  'query': 'x' * 60})
        self.assertIn('topic=files', shown)
        self.assertIn('look=True', shown)
        self.assertNotIn('私下的话', shown)
        self.assertNotIn('items', shown)
        self.assertIn('query=' + 'x' * 40 + '…', shown)
        self.assertEqual('', s._tool_log_args(None))


if __name__ == '__main__':
    unittest.main()
