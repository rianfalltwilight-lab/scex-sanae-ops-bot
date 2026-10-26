"""Round 9: lag alerts, mod update notices, weekly player section, bug reports,
and the DeepSeek fallback for tool turns.

Offline only: temp trees and mocked model, RCON and QQ calls.
"""
import copy
import json
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock

from tests.test_fusion_integration import FusionCase, bridge, s
from tests import test_yachiyo_runtime as runtime_tests
from tests import test_remote_legacy_monitor as monitor_tests
from tests import test_weekly_remote as weekly_tests
import bug_reports as bugs
import server_watch as sw
import weekly_reports

TZ = sw.TZ
T0 = datetime(2026, 10, 26, 21, 0, tzinfo=TZ).timestamp()
TICKS = ('Overworld: 7.900 TPS (126.583 ms/tick)\nThe Nether: 20.000 TPS (3.837 ms/tick)\n'
         'thaumcraft:outer_lands: 20.000 TPS (0.029 ms/tick)\nOverall: 7.900 TPS (131.000 ms/tick)')
ONLINE = 'There are 3 of a max of 20 players online: Alice, Bob, Carol'


def at(*args):
    return datetime(*args, tzinfo=TZ).timestamp()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def lagging(i, tps=8.0, step=60):
    return {'timestamp': T0 + step * i, 'tps': tps, 'mspt': 120.0, 'tick_parser': 2}


def feed(rows, state=None, **kw):
    out = []
    for row in rows:
        state, streak = sw.lag_step(state, row, **kw)
        out.append(streak)
    return state, out


class LagStepTests(unittest.TestCase):
    def test_five_low_minutes_alert_once_then_hourly(self):
        _state, out = feed([lagging(i) for i in range(66)])
        hits = [i for i, streak in enumerate(out) if streak]
        self.assertEqual([4, 64], hits)
        self.assertEqual((5, 8.0, 120.0, False), (out[4]['samples'], out[4]['min_tps'],
                                                  out[4]['avg_mspt'], out[4]['continuing']))
        self.assertTrue(out[64]['continuing'])

    def test_recovered_missing_or_gapped_samples_restart_the_streak(self):
        for broken in ({'timestamp': T0 + 120, 'tps': 15.0, 'mspt': 40.0, 'tick_parser': 2},
                       {'timestamp': T0 + 120, 'tps': None, 'mspt': None, 'tick_parser': 2},
                       {'timestamp': T0 + 120, 'tps': 5.0, 'mspt': 200.0, 'tick_parser': 1}):
            with self.subTest(broken=broken):
                rows = [lagging(i) for i in range(8)]
                rows[2] = broken
                _state, out = feed(rows)
                self.assertEqual([7], [i for i, streak in enumerate(out) if streak])
        rows = [lagging(i) for i in range(3)] + [lagging(i, step=60) for i in range(6, 12)]
        _state, out = feed(rows)            # a 240 s gap after the third sample
        self.assertEqual([7], [i for i, streak in enumerate(out) if streak])

    def test_threshold_is_strictly_below_ten(self):
        _state, out = feed([lagging(i, tps=10.0) for i in range(8)])
        self.assertFalse(any(out))

    def test_alert_names_slow_dimensions_and_players(self):
        _state, out = feed([lagging(i) for i in range(5)])
        text = sw.format_lag_alert('[怀旧]', out[4], TICKS, ONLINE)
        self.assertIn('[怀旧]【卡顿提醒】TPS 已连续约 5 分钟低于 10（10-26 21:00 起，最低 8.0，平均 MSPT 120）', text)
        self.assertIn('最耗时的维度：Overworld 127 ms/tick、The Nether 4 ms/tick', text)
        self.assertNotIn('outer_lands', text)
        self.assertNotIn('Overall', text)
        self.assertIn('在线 3 人：Alice、Bob、Carol', text)


OLD_MODS = {'mods': [
    {'file': 'thaumcraft-port.277.jar', 'id': 'thaumcraft', 'name': 'Thaumcraft',
     'version': '0.2.2.107-port.277', 'metadata': 'META-INF/neoforge.mods.toml'},
    {'file': 'jei-19.1.jar', 'id': 'jei', 'name': 'JEI', 'version': '19.1', 'metadata': 'toml'},
    {'file': '[AE2多配方兼容] polyeng-0.4.1.jar', 'id': '[AE2多配方兼容] polyeng-0.4.1',
     'name': '[AE2多配方兼容] polyeng-0.4.1', 'version': '', 'metadata': 'filename'},
    {'file': 'oldmod-1.0.jar', 'id': 'oldmod', 'name': 'Old Mod', 'version': '1.0', 'metadata': 'toml'},
    {'file': 'same-2.0.jar', 'id': 'same', 'name': 'Same', 'version': '2.0', 'metadata': 'toml'}]}
