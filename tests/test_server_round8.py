"""Round 8: server information tools and the smart `!cmd`.

Offline only: temp trees, the remote scripts run by this interpreter instead of
over SSH, and mocked model, RCON and QQ calls. Audit and result logs go to temp.
"""
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_fusion_integration import FusionCase, bridge, s
from tests import test_yachiyo_runtime as runtime_tests
from tests.test_remote_collectors import run_local
import command_catalog as cc
import rcon_ops
import remote_server_reader as reader
import sanae_local_ops
import server_insight as si

REAL_IS_KNOWN = rcon_ops.is_known_command
REAL_EXECUTE_TOOL = s.execute_tool
NOW = 1792900000.0
LEGACY = {'id': 'legacy', 'prefix': '[怀旧]', 'name': 'Legacy'}
CATALOG_HELP = '\n'.join([
    '/list', '/give <targets> <item> [<count>]', '/weather (clear|rain|thunder)', '/say <message>',
    '/data (get|merge|modify|remove)', '/tp -> teleport', '/kick <targets> [<reason>]'])


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


class TempCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ops = self.root / 'ops' / 'legacy'
        self.ops.mkdir(parents=True)
        self.mirror = self.root / 'mirror'
        self.mirror.mkdir()


class SnbtAndQuestTests(TempCase):
    def test_snbt_reads_typed_numbers_arrays_comments_and_escapes(self):
        value = si.parse_snbt('{ id: "AB12", count: 64L, weight: 1.5d, flag: true, // note\n'
                              ' ints: [I; 1, 2, 3], nested: { name: \'it\\\'s\', text: "a\\nb" },'
                              ' list: ["x" "y"] }')
        self.assertEqual({'id': 'AB12', 'count': 64, 'weight': 1.5, 'flag': True, 'ints': [1, 2, 3],
                          'nested': {'name': "it's", 'text': 'a\nb'}, 'list': ['x', 'y']}, value)
        with self.assertRaises(ValueError):
            si.parse_snbt('{ open: "x"')

    def quest_book(self):
        base = self.mirror / 'config' / 'ftbquests' / 'quests'
        (base / 'chapters').mkdir(parents=True)
        (base / 'lang').mkdir()
        (base / 'chapters' / 'thaumcraft.snbt').write_text(
            '{\n id: "C1"\n filename: "thaumcraft"\n quests: [\n'
            '  { id: "Q1", tasks: [{ id: "T1", type: "item", item: { id: "thaumcraft:thaumonomicon", count: 1 } }],\n'
            '    rewards: [{ id: "R1", type: "xp", xp: 50 }] }\n'
            '  { id: "Q2", dependencies: ["Q1"], tasks: [{ id: "T2", type: "checkmark" }], rewards: [] }\n'
            '  { id: "Q3", tasks: [{ id: "T3", type: "stat", stat: "minecraft:total_world_time", value: 20 }\n'
            '    { id: "T4", type: "kill", entity: "minecraft:zombie", value: 5L }\n'
            '    { id: "T5", type: "observation", to_observe: "thaumcraft:aura_node" }] }\n'
            ' ]\n}\n', encoding='utf-8')
        (base / 'lang' / 'zh_cn.snbt').write_text(
            '{\n chapter.C1.title: "&6神秘时代"\n quest.Q1.title: "&l初识神秘"\n'
            ' quest.Q1.quest_subtitle: "拿到&#ff69b4神秘学典籍"\n quest.Q1.quest_desc: ["第一行", "", "第二行"]\n'
            ' quest.Q2.title: "§a继续研究"\n}\n', encoding='utf-8')
        write_json(self.ops / 'recipe-index.json', {'names': {
            'thaumcraft:thaumonomicon': {'zh': '神秘学典籍', 'en': 'Thaumonomicon'}}})

    def test_quest_lookup_uses_translations_item_names_and_strips_colour_codes(self):
        self.quest_book()
        text = si.quest_lookup(self.mirror, self.ops, '初识')
        self.assertIn('章节「神秘时代」· 任务「初识神秘」：拿到神秘学典籍', text)
        self.assertIn('说明：第一行 第二行', text)
        self.assertIn('要求：神秘学典籍（thaumcraft:thaumonomicon）', text)
        self.assertIn('奖励：经验 50', text)
        self.assertIn('没有前置任务', text)
        self.assertIn('章节「神秘时代」没有前置、可以先做的任务：初识神秘、统计 minecraft:total_world_time 达到 20（进入世界即可完成）', si.quest_lookup(self.mirror, self.ops, '神秘时代'))
        self.assertNotIn('&', text)
        follow = si.quest_lookup(self.mirror, self.ops, '继续研究')
        self.assertIn('要求：勾选完成', follow)
        self.assertIn('前置任务 1 个', follow)
        self.assertIn('任务书共 1 章、3 个任务', si.quest_lookup(self.mirror, self.ops, ''))
        other = si.quest_lookup(self.mirror, self.ops, '统计')
        self.assertIn('统计 minecraft:total_world_time 达到 20（进入世界即可完成）', other)
        self.assertIn('击杀 minecraft:zombie ×5', other)
        self.assertIn('观察 thaumcraft:aura_node', other)
        self.assertIn('没有找到', si.quest_lookup(self.mirror, self.ops, '不存在的东西'))
        self.assertIn('还没有从服务器同步', si.quest_lookup(self.root / 'empty', self.ops, 'x'))


