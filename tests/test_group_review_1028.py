"""Round 31: fixes from the 10-27 evening and 10-28 morning chat review. Offline."""
import contextlib
import copy
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
from tests.test_server_round8 import text_reply, tool_call
import lookup_tools
import qq_chat_reader
import recipe_index
import server_insight

s, bridge = runtime_tests.s, runtime_tests.bridge


class ChatReaderTests(unittest.TestCase):
    """10-28: a 549-message forward used the whole text budget and the 80 newer messages were dropped."""

    def rows(self):
        rows = [{'message_id': i, 'message_seq': 1000 + i, 'time': 1793100000 + i, 'group_id': 1,
                 'user_id': 2, 'sender': {'card': f'群友{i}'},
                 'message': [{'type': 'text', 'data': {'text': f'消息{i}'}}]} for i in range(30)]
        rows[5]['message'] = [{'type': 'forward', 'data': {'id': 'big'}}]
        return rows

    def test_the_newest_messages_survive_a_huge_forward(self):
        rows = self.rows()

        class Reader(qq_chat_reader._Reader):
            def __init__(self, api, gid):
                self.api, self.group_id, self.deadline = api, gid, time.monotonic() + 30
                self.calls = 0
                self.forward_ids, self.expanded, self.messages_seen = {'big'}, set(), set()
                self.warnings, self.remaining_chars, self.forward_cap = [], 4000, 0
        with mock.patch.object(qq_chat_reader, '_Reader', Reader), \
                mock.patch.object(qq_chat_reader.chat_recall, 'fetch_rows', return_value=rows), \
                mock.patch.object(qq_chat_reader.chat_recall, 'expand_forward', return_value='转' * 50000):
            data = json.loads(qq_chat_reader.read_group_chat('http://x', 1, count=30))
        texts = [row['text'] for row in data['messages']]
        self.assertEqual('消息29', texts[-1])
        self.assertEqual(30, len(texts))
        big = next(text for text in texts if text.startswith('转'))
        self.assertLessEqual(len(big), qq_chat_reader.HISTORY_FORWARD_CHARS + 40)
        self.assertIn('只列出开头', big)
        self.assertEqual(sorted(row['message_seq'] for row in data['messages']), [row['message_seq'] for row in data['messages']])


class RecipeIndexTests(unittest.TestCase):
    def test_processing_recipes_with_results_lists(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / 'mods').mkdir()
        recipes = {
            'data/create/recipe/mixing/brass_ingot.json': {
                'type': 'create:mixing', 'heat_requirement': 'heated',
                'ingredients': [{'tag': 'c:ingots/copper'}, {'tag': 'c:ingots/zinc'}],
                'results': [{'count': 2, 'id': 'create:brass_ingot'}]},
            'data/create/recipe/crushing/iron_ore.json': {
                'type': 'create:crushing', 'ingredients': [{'item': 'minecraft:iron_ore'}],
                'results': [{'id': 'create:crushed_raw_iron'}, {'id': 'create:experience_nugget', 'chance': 0.75}]},
            'data/create/recipe/mixing/lava.json': {
                'type': 'create:mixing', 'heat_requirement': 'superheated',
                'ingredients': [{'type': 'fluid_stack', 'fluid': 'minecraft:water', 'amount': 250}],
                'results': [{'id': 'minecraft:obsidian'}]},
        }
        with zipfile.ZipFile(root / 'mods' / 'create.jar', 'w') as jar:
            for name, data in recipes.items():
                jar.writestr(name, json.dumps(data))
            jar.writestr('assets/create/lang/zh_cn.json', json.dumps({'item.create.brass_ingot': '黄铜锭',
                                                                     'item.create.experience_nugget': '经验颗粒'}))
        index = recipe_index.build_recipe_index(root, 'legacy', '[怀旧]')
        self.assertEqual(2, index['schema'])
        rows = {row['id']: row for row in index['recipes']}
        brass = rows['create:mixing/brass_ingot']
        self.assertEqual(('create:brass_ingot', 2, 'heated'), (brass['result'], brass['count'], brass['heat']))
        self.assertEqual([['create:experience_nugget', 1, 0.75]], rows['create:crushing/iron_ore']['outputs'])
        self.assertEqual(['minecraft:water 250mB'], rows['create:mixing/lava']['ingredients'])
        self.assertIn('create:experience_nugget', index['names'])
        path = root / 'recipe-index.json'
        recipe_index.atomic_write_json(path, index)
        text = lookup_tools.recipe_lookup(path, '黄铜锭')
        self.assertIn('机械动力搅拌（工作盆 + 动力搅拌器）', text)
        self.assertIn('需要加热：工作盆正下方放烈焰人燃烧室并喂燃料（岩浆、营火不算）', text)
        crushed = lookup_tools.recipe_lookup(path, 'create:crushed_raw_iron')
        self.assertIn('副产物：经验颗粒 ×1（75%）', crushed)

    def test_an_old_index_is_rebuilt(self):
        import ops_telemetry
        source = Path(ops_telemetry.__file__).read_text(encoding='utf-8')
        self.assertIn('current_recipes.get("schema") != INDEX_SCHEMA', source)