NEW_MODS = {'mods': [
    {'file': 'thaumcraft-port.278.jar', 'id': 'thaumcraft', 'name': 'Thaumcraft',
     'version': '0.2.2.107-port.278', 'metadata': 'META-INF/neoforge.mods.toml'},
    {'file': 'jei-19.1.jar', 'id': 'jei', 'name': 'JEI', 'version': '19.1', 'metadata': 'toml'},
    {'file': '[AE2多配方兼容] polyeng-0.4.2.jar', 'id': '[AE2多配方兼容] polyeng-0.4.2',
     'name': '[AE2多配方兼容] polyeng-0.4.2', 'version': '', 'metadata': 'filename'},
    {'file': 'newmod-3.0.jar', 'id': 'newmod', 'name': 'New Mod', 'version': '3.0', 'metadata': 'toml'},
    {'file': 'same-2.0.jar', 'id': 'same', 'name': 'Same', 'version': '2.0', 'metadata': 'toml'}]}


class ModChangeTests(unittest.TestCase):
    def test_versions_additions_removals_and_replaced_files(self):
        changes = sw.mod_changes(OLD_MODS, NEW_MODS, fetched=['thaumcraft-port.278.jar', 'same-2.0.jar'])
        self.assertEqual({('Thaumcraft', '0.2.2.107-port.277', '0.2.2.107-port.278'),
                          ('[AE2多配方兼容] polyeng', '0.4.1', '0.4.2'), ('Same', '2.0', '2.0')},
                         set(changes['updated']))
        self.assertEqual([('New Mod', '3.0')], changes['added'])
        self.assertEqual([('Old Mod', '1.0')], changes['removed'])
        self.assertEqual({'updated': [], 'added': [], 'removed': []}, sw.mod_changes(NEW_MODS, NEW_MODS))

    def test_notice_text_and_limit(self):
        changes = sw.mod_changes(OLD_MODS, NEW_MODS, fetched=['same-2.0.jar'])
        text = sw.format_update('[怀旧]', changes, T0, summary='主要是神秘时代更新。')
        self.assertTrue(text.startswith('[怀旧]【模组更新】10-26 21:00 发现服务器模组有变动：更新 3 个，新增 1 个，移除 1 个\n主要是神秘时代更新。'))
        self.assertIn('- Thaumcraft：0.2.2.107-port.277 → 0.2.2.107-port.278', text)
        self.assertIn('- Same 2.0（版本号未变，文件已替换）', text)
        self.assertIn('新增：\n- New Mod 3.0', text)
        self.assertIn('移除：\n- Old Mod 1.0', text)
        self.assertIn('……另有 3 项', sw.format_update('[怀旧]', changes, T0, limit=2))


class PlayerWeekTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.history = self.root / 'player-stats-history'

    def test_snapshot_slots_start_at_twenty_and_old_days_are_pruned(self):
        stats = {'players': [{'name': 'Alice', 'play_hours': 1.5, 'deaths': 2, 'top_mined': [['x', 1]]}]}
        write_json(self.history / '2026-08-01.json', {'taken_at': 0, 'players': {}})
        first = sw.snapshot_player_stats(stats, self.history, now=at(2026, 10, 26, 0, 30))
        self.assertEqual('2026-10-25.json', first.name)
        self.assertEqual({'Alice': {'play_hours': 1.5, 'deaths': 2}}, json.loads(first.read_text('utf-8'))['players'])
        self.assertEqual('2026-10-26.json', sw.snapshot_player_stats(stats, self.history, now=at(2026, 10, 26, 20, 10)).name)
        self.assertIsNone(sw.snapshot_player_stats(stats, self.history, now=at(2026, 10, 26, 20, 40)))
        self.assertFalse((self.history / '2026-08-01.json').exists())

    def snapshot(self, day, when, players):
        write_json(self.history / f'{day}.json', {'taken_at': when, 'players': players})

    def current(self, players):
        write_json(self.root / 'player-stats.json', {'generated_at': 0, 'players': players})

    def test_week_deltas_from_the_snapshot_at_the_window_start(self):
        base = {'Alice': {'play_hours': 100, 'deaths': 10, 'mined': 1000, 'distance_km': 51, 'mob_kills': 11},
                'Bob': {'play_hours': 5}}
        self.snapshot('2026-10-18', at(2026, 10, 18, 20, 10), dict(base, Alice=dict(base['Alice'], play_hours=90)))
        self.snapshot('2026-10-19', at(2026, 10, 19, 20, 10), base)
        self.current([{'name': 'Alice', 'play_hours': 112.5, 'deaths': 13, 'mined': 21000, 'distance_km': 95,
                       'mob_kills': 30},
                      {'name': 'Bob', 'play_hours': 5}, {'name': 'Newbie', 'play_hours': 3, 'deaths': 0},
                      {'name': 'ab12cd34', 'play_hours': 2}])
        now = at(2026, 10, 26, 20, 0)
        week = sw.player_week(self.root, now - 7 * 86400, now)
        self.assertEqual((2, ['Newbie'], False), (week['active'], week['new'], week['partial']))
        self.assertEqual([('Alice', 12.5), ('Newbie', 3)], week['hours'])
        self.assertEqual(['玩家 本周上线 2 人｜新面孔 Newbie', '时长榜 Alice 12.5h、Newbie 3.0h',
                          '死得最多 Alice 3 次｜打怪最多 Alice 19只｜挖得最多 Alice 2.0 万块｜跑得最远 Alice 44 km'],
                         sw.format_week(week))

    def test_short_history_is_marked_and_a_fresh_baseline_says_nothing(self):
        self.current([{'name': 'Alice', 'play_hours': 12}])
        now = at(2026, 10, 26, 20, 0)
        self.snapshot('2026-10-22', at(2026, 10, 22, 20, 10), {'Alice': {'play_hours': 10}})
        week = sw.player_week(self.root, now - 7 * 86400, now)
        self.assertTrue(week['partial'])
        self.assertIn('（玩家数据从 10-22 20:10 开始记录，不满 7 天）', sw.format_week(week))
        (self.history / '2026-10-22.json').unlink()
        self.snapshot('2026-10-26', now - 3600, {'Alice': {'play_hours': 11}})
        self.assertIsNone(sw.player_week(self.root, now - 7 * 86400, now))
        self.assertEqual([], sw.format_week(None))


class WeeklyPlayerSectionTests(unittest.TestCase):
    setUp = weekly_tests.RemoteWeeklyReportTests.setUp
    telemetry = weekly_tests.RemoteWeeklyReportTests.telemetry

    def test_remote_report_carries_the_player_lines(self):
        write_json(self.root / 'player-stats-history' / '2026-10-19.json',
                   {'taken_at': weekly_tests.NOW - 7 * 86400 + 600, 'players': {'Alice': {'play_hours': 100}}})
        write_json(self.root / 'player-stats.json', {'players': [{'name': 'Alice', 'play_hours': 108}]})
        data = weekly_reports.build_report(self.telemetry(), now=weekly_tests.NOW)
        text = weekly_reports.format_report(data)
        self.assertEqual(1, data['players_week']['active'])
        self.assertIn('玩家 本周上线 1 人\n时长榜 Alice 8.0h', text)
        (self.root / 'player-stats.json').unlink()
        self.assertNotIn('玩家 本周上线', weekly_reports.format_report(
            weekly_reports.build_report(self.telemetry(), now=weekly_tests.NOW)))