class TelemetryViewTests(TempCase):
    def test_live_status_and_trends(self):
        write_json(self.ops / 'runtime.json', {'timestamp': NOW - 40, 'players': 9, 'max_players': 20,
                                                'tps': 12.564, 'mspt': 79.59})
        self.assertEqual('在线 9/20，TPS 12.6，MSPT 80（40 秒前采样）', si.live_status(self.ops, now=NOW))
        self.assertEqual('', si.live_status(self.ops, now=NOW + 400))
        self.assertEqual('有点卡', si.live_verdict(self.ops, now=NOW))
        self.assertEqual('', si.live_verdict(self.ops, now=NOW + 400))
        for tps, mspt, verdict in ((20, 43, '流畅'), (19.9, 50, '有点卡'), (8, 120, '很卡')):
            write_json(self.ops / 'runtime.json', {'timestamp': NOW, 'tps': tps, 'mspt': mspt, 'tick_parser': 2})
            self.assertEqual(verdict, si.live_verdict(self.ops, now=NOW))
        rows = [{'timestamp': NOW - 3000 + i * 60, 'tick_parser': 2, 'tps': tps, 'mspt': mspt, 'players': p}
                for i, (tps, mspt, p) in enumerate([(20, 30, 2), (12.5, 80, 9), (19.8, 45, 5)])]
        rows.append({'timestamp': NOW - 90000, 'tick_parser': 2, 'tps': 1, 'mspt': 999, 'players': 50})
        (self.ops / 'perf-samples.jsonl').write_text(
            '\n'.join(json.dumps(r) for r in rows) + '\nnot json\n', encoding='utf-8')
        text = si.trends(self.ops, '1h', now=NOW)
        self.assertIn('采样 3 个', text)
        self.assertIn('最低 12.5', text)
        self.assertIn('卡顿样本（TPS<18 或 MSPT≥50）1 个', text)
        self.assertIn('峰值 9 人', text)
        self.assertNotIn('50 人', text)
        with self.assertRaises(ValueError):
            si.trends(self.ops, '3d', now=NOW)

    def test_events_filter_by_kind_and_player(self):
        rows = [
            {'timestamp': NOW - 600, 'type': 'join', 'text': '[怀旧] Alice 加入了游戏', 'details': {'player': 'Alice'}},
            {'timestamp': NOW - 500, 'type': 'minecraft_event', 'text': '[怀旧] Alice 完成进度 [石器时代]'},
            {'timestamp': NOW - 400, 'type': 'minecraft_event', 'text': '[怀旧] Bob 被 僵尸 击杀'},
            {'timestamp': NOW - 300, 'type': 'crash', 'text': '[怀旧] 服务器崩溃：Ticking entity'},
            {'timestamp': NOW - 200, 'type': 'chat', 'text': '[怀旧] 普通聊天不算事件'},
        ]
        (self.ops / 'events.jsonl').write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows),
                                               encoding='utf-8')
        text = si.events(self.ops, '1h', now=NOW)
        self.assertIn('共 4 条，其中进服 1 次（1 人）', text)
        self.assertNotIn('普通聊天', text)
        self.assertNotIn('[怀旧]', text)
        deaths = si.events(self.ops, '1h', kind='death', now=NOW)
        self.assertIn('Bob 被 僵尸 击杀', deaths)
        self.assertNotIn('Alice', deaths)
        self.assertIn('共 2 条', si.events(self.ops, '1h', player='alice', now=NOW))
        with self.assertRaises(ValueError):
            si.events(self.ops, '1h', kind='chat', now=NOW)

    def test_player_stats_lookup_ranking_and_ambiguity(self):
        self.assertIn('还没有从服务器同步', si.player_stats(self.ops, player='Alice'))
        write_json(self.ops / 'player-stats.json', {'generated_at': NOW, 'players': [
            {'name': 'Alice', 'play_hours': 120.5, 'deaths': 30, 'mined': 50000,
             'top_mined': [['minecraft:stone', 30000]], 'last_seen': NOW - 3600},
            {'name': 'AliceMage', 'play_hours': 3.0, 'deaths': 70},
            {'name': 'Bob', 'play_hours': 40.0, 'deaths': 5}]})
        write_json(self.ops / 'recipe-index.json', {'names': {'minecraft:stone': {'zh': '石头'}}})
        ranking = si.player_stats(self.ops, top='deaths')
        self.assertIn('1. AliceMage：70\n2. Alice：30\n3. Bob：5', ranking)
        one = si.player_stats(self.ops, player='alice')
        self.assertIn('玩家 Alice 的统计', one)
        self.assertIn('游戏时长（小时）：120.5', one)
        self.assertIn('挖得最多：石头（minecraft:stone） 30000', one)
        self.assertIn('有多个玩家名匹配：Alice、AliceMage', si.player_stats(self.ops, player='ali'))
        self.assertIn('没有找到', si.player_stats(self.ops, player='nobody'))
        with self.assertRaises(ValueError):
            si.player_stats(self.ops, top='diamonds')