class StatsTests(unittest.TestCase):
    def test_longer_and_ascending_rankings(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        rows = [{'name': f'P{i}', 'play_hours': float(i)} for i in range(40)]
        (Path(temp.name) / 'player-stats.json').write_text(json.dumps({'generated_at': 1793100000, 'players': rows}),
                                                            encoding='utf-8')
        top = server_insight.player_stats(temp.name, top='play_hours', limit=30)
        self.assertIn('30. P10：10', top)
        self.assertNotIn('31.', top)
        low = server_insight.player_stats(temp.name, top='play_hours', limit=3, order='asc')
        self.assertIn('排行（从少到多）', low)
        self.assertIn('1. P0：0', low)
        self.assertIn('3. P2：2', low)
        schema = s.PLAYER_STATS['function']['parameters']['properties']
        self.assertEqual(30, schema['limit']['maximum'])
        self.assertEqual(['desc', 'asc'], schema['order']['enum'])
        self.assertIn('earliest', s.ADVANCEMENT_PROGRESS['function']['parameters']['properties']['order']['enum'])


class InterimTests(unittest.TestCase):
    def test_notice_waits_and_is_dropped_when_the_answer_comes_first(self):
        sent = []
        quick = s._DelayedInterim(sent.append, 0.2)
        quick('收到，正在处理，请稍候…')
        quick.cancel()
        time.sleep(0.35)
        self.assertEqual([], sent)
        slow = s._DelayedInterim(sent.append, 0.05)
        slow('收到，正在处理，请稍候…')
        slow('刚才只生成了计划，我继续核对。')
        time.sleep(0.25)
        self.assertEqual(['收到，正在处理，请稍候…'], sent)
        now = s._DelayedInterim(sent.append, 0)
        now('立刻')
        self.assertEqual('立刻', sent[-1])

    def test_the_notice_keeps_the_senders_context(self):
        import contextvars
        group = contextvars.ContextVar('group', default='none')
        seen = []
        token = group.set('1001')
        try:
            notice = s._DelayedInterim(lambda text: seen.append(group.get()), 0.05)
        finally:
            group.reset(token)
        thread = threading.Thread(target=notice, args=('x',))
        thread.start()
        thread.join()
        time.sleep(0.2)
        self.assertEqual(['1001'], seen)

    def test_qq_actions_send_no_notice(self):
        self.assertTrue({'profile_like', 'qq_poke', 'group_mute', 'group_info'} <= s._QUIET_INTERIM_TOOLS)


class FollowupTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def test_a_chat_follow_up_sees_her_summary_once(self):
        # 10-27 21:02 summary for 自称群主的成员, 21:04 "@早苗 你评价一下" -> "这是让我评价空气吗？"
        with mock.patch.object(s.time, 'time', return_value=50000):
            s._set_history('g', 'u', False, [{'role': 'user', 'content': '总结一下'},
                                              {'role': 'assistant', 'content': '【简短总结】二游社区高压的讨论'}])
        with mock.patch.object(s.time, 'time', return_value=50150):
            s.run_agent('你评价一下', 'u', False, 'g', short_reply=True)
            self.assertIn('二游社区高压', json.dumps(self.requests[-1]['messages'], ensure_ascii=False))
            self.assertNotIn('二游社区高压', json.dumps(s._get_chat_history('g', 'u', False), ensure_ascii=False))
        with mock.patch.object(s.time, 'time', return_value=50200):
            s.run_agent('那你觉得呢', 'u', False, 'g', short_reply=True)
        self.assertNotIn('二游社区高压', json.dumps(self.requests[-1]['messages'], ensure_ascii=False))

    def test_old_or_ambient_turns_do_not_see_it(self):
        with mock.patch.object(s.time, 'time', return_value=50000):
            s._set_history('g', 'u', False, [{'role': 'user', 'content': '总结一下'},
                                              {'role': 'assistant', 'content': '旧总结锚'}])
        with mock.patch.object(s.time, 'time', return_value=50000 + s.CHAT_FOLLOWS_SERIOUS_SECONDS + 1):
            s.run_agent('你评价一下', 'u', False, 'g', short_reply=True)
        self.assertNotIn('旧总结锚', json.dumps(self.requests[-1]['messages'], ensure_ascii=False))
        with mock.patch.object(s.time, 'time', return_value=50010):
            s.run_agent('哈哈', 'u', False, 'g', social_only=True)
        self.assertNotIn('旧总结锚', json.dumps(self.requests[-1]['messages'], ensure_ascii=False))


class ImageToolTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def test_a_crafting_screenshot_is_described_then_answered_with_tools(self):
        self.assertTrue(s._has_server_query_intent('早苗为什么我无法合成黄铜'))
        server = {'id': 'legacy', 'prefix': '[怀旧]'}
        sent = []
        replies = iter([tool_call('recipe_lookup', {'query': '黄铜锭'}), text_reply('要给工作盆加热')])

        def model(payload, **kwargs):
            sent.append(copy.deepcopy(payload))
            return next(replies)
        observed = ('【本次用户图片的先行观察】{"images":[{"index":1,"observations":"工作盆里有铜锭和锌锭"}]}', {})
        with mock.patch.object(s, '_text_post', side_effect=model), \
                mock.patch.object(s, 'VISION_PROVIDER', 'deepseek'), mock.patch.object(s, 'TEXT_PROVIDER', 'gemini'), \
                mock.patch.object(s, '_vision_media_url', return_value='data:image/png;base64,AA'), \
                mock.patch.object(s, '_observe_images', return_value=observed) as observe, \
                mock.patch.object(s, '_vision_reply', side_effect=AssertionError('picture-only path')), \
                mock.patch.object(s, 'execute_tool', return_value='黄铜锭 ×2 需要加热') as tool:
            answer, _usage, used = s.run_agent('早苗为什么我无法合成黄铜', 'u', False, 'g',
                                               image_url='https://img/1.png', selected_server=server)
        self.assertEqual('要给工作盆加热', answer)
        observe.assert_called_once()
        self.assertEqual('recipe_lookup', tool.call_args.args[0])
        text = json.dumps(sent[0]['messages'], ensure_ascii=False)
        self.assertIn('工作盆里有铜锭和锌锭', text)
        self.assertNotIn('image_url', text)

    def test_the_description_is_asked_of_a_deepseek_model(self):
        # Replay 10-28: with Gemini as the text model, DS_MODEL is the Gemini model and DeepSeek answered 400.
        sent = []

        def post(url, key, payload, timeout=None):
            sent.append(payload)
            return {'choices': [{'message': {'content': json.dumps(
                {'images': [{'index': 1, 'observations': '一个齿轮'}]}, ensure_ascii=False)}}]}
        parts = [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA'}}]
        with mock.patch.object(s, '_post', side_effect=post), mock.patch.object(s, 'TEXT_PROVIDER', 'gemini'), \
                mock.patch.object(s, 'DS_MODEL', 'gemini-4-argon'):
            text, _ = s._observe_images(parts)
        self.assertEqual(s.DEEPSEEK_VISION_MODEL, sent[0]['model'])
        self.assertIn('一个齿轮', text)
        with mock.patch.object(s, '_post', side_effect=post), mock.patch.object(s, 'TEXT_PROVIDER', 'deepseek'), \
                mock.patch.object(s, 'DS_MODEL', 'deepseek-pro'):
            s._observe_images(parts)
        self.assertEqual('deepseek-pro', sent[1]['model'])

    def test_a_plain_picture_question_keeps_the_picture_reply(self):
        server = {'id': 'legacy', 'prefix': '[怀旧]'}
        with mock.patch.object(s, 'VISION_PROVIDER', 'deepseek'), mock.patch.object(s, 'TEXT_PROVIDER', 'gemini'), \
                mock.patch.object(s, '_vision_reply', return_value=('是一只猫', {})) as vision:
            answer, _usage, _used = s.run_agent('这是什么动物', 'u', False, 'g', image_url='https://img/1.png',
                                                selected_server=server)
        self.assertEqual('是一只猫', answer)
        vision.assert_called_once()


class BridgeTests(unittest.TestCase):
    def test_pending_note(self):
        event = {'group_id': 1001, 'message_id': 77, 'user_id': 2013,
                 'sender': {'card': '　　　　　　'}, 'raw_message': '[CQ:at,qq=1002] 这个破坏用RTS模组挖掘的算不算在内'}
        with mock.patch.object(bridge, '_AI_PENDING', {}):
            bridge._ai_pending_start(event)
            note = bridge._ai_pending_note(1001, exclude_message_id=78, now=time.time() + 54)
            self.assertIn('“这个破坏用RTS模组挖掘的算不算在内”', note)
            self.assertIn('54 秒前', note)
            self.assertIn('不要说没看见', note)
            self.assertEqual('', bridge._ai_pending_note(1001, exclude_message_id=77))
            bridge._ai_pending_done(event)
            self.assertEqual('', bridge._ai_pending_note(1001))

    def test_answers_reach_the_chat_context(self):
        source = Path(bridge.__file__).read_text(encoding='utf-8')
        self.assertIn("_commit_social_outgoing(event, reply.strip())\n                    except Exception as exc:\n"
                      "                        print(f'[social-lite] answer save failed", source)
        self.assertIn('        finally:\n            _ai_pending_done(event)', source)
        self.assertIn("pending_note = _ai_pending_note(gid, event.get('message_id'))", source)


if __name__ == '__main__':
    unittest.main()