class MonitorNoticeTests(unittest.TestCase):
    setUp = monitor_tests.RemoteMonitorTests.setUp

    def queue(self, name):
        return (self.monitor.load_state(name, {'items': []}) or {}).get('items', [])

    def test_lag_alert_is_queued_once_rate_limited_and_flushed(self):
        monitor = self.monitor
        monitor.LAG_ALERT_GROUPS = (2105,)
        rcon = mock.Mock(side_effect=lambda server, command: TICKS if command == 'neoforge tps' else ONLINE)
        with mock.patch.object(monitor, 'query_server', rcon):
            self.assertIsNone(monitor._lag_watch(None))
            texts = [monitor._lag_watch(lagging(i)) for i in range(5)]
            self.assertEqual([None] * 4, texts[:4])
            self.assertIn('【卡顿提醒】', texts[4])
            self.assertEqual(1, len(self.queue('legacy-lag-notify-2105')))
            monitor._lag_watch(lagging(5, tps=19))                    # recovered
            again = [monitor._lag_watch(lagging(i)) for i in range(6, 12)]
        self.assertFalse(any(again))                                 # a new streak within the hour
        self.ops.record_event.assert_called_once()
        self.assertEqual('lag_alert', self.ops.record_event.call_args.args[0])
        push = mock.Mock(return_value=True)
        monitor._flush_notices(push)
        push.assert_called_once()
        self.assertEqual(2105, push.call_args.args[1])
        self.assertEqual([], self.queue('legacy-lag-notify-2105'))
        monitor._flush_notices(push)                                 # empty queue: nothing sent
        push.assert_called_once()

    def test_lag_watch_is_off_without_groups(self):
        self.monitor.LAG_ALERT_GROUPS = ()
        self.assertIsNone(self.monitor._lag_watch(lagging(0)))
        self.assertIsNone(self.monitor.load_state('legacy-lag-watch'))

    def test_mirror_job_announces_mod_changes_with_a_summary(self):
        monitor = self.monitor
        monitor.UPDATE_NOTIFY_GROUPS = (2105,)
        monitor.UPDATE_SUMMARY_FN = mock.Mock(return_value='主要是神秘时代更新。')
        inventory = Path(self.temp.name) / 'mod-inventory.json'
        write_json(inventory, OLD_MODS)

        def refresh():
            write_json(inventory, NEW_MODS)
            return ['mods']
        self.ops.refresh_indexes.side_effect = refresh
        sync = mock.Mock(return_value={'remote': 5, 'fetched': ['thaumcraft-port.278.jar'], 'deleted': [],
                                       'skipped': [], 'missing': [], 'changed': True, 'complete': True})
        with mock.patch.object(monitor.remote_mods_mirror, 'sync', sync):
            monitor._mirror_job()
        items = self.queue('legacy-update-notify-2105')
        self.assertEqual(1, len(items))
        self.assertIn('【模组更新】', items[0]['text'])
        self.assertIn('主要是神秘时代更新。', items[0]['text'])
        self.assertIn('Thaumcraft：0.2.2.107-port.277 → 0.2.2.107-port.278', monitor.UPDATE_SUMMARY_FN.call_args.args[0])
        self.assertEqual('mod_update', self.ops.record_event.call_args.args[0])

    def test_update_without_a_previous_index_or_with_a_failing_summary(self):
        monitor = self.monitor
        monitor.UPDATE_NOTIFY_GROUPS = (2105,)
        write_json(Path(self.temp.name) / 'mod-inventory.json', NEW_MODS)
        self.assertIsNone(monitor._announce_update(None, {}))
        monitor.UPDATE_SUMMARY_FN = mock.Mock(side_effect=RuntimeError('model'))
        text = monitor._announce_update(OLD_MODS, {'fetched': []})
        self.assertTrue(text.startswith('[怀旧]【模组更新】'))
        monitor.UPDATE_NOTIFY_GROUPS = ()
        monitor.UPDATE_SUMMARY_FN = None
        self.assertTrue(monitor._announce_update(OLD_MODS, {'fetched': []}))   # recorded, not queued
        self.assertEqual(1, len(self.queue('legacy-update-notify-2105')))

    def test_mirror_extras_keeps_a_daily_stats_snapshot(self):
        monitor = self.monitor
        monitor._mirror_extras = self.real_mirror_extras
        stats = {'generated_at': 1, 'players': [{'name': 'Alice', 'play_hours': 3}]}
        with mock.patch.object(monitor.remote_mods_mirror, 'sync_tree',
                               return_value={'fetched': 0, 'deleted': 0, 'missing': 0}), \
                mock.patch.object(monitor.remote_server_reader, 'player_stats', return_value=stats), \
                mock.patch.object(monitor.command_catalog, 'refresh_server', return_value={'count': 1}):
            monitor._mirror_extras()
        files = list((Path(self.temp.name) / 'player-stats-history').glob('*.json'))
        self.assertEqual(1, len(files))
        self.assertEqual({'Alice': {'play_hours': 3}}, json.loads(files[0].read_text('utf-8'))['players'])


class BugLedgerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.now = [T0]
        write_json(root / 'mods.json', {'mods': [
            {'id': 'thaumcraft', 'name': 'Thaumcraft', 'version': '0.2.2.107-port.278', 'metadata': 'toml'},
            {'id': 'jei', 'name': 'Just Enough Items', 'version': '19.1', 'metadata': 'toml'},
            {'id': 'odd-1.0', 'name': 'odd', 'version': '', 'metadata': 'filename'}]})
        (root / 'events.jsonl').write_text('\n'.join(json.dumps(row) for row in [
            {'timestamp': T0 - 3600, 'type': 'crash', 'details': {'report': 'crash-a.txt'}},
            {'timestamp': T0 - 9 * 3600, 'type': 'crash', 'details': {'report': 'crash-old.txt'}},
            {'timestamp': T0 - 60, 'type': 'join', 'details': {}}]), encoding='utf-8')
        self.ledger = bugs.BugLedger(root / 'bugs.json', inventory_path=root / 'mods.json',
                                     events_path=root / 'events.jsonl', clock=lambda: self.now[0])

    def evidence(self, mid, uid='1001', name='Alice', text='奥法修复台的物品模型缺少 display 数据'):
        return {'message_id': mid, 'uid': uid, 'name': name, 'time': '10-26 21:00', 'text': text}

    def report(self, **kw):
        return dict({'ids': ['m1'], 'title': '奥法修复台物品模型缺 display', 'summary': '手持时模型不对',
                     'mod': 'thaumcraft', 'category': 'visual', 'duplicate_of': 0}, **kw)

    def test_new_items_record_the_mod_version_and_crash_reports(self):
        item, merged = self.ledger.record(self.report(), [self.evidence('m1')], 2103)
        self.assertFalse(merged)
        self.assertEqual((1, 'Thaumcraft', '0.2.2.107-port.278', []),
                         (item['id'], item['mod_name'], item['mod_version'], item['crashes']))
        crash, _ = self.ledger.record(self.report(title='开箱子就崩', category='crash', mod=''),
                                      [self.evidence('m2')], 2103)
        self.assertEqual(['crash-a.txt'], crash['crashes'])
        self.assertEqual(['jei', 'thaumcraft'], self.ledger.installed_ids())

    def test_duplicates_merge_only_into_open_items(self):
        self.ledger.record(self.report(), [self.evidence('m1')], 2103)
        item, merged = self.ledger.record(self.report(duplicate_of=1),
                                          [self.evidence('m1'), self.evidence('m3', uid='1002', name='Bob')],
                                          1001)
        self.assertTrue(merged)
        self.assertEqual((2, 2, [2103, 1001], 3),
                         (item['reports'], len(item['reporters']), item['groups'], len(item['evidence']) + 1))
        self.ledger.set_status(1, 'resolved', 'admin')
        fresh, merged = self.ledger.record(self.report(duplicate_of=1), [self.evidence('m4')], 2103)
        self.assertFalse(merged)
        self.assertEqual(2, fresh['id'])

    def test_manual_reports_guess_the_mod_and_are_rate_limited(self):
        item, reason = self.ledger.manual('thaumcraft 的奥法修复台模型坏了', '1001', 'Alice', 2103, 'q1')
        self.assertEqual(('ok', 'thaumcraft', 'manual'), (reason, item['mod'], item['source']))
        item, _ = self.ledger.manual('Just Enough Items 搜索框打不了字', '1001', 'Alice', 2103)
        self.assertEqual('jei', item['mod'])
        self.assertEqual((None, 'short'), self.ledger.manual('坏了', '1001', 'x', 1))
        for _ in range(3):
            self.ledger.manual('又一个问题描述', '1001', 'Alice', 2103)
        self.assertEqual((None, 'limit'), self.ledger.manual('第六个问题描述', '1001', 'Alice', 2103))
        self.now[0] += 3601
        self.assertEqual('ok', self.ledger.manual('第六个问题描述', '1001', 'Alice', 2103)[1])

    def test_commands_for_members_and_admins(self):
        cmd = lambda text, admin=False: bugs.command(self.ledger, text, '1001', 'Alice', 2103, admin, 'q')
        self.assertIsNone(cmd('奥法修复台坏了'))
        self.assertEqual(bugs.USAGE, cmd('!bug'))
        self.assertEqual('目前没有未处理的问题反馈。', cmd('!bug', True))
        self.assertIn('记下了，编号 #1，关联模组 Thaumcraft 0.2.2.107-port.278', cmd('！BUG thaumcraft 奥法修复台模型坏了'))
        self.assertIn('仅管理员可用', cmd('!bug 详情 1'))
        self.assertIn('仅管理员可用', cmd('!bug 全部'))
        self.assertIn('未处理的问题反馈（1 条', cmd('!bug', True))
        detail = cmd('!bug 详情 1', True)
        self.assertIn('来源：群友 !bug 提交', detail)
        self.assertIn('原话：\n- 10-26 21:00 Alice：thaumcraft 奥法修复台模型坏了', detail)
        self.assertIn('已标为已解决', cmd('!bug 解决 #1 port.279 修了', True))
        self.assertEqual('目前没有未处理的问题反馈。', cmd('!bug', True))
        self.assertIn('【已解决】#1', cmd('!bug 全部', True))
        self.assertIn('处理：10-26 21:00 port.279 修了', cmd('!bug 详情 1', True))
        self.assertIn('已标为未处理', cmd('!bug 重开 1', True))
        self.assertIn('没有编号 #9', cmd('!bug 忽略 9', True))

    def test_daily_digest_after_nine_new_items_and_merges(self):
        self.now[0] = at(2026, 10, 26, 20, 0)
        self.ledger.record(self.report(), [self.evidence('m1')], 2103)
        self.assertEqual((None, []), self.ledger.digest_due())
        self.now[0] = at(2026, 10, 26, 21, 5)
        text, ids = self.ledger.digest_due()
        self.assertEqual([1], ids)
        self.assertTrue(text.startswith('【玩家问题汇总】10-26：新增 1 条（未处理共 1 条）\n#1 [Thaumcraft 0.2.2.107-port.278]'))
        self.ledger.mark_digest(ids, False)
        self.assertEqual((None, []), self.ledger.digest_due())
        self.now[0] += 1801
        text, ids = self.ledger.digest_due()
        self.ledger.mark_digest(ids, True)
        self.assertEqual((None, []), self.ledger.digest_due())
        self.now[0] = at(2026, 10, 27, 21, 0)
        self.assertEqual((None, []), self.ledger.digest_due())       # nothing new: quiet day
        self.now[0] = at(2026, 10, 28, 9, 0)
        self.ledger.record(self.report(duplicate_of=1), [self.evidence('m9', uid='2002', name='Bob')], 1001)
        self.now[0] = at(2026, 10, 28, 21, 0)
        text, ids = self.ledger.digest_due()
        self.assertIn('新增 0 条，补充 1 条', text)
        self.assertIn('补充：#1 奥法修复台物品模型缺 display 又有 1 次反馈', text)