class ConfigLookupTests(TempCase):
    def setUp(self):
        super().setUp()
        (self.mirror / 'config').mkdir()
        (self.mirror / 'config' / 'a.toml').write_text(
            'enabled = true\nrcon.password = "hunter2"\napi_key: abc123\n', encoding='utf-8')
        (self.mirror / 'mods').mkdir()
        (self.mirror / 'mods' / 'x.toml').write_text('x', encoding='utf-8')
        (self.mirror / 'server.properties').write_text('rcon.password=hunter2\nmotd=hi\n', encoding='utf-8')

    def test_reads_are_confined_to_config_roots_and_redacted(self):
        text = si.read_config(self.mirror, 'config/a.toml')
        self.assertIn('2: rcon.password = "<已隐藏>"', text)
        self.assertNotIn('hunter2', text)
        self.assertNotIn('abc123', text)
        self.assertIn('a.toml', si.read_config(self.mirror, 'config'))
        self.assertNotIn('hunter2', si.read_config(self.mirror, 'server.properties'))
        for path in ('../outside.txt', 'mods/x.toml', 'config/../mods/x.toml', 'world/level.dat'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                si.read_config(self.mirror, path)
        hits = si.search_config(self.mirror, 'password')
        self.assertIn('config/a.toml:2:', hits)
        self.assertIn('server.properties:1:', hits)
        self.assertNotIn('hunter2', hits)
        self.assertIn('不在服务器目录内', si.search_config(self.mirror, 'xx', scope='../'))
        with self.assertRaises(ValueError):
            si.search_config(self.mirror, 'x')

    def test_members_get_stats_but_not_configs(self):
        write_json(self.ops / 'player-stats.json', {'generated_at': NOW, 'players': [{'name': 'Bob', 'deaths': 5}]})
        server = dict(LEGACY, server_dir=str(self.mirror))
        with mock.patch.dict(os.environ, {'OPS_STATE_BASE': str(self.root / 'ops')}), \
                mock.patch.object(sanae_local_ops.server_registry, 'resolve_server', return_value=server):
            denied = sanae_local_ops.execute_insight('read_server_config', {'path': 'config/a.toml'},
                                                     {'id': 'legacy'}, False)
            allowed = sanae_local_ops.execute_insight('read_server_config', {'path': 'config/a.toml'},
                                                      {'id': 'legacy'}, True)
            stats = sanae_local_ops.execute_insight('player_stats', {'top': 'deaths'}, {'id': 'legacy'}, False)
        self.assertTrue(denied.startswith('拒绝'))
        self.assertIn('<已隐藏>', allowed)
        self.assertTrue(stats.startswith('[怀旧] 死亡次数排行'))
        with mock.patch.object(sanae_local_ops.server_registry, 'resolve_server', return_value=None), \
                self.assertRaises(ValueError):
            sanae_local_ops.execute_insight('player_stats', {'top': 'deaths'}, {'id': 'gone'}, True)

    def test_model_tools_route_to_insight_and_members_lack_config_tools(self):
        with mock.patch.object(s, 'execute_insight', return_value='OK') as insight:
            self.assertEqual('OK', REAL_EXECUTE_TOOL('server_trends', {'window': '6h'}, False,
                                                     selected_server={'id': 'legacy'}))
        insight.assert_called_once_with('server_trends', {'window': '6h'}, {'id': 'legacy'}, False)
        member = {t['function']['name'] for t in s.TOOLS_MEMBER}
        admin = {t['function']['name'] for t in s.TOOLS_ADMIN}
        self.assertTrue({'server_trends', 'server_events', 'player_stats', 'quest_lookup'} <= member)
        self.assertFalse({'search_server_config', 'read_server_config', 'bluemap_shot'} & member)
        self.assertTrue({'search_server_config', 'read_server_config'} <= admin)
        self.assertNotIn('bluemap_shot', admin)


class RemoteServerReaderTests(TempCase):
    def setUp(self):
        super().setUp()
        self.server = {'remote_ssh_host': 'remote-test', 'remote_os': 'posix',
                       'remote_server_dir': str(self.mirror), 'remote_python': 'python3'}

    def test_player_stats_are_summarised_with_names_from_usercache(self):
        uuid = '0f0e0d0c-0000-4000-8000-000000000001'
        write_json(self.mirror / 'usercache.json', [{'uuid': uuid, 'name': 'Alice'}])
        write_json(self.mirror / 'world' / 'stats' / f'{uuid}.json', {'stats': {
            'minecraft:custom': {'minecraft:play_time': 20 * 3600 * 2, 'minecraft:deaths': 3,
                                 'minecraft:walk_one_cm': 200000, 'minecraft:fly_one_cm': 50000,
                                 'minecraft:leave_game': 4, 'minecraft:jump': 10},
            'minecraft:mined': {'minecraft:stone': 100, 'minecraft:dirt': 5},
            'minecraft:killed': {'minecraft:zombie': 7}}})
        (self.mirror / 'world' / 'stats' / 'broken.json').write_text('{', encoding='utf-8')
        data = reader.player_stats(self.server, runner=run_local)
        self.assertEqual(1, len(data['players']))
        row = data['players'][0]
        self.assertEqual(('Alice', 2.0, 3, 105, 2.5, 4, 10),
                         (row['name'], row['play_hours'], row['deaths'], row['mined'],
                          row['distance_km'], row['sessions'], row['jumps']))
        self.assertEqual([['minecraft:stone', 100], ['minecraft:dirt', 5]], row['top_mined'])
        self.assertEqual([['minecraft:zombie', 7]], row['top_killed'])

    def test_log_tail_hides_secrets_and_the_last_ip_octet(self):
        (self.mirror / 'logs').mkdir()
        (self.mirror / 'logs' / 'latest.log').write_text(
            'old\n[12:00] Alice[/192.0.2.16:5555] logged in\npassword=hunter2 here\nlast\n', encoding='utf-8')
        self.assertEqual(['[12:00] Alice[/192.0.2.*:5555] logged in', 'password=<redacted> here', 'last'],
                         reader.tail_log(self.server, 3, runner=run_local))

    def test_crash_report_reads_newest_named_or_none(self):
        folder = self.mirror / 'crash-reports'
        folder.mkdir()
        (folder / 'crash-a.txt').write_text('---- A ----\nDescription: old\n', encoding='utf-8')
        os.utime(folder / 'crash-a.txt', (time.time() - 600, time.time() - 600))
        (folder / 'crash-b.txt').write_text('---- B ----\nDescription: new\n', encoding='utf-8')
        newest = reader.crash_report(self.server, runner=run_local)
        self.assertEqual('crash-b.txt', newest['name'])
        self.assertEqual(['---- B ----', 'Description: new'], newest['lines'])
        self.assertEqual(['crash-a.txt', 'crash-b.txt'], newest['names'])
        self.assertEqual('crash-a.txt', reader.crash_report(self.server, 'crash-a.txt', runner=run_local)['name'])
        self.assertEqual('', reader.crash_report(self.server, 'missing.txt', runner=run_local)['name'])
        with self.assertRaises(ValueError):
            reader.crash_report(self.server, '../secret.txt')


class RiskClassTests(unittest.TestCase):
    def test_classes(self):
        cases = {
            'list': 'read', 'spark health': 'read', 'data get entity Alice Pos': 'read',
            '/gamerule keepInventory': 'read', 'locate structure minecraft:village_plains': 'read',
            'weather clear': 'low', 'time set day': 'low', 'say 大家好': 'low',
            'give Alice minecraft:diamond 64': 'confirm', 'tp Alice 0 64 0': 'confirm', 'op Alice': 'confirm',
            'gamerule keepInventory true': 'confirm', 'execute as @a run kill @s': 'confirm', 'stop': 'confirm',
            'data get entity Alice Pos; stop': 'confirm', '': 'confirm', 'weather clear 999999999': 'confirm',
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(expected, cc.classify(command))

    def test_direct_console_run_is_audited_and_failures_are_labelled(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(rcon_ops, 'AUDIT_PATH', Path(temp) / 'audit.jsonl'):
            done = rcon_ops.run_console_direct('weather clear', 'admin',
                                               query_fn=lambda _c: 'Set the weather to clear', risk='low')

            def refused(_command):
                raise ConnectionRefusedError('refused 10.0.0.1')
            failed = rcon_ops.run_console_direct('list', 'admin', query_fn=refused)
            entries = [json.loads(x) for x in (Path(temp) / 'audit.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual('[控制台返回] weather clear\nSet the weather to clear', done)
        self.assertEqual('[控制台] 执行失败：连接被拒绝', failed)
        self.assertEqual(['cmd_direct', 'cmd_direct'], [e['event'] for e in entries])
        self.assertTrue(entries[0]['result'].startswith('low: '))
        self.assertEqual('failed: ConnectionRefusedError', entries[1]['result'])
        self.assertNotIn('10.0.0.1', json.dumps(entries))


def tool_call(name, arguments, ident='fixture-call'):
    return {'choices': [{'message': {'content': '', 'tool_calls': [{
        'id': ident, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]}}]}


def text_reply(content):
    return {'choices': [{'message': {'content': content}}]}


class ConsoleAgentTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def run_console(self, text, responses, tool_result):
        sent = []
        replies = iter(responses)

        def model(payload, **kwargs):
            sent.append(copy.deepcopy(payload))
            return next(replies)
        with mock.patch.object(s, '_text_post', side_effect=model), \
                mock.patch.object(s, 'execute_tool', return_value=tool_result) as execute:
            answer, _, used = s.run_agent(text, 'fixture-admin', True, 'fixture-group',
                                          selected_server={'id': 'legacy'}, explicit_server=True,
                                          console_request=True)
        return answer, used, sent, execute

    def test_profile_needs_an_admin_and_a_target(self):
        on = s.request_profile('看看 Alice 在哪', selected_server=LEGACY, privileged=True,
                               explicit_server=True, console_request=True)
        self.assertTrue(on['console_request'])
        self.assertFalse(on['read_only_tool_required'] or on['operation_tool_required'] or on['explanation_only'])
        self.assertEqual('tool', on['tier'])
        self.assertFalse(s.request_profile('看看 Alice 在哪', selected_server=LEGACY, privileged=False,
                                           console_request=True)['console_request'])
        self.assertFalse(s.request_profile('看看 Alice 在哪', privileged=True,
                                           console_request=True)['console_request'])

    def test_console_turn_uses_console_tools_and_forces_only_the_first_call(self):
        answer, used, sent, execute = self.run_console(
            '看看 Alice 在哪',
            [tool_call('run_rcon', {'command': 'data get entity Alice Pos'}), text_reply('Alice 在 (1, 64, 2)。')],
            'Alice has the following entity data: [1.0d, 64.0d, 2.0d]')
        self.assertTrue(used)
        self.assertEqual('Alice 在 (1, 64, 2)。', answer)
        self.assertEqual(2, len(sent))
        self.assertEqual('required', sent[0]['tool_choice'])
        self.assertNotIn('tool_choice', sent[1])
        for payload in sent:
            self.assertEqual(s.CONSOLE_TOOLS, payload['tools'])
            self.assertEqual({'type': 'disabled'}, payload['thinking'])
            self.assertIn('!cmd 管理员控制台', payload['messages'][0]['content'])
        kwargs = execute.call_args.kwargs
        self.assertTrue(kwargs['console_request'])
        self.assertTrue(kwargs['allow_mutation'])
        self.assertEqual('看看 Alice 在哪', kwargs['current_user_text'])

    def test_ask_back_after_a_search_is_kept_as_her_answer(self):
        question = '要给谁、给多少？补全后再发一次，比如 !cmd give Alice minecraft:diamond 64。'
        answer, _, _, _ = self.run_console(
            '给点钻石', [tool_call('search_server_commands', {'query': 'give'}), text_reply(question)],
            '本服已注册命令匹配：\n/give <targets> <item> [<count>]')
        self.assertEqual(question, answer)

    def test_a_request_ends_the_turn_with_the_code_text(self):
        code = '八千代复核：给在线的 Alice 64 个钻石。\n[高危确认] 即将执行 RCON 命令。\n请在 90 秒内发送：!确认 ABCD'
        answer, _, sent, _ = self.run_console(
            '给 Alice 64 个钻石',
            [tool_call('request_console_command', {'command': 'give Alice minecraft:diamond 64'})], code)
        self.assertEqual(code, answer)
        self.assertEqual(1, len(sent))

    def test_request_tool_in_console_mode_skips_the_chat_intent_check_and_keeps_the_note(self):
        dispatch_text = ('[高危确认] 即将执行 RCON 命令：give Alice minecraft:diamond 64。\n'
                         '请在 90 秒内发送：!确认 ABCD\n如需取消：!取消确认')
        args = {'command': 'give Alice minecraft:diamond 64',
                'note': '给在线的 Alice 64 个钻石，[CQ:at,qq=1]\n给出去就收不回。'}
        with mock.patch.object(s._command_catalog, 'validate',
                               return_value=(True, '/give <targets> <item> [<count>]', '')), \
                mock.patch.object(s._rcon_ops, 'dispatch', return_value=dispatch_text) as dispatch:
            console = REAL_EXECUTE_TOOL('request_console_command', dict(args), True,
                                        selected_server={'id': 'legacy'}, explicit_server=True,
                                        current_user_text='看看能不能给 Alice 钻石', console_request=True)
            chat = REAL_EXECUTE_TOOL('request_console_command', dict(args), True,
                                     selected_server={'id': 'legacy'}, explicit_server=True,
                                     current_user_text='看看能不能给 Alice 钻石')
        self.assertTrue(console.startswith('八千代复核：给在线的 Alice 64 个钻石， 给出去就收不回。\n[高危确认]'))
        self.assertNotIn('[CQ:', console)
        self.assertIn('!确认 ABCD', console)
        self.assertIn('本服注册语法：/give', console)
        self.assertTrue(chat.startswith('拒绝'))
        dispatch.assert_called_once()

    def test_run_rcon_in_console_mode_runs_read_low_and_points_the_rest_to_a_code(self):
        with mock.patch.object(s, 'query_server', return_value='') as query:
            refused = REAL_EXECUTE_TOOL('run_rcon', {'command': 'give Alice minecraft:diamond 1'}, True,
                                        allow_mutation=True, selected_server={'id': 'legacy'},
                                        console_request=True)
            done = REAL_EXECUTE_TOOL('run_rcon', {'command': 'weather clear'}, True, allow_mutation=True,
                                     selected_server={'id': 'legacy'}, console_request=True)
        self.assertIn('request_console_command', refused)
        self.assertEqual('(命令已执行，无返回内容)', done)
        query.assert_called_once_with({'id': 'legacy'}, 'weather clear')

    def test_review_note_is_one_clean_line_and_empty_on_failure(self):
        with mock.patch.object(s, '_text_post', return_value=text_reply('会给 Alice 64 个钻石。\n他当前在线。')) as post:
            note = s.console_review_note('give Alice minecraft:diamond 64', '/give <targets> <item> [<count>]',
                                         'There are 1 of a max of 20 players online: Alice')
        self.assertEqual('会给 Alice 64 个钻石。 他当前在线。', note)
        payload = post.call_args.args[0]
        self.assertNotIn('tools', payload)
        self.assertIn('give Alice minecraft:diamond 64', payload['messages'][1]['content'])
        with mock.patch.object(s, '_text_post', side_effect=ValueError('bad')):
            self.assertEqual('', s.console_review_note('op Alice'))


class ConsoleBridgeTests(FusionCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        catalog = self.root / 'catalog.json'
        cc._save({'schema': 1, 'servers': {'legacy': {'count': 7, 'commands': cc.parse_help(CATALOG_HELP)}}},
                 catalog)
        self.stack.enter_context(mock.patch.object(bridge._command_catalog, 'CATALOG_PATH', catalog))
        self.results = self.stack.enter_context(mock.patch.object(bridge._rcon_ops, 'record_command_result'))
        self.gid = bridge.SOCIAL_LITE_GROUP

    def test_plan_routes_exact_commands_free_text_and_a_missing_catalog(self):
        plan = lambda rest, target=LEGACY: bridge._console_plan(target, rest)
        self.assertEqual(('direct', 'list', 'read'), plan('list'))
        self.assertEqual(('direct', 'list', 'read'), plan('</list>'))
        self.assertEqual(('direct', 'weather clear', 'low'), plan('weather clear'))
        self.assertEqual(('direct', 'say 大家好', 'low'), plan('say 大家好'))
        self.assertEqual(('confirm', 'give Alice minecraft:diamond 64', '/give <targets> <item> [<count>]'),
                         plan('give Alice minecraft:diamond 64'))
        self.assertEqual('confirm', plan('kick Alice 挂机太久')[0])
        self.assertEqual(('ai', 'give Alice 钻石 64', ''), plan('give Alice 钻石 64'))
        self.assertEqual(('ai', '把天气改成晴天', ''), plan('把天气改成晴天'))
        self.assertEqual(('ai', 'giv Alice diamond', ''), plan('giv Alice diamond'))
        self.assertEqual('usage', plan('')[0])
        self.assertEqual(('legacy', 'list', ''), plan('list', {'id': 'unscanned', 'prefix': '[新]'}))

    def route(self, text, uid):
        self.stack.enter_context(mock.patch.object(bridge._rcon_ops, 'is_known_command', side_effect=REAL_IS_KNOWN))
        self.stack.enter_context(mock.patch.object(bridge, '_command_target',
                                                   side_effect=lambda raw, *_: (LEGACY, raw, True, '')))
        self.stack.enter_context(mock.patch.object(bridge, 'OWNERS', {'90001'}))
        self.handler._process_event(self.event(text, uid=uid))
        return [c for c in self.worker.call_args_list + self.ai_worker.call_args_list
                if c.args and c.args[0] == self.handler._console_command_thread]

    def test_admin_cmd_goes_to_the_console_worker(self):
        calls = self.route('!cmd 把天气改成晴天', '90001')
        self.assertEqual(1, len(calls))
        self.assertIn(calls[0], self.ai_worker.call_args_list)
        _event, plan, target, uid, gid = calls[0].kwargs['args']
        self.assertEqual((('ai', '把天气改成晴天', ''), LEGACY, '90001', self.gid), (plan, target, uid, gid))

    def test_exact_read_command_uses_the_plain_worker(self):
        calls = self.route('!cmd list', '90001')
        self.assertEqual(1, len(calls))
        self.assertIn(calls[0], self.worker.call_args_list)
        self.assertEqual(('direct', 'list', 'read'), calls[0].kwargs['args'][1])

    def test_member_cmd_keeps_the_old_refusal(self):
        self.assertEqual([], self.route('!cmd list', '2001'))
        self.dispatch_workers()
        self.assertIn('拒绝', self.send.call_args.args[0])

    def test_direct_runs_and_reports_with_the_server_prefix(self):
        with mock.patch.object(bridge._rcon_ops, 'run_console_direct',
                               return_value='[控制台返回] list\nThere are 3 players') as direct:
            self.handler._console_command_thread(self.event('!cmd list', uid='90001'),
                                                 ('direct', 'list', 'read'), LEGACY, '90001', self.gid)
        self.assertEqual(('list', '90001'), direct.call_args.args)
        self.assertEqual('read', direct.call_args.kwargs['risk'])
        self.assertIs(LEGACY, direct.call_args.kwargs['server'])
        self.assertEqual('[怀旧] [控制台返回] list\nThere are 3 players', self.send.call_args.args[0])
        self.results.assert_called_once()

    def test_confirm_is_reviewed_before_the_code_is_created(self):
        order = []
        note = mock.Mock(side_effect=lambda *a: order.append('note') or '会给 Alice 64 个钻石，他在线。')
        dispatch = mock.Mock(side_effect=lambda *a, **k: order.append('code') or
                             '[高危确认] 即将执行 RCON 命令。\n请在 90 秒内发送：!确认 ABCD')
        with mock.patch.object(bridge, 'query_server', return_value='There are 1 players online: Alice'), \
                mock.patch.object(bridge._sanae_ai, 'console_review_note', note), \
                mock.patch.object(bridge._rcon_ops, 'dispatch', dispatch), \
                mock.patch.object(bridge, 'list_servers', return_value=[LEGACY, {'id': 'other'}]):
            self.handler._console_command_thread(
                self.event('!cmd give', uid='90001'),
                ('confirm', 'give Alice minecraft:diamond 64', '/give <targets> <item> [<count>]'),
                LEGACY, '90001', self.gid)
        self.assertEqual(['note', 'code'], order)
        self.assertEqual(('give Alice minecraft:diamond 64', '/give <targets> <item> [<count>]',
                          'There are 1 players online: Alice'), note.call_args.args)
        self.assertEqual('cmd give Alice minecraft:diamond 64', dispatch.call_args.args[0])
        text = self.send.call_args.args[0]
        self.assertTrue(text.startswith('[怀旧] 八千代复核：会给 Alice 64 个钻石，他在线。\n[高危确认]'))
        self.assertIn('发送：[怀旧] !确认 ABCD', text)

    def test_free_text_goes_to_her_with_console_mode(self):
        with mock.patch.object(bridge._sanae_ai, 'sanae_reply', return_value='要给谁？') as model, \
                mock.patch.object(bridge, '_send_ai_text', return_value=True) as send_ai:
            self.handler._console_command_thread(self.event('!cmd 给点钻石', uid='90001'),
                                                 ('ai', '给点钻石', ''), LEGACY, '90001', self.gid)
        self.assertEqual(('给点钻石', '90001', '测试成员'), model.call_args.args)
        kwargs = model.call_args.kwargs
        self.assertEqual((True, True, True, True, LEGACY),
                         (kwargs['console_request'], kwargs['privileged'], kwargs['force'],
                          kwargs['explicit_server'], kwargs['selected_server']))
        self.assertEqual('[CQ:at,qq=90001] [怀旧] 要给谁？', send_ai.call_args.args[0])
        with mock.patch.object(bridge._sanae_ai, 'sanae_reply', return_value=None), \
                mock.patch.object(bridge, '_send_ai_text', return_value=True) as send_ai:
            self.handler._console_command_thread(self.event('!cmd ？', uid='90001'),
                                                 ('ai', '？', ''), LEGACY, '90001', self.gid)
        self.assertIn('再发一次 !cmd', send_ai.call_args.args[0])


class SocialStatusTests(FusionCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        write_json(self.root / 'legacy' / 'runtime.json', {'timestamp': time.time() - 30, 'players': 9,
                                                           'max_players': 20, 'tps': 19.6, 'mspt': 41})
        self.stack.enter_context(mock.patch.object(bridge, 'OPS_STATE_BASE', self.root))
        self.default = self.stack.enter_context(mock.patch.object(bridge, 'get_group_default',
                                                                  return_value=(LEGACY, '')))

    def test_status_line_only_for_server_talk(self):
        line = bridge._social_server_status(bridge.SOCIAL_LITE_GROUP, '今晚服务器卡不卡')
        self.assertTrue(line.startswith('〖服务器现状〗[怀旧] 在线 9/20，TPS 19.6，MSPT 41'))
        self.assertIn('按 TPS/MSPT 判断：流畅', line)
        self.assertEqual('', bridge._social_server_status(bridge.SOCIAL_LITE_GROUP, '今天吃什么'))
        self.default.return_value = (None, '')
        with mock.patch.object(bridge, 'list_servers', return_value=[LEGACY, {'id': 'other'}]):
            self.assertEqual('', bridge._social_server_status(bridge.SOCIAL_LITE_GROUP, '服务器人多吗'))

    def test_ambient_reply_carries_the_status_line(self):
        self.stack.enter_context(mock.patch.object(bridge.FEEDBACK_STORE, 'style_override', return_value=''))
        with mock.patch.object(s, 'sanae_reply', return_value=None) as model:
            self.handler._social_reply_thread(self.event('现在服务器几个人啊'), '合成上下文')
        self.assertIn('〖服务器现状〗[怀旧] 在线 9/20', model.call_args.kwargs['context_reference'])
        with mock.patch.object(s, 'sanae_reply', return_value=None) as model:
            self.handler._social_reply_thread(self.event('晚饭吃面'), '合成上下文')
        self.assertNotIn('〖服务器现状〗', model.call_args.kwargs['context_reference'])


if __name__ == '__main__':
    unittest.main()