def judge_reply(reports):
    return {'choices': [{'message': {'content': json.dumps({'reports': reports}, ensure_ascii=False)}}]}


class BugJudgeTests(unittest.TestCase):
    def test_output_is_limited_to_the_batch_installed_mods_and_open_items(self):
        batch = [{'id': 'm1', 'sender': 'a', 'name': 'A', 'time': 't', 'text': '奥法修复台模型不对'},
                 {'id': 'm2', 'sender': 'b', 'name': 'B', 'time': 't', 'text': '开箱子就崩'}]
        reply = judge_reply([
            {'ids': ['m1', 'zzz'], 'title': '奥法修复台模型不对', 'summary': '模型\n不对', 'mod': 'thaumcraft',
             'category': 'visual', 'duplicate_of': 0},
            {'ids': ['m1'], 'title': '重复认领', 'mod': '', 'category': 'visual'},
            {'ids': ['m2'], 'title': '开箱子崩溃', 'mod': 'notinstalled', 'category': 'weird', 'duplicate_of': 7},
            {'ids': ['m2'], 'title': '', 'mod': ''}])
        with mock.patch.object(s, '_post', return_value=reply) as post:
            reports = s.judge_bug_reports([], batch, [{'id': 3, 'title': '旧问题', 'mod': ''}], ['thaumcraft'])
        self.assertEqual([
            {'ids': ['m1'], 'title': '奥法修复台模型不对', 'summary': '模型 不对', 'mod': 'thaumcraft',
             'category': 'visual', 'duplicate_of': 0},
            {'ids': ['m2'], 'title': '开箱子崩溃', 'summary': '', 'mod': '', 'category': 'other', 'duplicate_of': 0}],
            reports)
        payload = post.call_args.args[2]
        self.assertEqual({'type': 'disabled'}, payload['thinking'])
        self.assertEqual({'type': 'json_object'}, payload['response_format'])
        sent = json.loads(payload['messages'][1]['content'])
        self.assertEqual((['thaumcraft'], [{'id': 3, 'title': '旧问题', 'mod': ''}]), (sent['installed'], sent['open']))
        self.assertEqual([], s.judge_bug_reports([], [], [], []))

    def test_mod_update_summary_is_one_line_or_empty(self):
        with mock.patch.object(s, '_text_post', return_value={'choices': [{'message': {'content': '主要是神秘时代\n更新。'}}]}):
            self.assertEqual('主要是神秘时代 更新。', s.summarize_mod_update('更新：…'))
        with mock.patch.object(s, '_text_post', side_effect=ValueError('bad')):
            self.assertEqual('', s.summarize_mod_update('更新：…'))

    def test_spark_is_no_longer_offered_over_rcon(self):
        self.assertNotIn('spark', s.RUN_RCON_RO['function']['description'])
        self.assertNotIn('spark', s.RUN_RCON_CONSOLE['function']['description'])
        self.assertFalse(s._member_safe('spark health'))


def tool_call(name, arguments):
    return {'choices': [{'message': {'content': '', 'tool_calls': [{
        'id': 'fixture-call', 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments)}}]}}], 'usage': {}}


def text_reply(content):
    return {'choices': [{'message': {'content': content}}], 'usage': {}}


class ToolFallbackTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def run_query(self, primary, fallback):
        sent = []
        with mock.patch.object(s, 'TEXT_PROVIDER', 'gemini'), mock.patch.object(s, 'TEXT_FALLBACK', 'deepseek'), \
                mock.patch.object(s, '_text_post', side_effect=primary) as text_post, \
                mock.patch.object(s, '_deepseek_tool_fallback_post',
                                  side_effect=lambda payload, **kw: sent.append(copy.deepcopy(payload)) or next(fallback)) as tool_post, \
                mock.patch.object(s, '_deepseek_fallback_post') as chat_post, \
                mock.patch.object(s, 'execute_tool', return_value='There are 6 of a max of 20 players online') as execute:
            answer, usage, used = s.run_agent('当前世界多少天了', 'fixture-member', False, 'fixture-group',
                                              selected_server={'id': 'legacy'})
        return answer, usage, sent, text_post, tool_post, chat_post, execute

    def test_relay_drop_before_any_tool_moves_the_turn_to_deepseek(self):
        replies = iter([tool_call('run_rcon', {'command': 'list'}), text_reply('现在 6 个人在线。')])
        answer, usage, sent, text_post, tool_post, chat_post, execute = self.run_query(
            urllib.error.URLError('ssl eof'), replies)
        self.assertEqual('现在 6 个人在线。', answer)
        self.assertTrue(usage.text_fallback)
        self.assertEqual(1, text_post.call_count)
        self.assertEqual(2, tool_post.call_count)
        chat_post.assert_not_called()
        execute.assert_called_once()
        self.assertEqual('required', sent[0]['tool_choice'])
        self.assertTrue(sent[0]['tools'])

    def test_no_switch_once_a_tool_has_run(self):
        primary = [tool_call('run_rcon', {'command': 'list'}), urllib.error.URLError('ssl eof')]
        answer, usage, sent, text_post, tool_post, chat_post, execute = self.run_query(primary, iter([]))
        self.assertEqual(2, text_post.call_count)
        tool_post.assert_not_called()
        execute.assert_called_once()
        self.assertIn('没能完成', answer)

    def test_request_shape_for_deepseek(self):
        payload = {'model': 'gemini', 'stream': False, 'max_tokens': 100, 'thinking': {'type': 'enabled'},
                   'reasoning_effort': 'high', 'tool_choice': 'required', '_background_thinking': 'low',
                   'tools': [s.RUN_RCON_RO],
                   'messages': [{'role': 'system', 'content': 'sys'},
                                {'role': 'user', 'content': 'q', '_gemini_parts': ['x']}]}
        with mock.patch.object(s, '_post', return_value={'choices': []}) as post:
            result = s._deepseek_tool_fallback_post(payload, timeout=30)
        url, _key, request, timeout = post.call_args.args
        self.assertTrue(url.endswith('/beta/chat/completions'))
        self.assertEqual((s.DEEPSEEK_FALLBACK_MODEL, {'type': 'disabled'}, 'required', 30),
                         (request['model'], request['thinking'], request['tool_choice'], timeout))
        self.assertNotIn('reasoning_effort', request)
        self.assertFalse([key for key in request if key.startswith('_')])
        self.assertNotIn('_gemini_parts', request['messages'][1])
        self.assertTrue(request['tools'][0]['function']['strict'])
        self.assertNotIn('strict', payload['tools'][0]['function'])
        self.assertTrue(result['_text_fallback'])


class BugBridgeTests(FusionCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.gid = bridge.SOCIAL_LITE_GROUP
        self.ledger = bugs.BugLedger(Path(temp.name) / 'bugs.json')
        self.judge = mock.Mock()
        for name, value in {'BUG_LEDGER': self.ledger, 'BUG_REPORT_GROUPS': frozenset({self.gid}),
                            'BUG_DIGEST_GROUPS': frozenset({2105}),
                            '_BUG_JUDGES': {self.gid: self.judge},
                            '_BUG_MESSAGES': bridge.collections.OrderedDict()}.items():
            self.stack.enter_context(mock.patch.object(bridge, name, value))

    def test_messages_are_context_and_longer_ones_get_judged(self):
        for text, eligible in (('奥法修复台的物品模型不对', True), ('!bug 奥法修复台', False), ('好卡', False)):
            bridge._bug_observe(self.event(text, mid=text), text)
            self.assertEqual(eligible, self.judge.observe.call_args.kwargs['eligible'], text)
        self.assertEqual(3, self.judge.observe.call_count)
        bridge._bug_observe(self.event('机器人自己的消息很长很长', uid=bridge.WIKI_BOT_QQ), '机器人自己的消息很长很长')
        bridge._bug_observe(self.event('别的群里的问题描述', gid=111), '别的群里的问题描述')
        self.assertEqual(3, self.judge.observe.call_count)
        self.assertEqual('测试成员', bridge._BUG_MESSAGES['奥法修复台的物品模型不对']['name'])

    def test_judged_reports_land_in_the_ledger_with_the_original_words(self):
        bridge._bug_observe(self.event('奥法修复台的物品模型不对', mid='m1'), '奥法修复台的物品模型不对')
        found = [{'ids': ['m1'], 'title': '奥法修复台模型不对', 'summary': '', 'mod': '', 'category': 'visual',
                  'duplicate_of': 0}]
        with mock.patch.object(s, 'judge_bug_reports', return_value=found) as judge:
            self.assertEqual([], bridge._bug_judge(self.gid, [], [{'id': 'm1'}]))
        item = self.ledger.get(1)
        self.assertEqual(('ai', '奥法修复台的物品模型不对', '2001'),
                         (item['source'], item['evidence'][0]['text'], item['reporters'][0]['uid']))
        with mock.patch.object(s, 'judge_bug_reports', side_effect=ValueError('json')):
            self.assertEqual([], bridge._bug_judge(self.gid, [], [{'id': 'm2'}]))
        self.assertEqual(1, len(self.ledger.items()))

    def test_bug_command_route(self):
        self.handler._process_event(self.event('!bug 奥法修复台物品模型缺 display'))
        self.dispatch_workers()
        self.assertIn('记下了，编号 #1', self.send.call_args.args[0])
        self.stack.enter_context(mock.patch.object(bridge, 'OWNERS', {'90001'}))
        self.handler._process_event(self.event('!bug', uid='90001', mid='502'))
        self.dispatch_workers()
        self.assertIn('未处理的问题反馈（1 条', self.send.call_args.args[0])

    def test_digest_goes_to_the_admin_group(self):
        self.ledger.manual('奥法修复台物品模型缺 display', '2001', 'Alice', self.gid)
        groups = []
        self.send.side_effect = lambda text, api=None: groups.append(bridge.REPLY_GROUP.get()) or True
        self.assertFalse(bridge._bug_digest_once(at(2026, 10, 26, 20, 0)))
        self.assertTrue(bridge._bug_digest_once(at(2026, 10, 26, 21, 1)))
        self.assertEqual([2105], groups)
        self.assertTrue(self.ledger.get(1)['notified'])
        self.assertFalse(bridge._bug_digest_once(at(2026, 10, 26, 21, 30)))


if __name__ == '__main__':
    unittest.main()
