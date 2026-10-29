"""Round 29: QQ group actions (owner decision 10-27: the owner and admins may have her mute members,
five minutes by default; plus pokes, reactions and likes). Offline, with a fake OneBot."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
from tests.test_server_round8 import text_reply, tool_call
import qq_interact

s, bridge = runtime_tests.s, runtime_tests.bridge
GID, BOT = 1001, '1002'
OWNER, ADMIN, LOLI, SLE, OTHER29 = '2001', '2002', '2904', '2003', '2905'
MEMBERS = [{'user_id': int(OWNER), 'card': 'FixtureOwner'},
           {'user_id': int(ADMIN), 'card': 'FixtureAdmin'},
           {'user_id': int(LOLI), 'card': '自称群主的成员'},
           {'user_id': int(SLE), 'card': 'FixtureMember'},
           {'user_id': int(BOT), 'card': 'FixtureBot'}]
ROLES = {OWNER: 'owner', ADMIN: 'admin', LOLI: 'member', SLE: 'member', BOT: 'admin', OTHER29: 'member'}


class FakeActions:
    def __init__(self, roles=None, members=None, messages=None, fail=None):
        self.roles = dict(ROLES if roles is None else roles)
        self.rows = list(MEMBERS if members is None else members)
        self.messages = messages or {}
        self.fail = fail or {}
        self.calls = []

    def member_info(self, gid, uid):
        role = self.roles.get(str(uid))
        if role is None:
            raise RuntimeError('OneBot get_group_member_info failed')
        row = next((r for r in self.rows if str(r['user_id']) == str(uid)), {'user_id': int(uid), 'card': ''})
        return dict(row, role=role)

    def members(self, gid):
        return [dict(r, role=self.roles.get(str(r['user_id']), 'member')) for r in self.rows]

    def mute(self, gid, uid, duration):
        self.calls.append(('mute', gid, str(uid), duration))

    def poke(self, uid, gid):
        self.calls.append(('poke', str(uid), gid))

    def call(self, action, params):
        if action in self.fail:
            raise RuntimeError(self.fail[action])
        self.calls.append((action, params))
        return self.messages.get(params.get('message_id')) if action == 'get_msg' else None


class Base(unittest.TestCase):
    def make(self, **kwargs):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.audit = Path(self.temp.name) / 'audit.jsonl'
        self.now = [1793100000.0]
        self.qq = FakeActions(**kwargs)
        return qq_interact.QQInteract(self.qq, BOT, self.audit, clock=lambda: self.now[0])

    def audit_rows(self):
        return [json.loads(line) for line in self.audit.read_text(encoding='utf-8').splitlines()] if self.audit.exists() else []


class MuteTests(Base):
    def test_the_owners_order_from_10_27_mutes_the_member_for_five_minutes(self):
        qq = self.make()
        result = qq.run('group_mute', {'target': '29'}, GID, OWNER)    # "@早苗 赏29五分钟禁言"
        self.assertEqual('已禁言 自称群主的成员（QQ 2904）5 分钟。', result)
        self.assertEqual([('mute', GID, LOLI, 300)], self.qq.calls)
        row = self.audit_rows()[0]
        self.assertEqual(('mute', OWNER, LOLI, 5), (row['action'], row['requester'], row['target'], row['minutes']))

    def test_only_the_owner_and_admins(self):
        qq = self.make()
        self.assertEqual('拒绝：只有群主和管理员能让我禁言群友。', qq.run('group_mute', {'target': '29'}, GID, SLE))
        self.assertEqual([], self.qq.calls)
        self.assertEqual([], self.audit_rows())
        qq.run('group_mute', {'target': 'FixtureMember'}, GID, ADMIN)
        self.assertEqual([('mute', GID, SLE, 300)], self.qq.calls)

    def test_targets_from_mentions_numbers_and_names(self):
        qq = self.make()
        qq.run('group_mute', {'target': '他'}, GID, OWNER, turn={'mentions': [LOLI]})
        qq.run('group_mute', {'target': SLE, 'minutes': 10}, GID, OWNER)
        qq.run('group_mute', {'target': '@自称群主的成员'}, GID, OWNER)
        self.assertEqual([('mute', GID, LOLI, 300), ('mute', GID, SLE, 600), ('mute', GID, LOLI, 300)], self.qq.calls)

    def test_unclear_targets_are_asked_about(self):
        qq = self.make(members=MEMBERS + [{'user_id': int(OTHER29), 'card': '小二九'}])
        result = qq.run('group_mute', {'target': '29'}, GID, OWNER)
        self.assertIn('“29”对得上好几个人：自称群主的成员（QQ 2904）、小二九（QQ 2905）', result)
        self.assertIn('没找到“路人甲”', qq.run('group_mute', {'target': '路人甲'}, GID, OWNER))
        self.assertIn('你没 @ 人', qq.run('group_mute', {'target': '他'}, GID, OWNER))
        self.assertEqual([], self.qq.calls)

    def test_duration_lift_and_bounds(self):
        qq = self.make()
        self.assertEqual('已解除 自称群主的成员（QQ 2904）的禁言。', qq.run('group_mute', {'target': LOLI, 'minutes': 0}, GID, OWNER))
        qq.run('group_mute', {'target': LOLI, 'minutes': 99999}, GID, OWNER)
        qq.run('group_mute', {'target': LOLI, 'minutes': 'x'}, GID, OWNER)
        self.assertEqual([0, 1440 * 60, 300], [call[3] for call in self.qq.calls])
        self.assertEqual('unmute', self.audit_rows()[0]['action'])

    def test_who_cannot_be_muted(self):
        qq = self.make()
        self.assertEqual('FixtureAdmin 是群主或管理员，禁言不了。', qq.run('group_mute', {'target': ADMIN}, GID, OWNER))
        self.assertEqual('不能禁言我自己。', qq.run('group_mute', {'target': BOT}, GID, OWNER))
        self.assertIn('不在这个群里', qq.run('group_mute', {'target': '30003'}, GID, OWNER))
        not_admin = self.make(roles=dict(ROLES, **{BOT: 'member'}))
        self.assertEqual('这个群里我不是管理员，禁言群友不了。', not_admin.run('group_mute', {'target': LOLI}, GID, OWNER))
        self.assertEqual('QQ 群操作只能在群里用。', not_admin.run('group_mute', {'target': LOLI}, 'fixture-group', OWNER))


class MessageActionTests(Base):
    def test_recall_the_quoted_message(self):
        messages = {555: {'group_id': GID, 'user_id': int(LOLI), 'raw_message': '[CQ:image,url=x]刷屏'},
                    556: {'group_id': GID, 'user_id': int(ADMIN), 'raw_message': '公告'}}
        qq = self.make(messages=messages)
        self.assertEqual('要撤回哪条？引用那条消息再叫我。', qq.run('recall_message', {}, GID, OWNER))
        self.assertEqual('拒绝：只有群主和管理员能让我撤回消息。', qq.run('recall_message', {}, GID, SLE, turn={'reply_id': '555'}))
        self.assertEqual('那条是群主或管理员发的，我撤回不了。', qq.run('recall_message', {}, GID, OWNER, turn={'reply_id': '556'}))
        self.assertEqual('已撤回那条消息。', qq.run('recall_message', {'reason': '刷屏'}, GID, OWNER, turn={'reply_id': '555'}))
        self.assertIn(('delete_msg', {'message_id': 555}), self.qq.calls)
        self.assertEqual(('recall', '[媒体]刷屏', '刷屏'), tuple(self.audit_rows()[0][k] for k in ('action', 'text', 'reason')))

    def test_essence(self):
        qq = self.make()
        self.assertEqual('已设为群精华。', qq.run('set_essence', {}, GID, ADMIN, turn={'reply_id': '777'}))
        self.assertEqual([('set_essence_msg', {'message_id': 777})], self.qq.calls)

    def test_reactions(self):
        qq = self.make()
        self.assertEqual('已经给那条消息贴了「点赞」。',
                         qq.run('react_message', {'emoji': '点赞'}, GID, SLE, turn={'reply_id': '555', 'message_id': 900}))
        self.assertEqual(('set_msg_emoji_like', {'message_id': 555, 'emoji_id': '76'}), self.qq.calls[0])
        qq.run('react_message', {'emoji': '吃瓜'}, GID, SLE, turn={'message_id': 900})
        self.assertEqual({'message_id': 900, 'emoji_id': '271'}, self.qq.calls[1][1])
        self.assertEqual('这条已经贴过这个表情了。', qq.run('react_message', {'emoji': '点赞'}, GID, SLE, turn={'reply_id': '555'}))
        self.assertIn('这个表情贴不了', qq.run('react_message', {'emoji': '原神'}, GID, SLE, turn={'message_id': 900}))

    def test_profile_like_once_a_day(self):
        qq = self.make()
        self.assertEqual('已经给你的资料卡点了赞。', qq.run('profile_like', {}, GID, SLE))
        self.assertEqual([('send_like', {'user_id': int(SLE), 'times': 10})], self.qq.calls)
        self.assertEqual('今天已经给你点过赞了，明天再来。', qq.run('profile_like', {}, GID, SLE))
        failing = self.make(fail={'send_like': '点赞次数已达上限'})
        self.assertIn('点赞次数已达上限', failing.run('profile_like', {}, GID, SLE))


class PokeTests(Base):
    def test_poke_on_request_with_limits(self):
        qq = self.make()
        self.assertEqual('已经戳了 自称群主的成员。', qq.run('qq_poke', {'target': '自称'}, GID, SLE))
        self.assertEqual('已经戳了 FixtureMember。', qq.run('qq_poke', {'target': '我'}, GID, SLE))
        qq.run('qq_poke', {'target': '自称'}, GID, ADMIN)
        self.assertEqual('刚戳过 自称群主的成员 了，过一会儿再说。', qq.run('qq_poke', {'target': '自称'}, GID, OWNER))
        self.assertEqual('戳我自己？不行。', qq.run('qq_poke', {'target': BOT}, GID, SLE))
        self.assertEqual([('poke', LOLI, GID), ('poke', SLE, GID), ('poke', LOLI, GID)], self.qq.calls)
        self.now[0] += 601
        self.assertEqual('已经戳了 自称群主的成员。', qq.run('qq_poke', {'target': '自称'}, GID, OWNER))

    def test_being_poked(self):
        qq = self.make()
        self.assertIsNone(qq.on_poke(GID, SLE, LOLI))            # someone else was poked
        self.assertEqual(['reply', 'poke_back', 'poke_back', None],
                         [qq.on_poke(GID, SLE, BOT) for _ in range(4)])
        self.assertEqual('reply', qq.on_poke(GID, LOLI, BOT))
        self.now[0] += 601
        self.assertEqual('reply', qq.on_poke(GID, SLE, BOT))

    def test_a_like_on_her_card_is_returned_once_a_day(self):
        qq = self.make()
        self.assertTrue(qq.on_profile_like(SLE))
        self.assertFalse(qq.on_profile_like(SLE))
        self.assertFalse(qq.on_profile_like(BOT))
        self.assertEqual([('send_like', {'user_id': int(SLE), 'times': 10})], self.qq.calls)


class RoutingTests(unittest.TestCase):
    def test_requests(self):
        cases = {'赏29五分钟禁言': {'group_mute'}, '把自称群主的成员禁言10分钟': {'group_mute'}, '给他解除禁言': {'group_mute'},
                 '戳一下他': {'qq_poke'}, '拍一拍Sle': {'qq_poke'}, '给我点个赞': {'profile_like'}, '赞我': {'profile_like'},
                 '给这条点个赞': {'react_message'}, '贴个表情': {'react_message'},
                 '怎么禁言别人': set(), '禁言是什么意思': set(), '去拍照了': set(), '撤回这条': set(), '今天天气不错': set()}
        for text, wanted in cases.items():
            with self.subTest(text=text):
                self.assertEqual(wanted, set(s._qq_action_request(text)))
        self.assertEqual({'recall_message'}, set(s._qq_action_request('撤回这条', has_quote=True)))
        self.assertEqual({'recall_message'}, set(s._qq_action_request('把这个撤了', has_quote=True)))
        self.assertEqual({'set_essence'}, set(s._qq_action_request('设为精华', has_quote=True)))
        self.assertEqual(set(), set(s._qq_action_request('我撤回了一条消息', has_quote=True)))

    def test_a_mute_is_not_a_server_operation(self):
        server = {'id': 'legacy', 'prefix': '[怀旧]'}
        profile = s.request_profile('把自称群主的成员禁言5分钟', selected_server=server, privileged=True, explicit_server=True)
        self.assertFalse(profile['operation_tool_required'])
        self.assertEqual({'group_mute'}, set(profile['qq_request']))
        self.assertEqual('tool', profile['tier'])
        self.assertEqual(frozenset(), s.request_profile('戳一下他', social_only=True)['qq_request'])

    def test_member_tools(self):
        names = {tool['function']['name'] for tool in s.TOOLS_MEMBER}
        self.assertTrue({'qq_poke', 'react_message', 'profile_like'} <= names)
        self.assertFalse({'group_mute', 'recall_message', 'set_essence'} & names)
        self.assertTrue(set(qq_interact.TOOL_NAMES) <= {tool['function']['name'] for tool in s.TOOLS_ADMIN})
        enum = s.REACT_MESSAGE['function']['parameters']['properties']['emoji']['enum']
        self.assertTrue(all(name in qq_interact.EMOJI for name in enum) and '' not in enum)


class LoopTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def test_a_refusal_written_without_the_tool_is_sent_back(self):
        # 10-27 replay: the model refused a member's mute request itself, before the tool could.
        sent, replies = [], iter([text_reply('禁言只有群主或管理员才能执行哦。'), tool_call('group_mute', {'target': '自称'}),
                                 text_reply('得群主或管理员来下令。')])

        def model(payload, **kwargs):
            sent.append(copy.deepcopy(payload))
            return next(replies)
        with mock.patch.object(s, '_text_post', side_effect=model), \
                mock.patch.object(s, 'execute_tool', return_value='拒绝：只有群主和管理员能让我禁言群友。') as tool:
            answer, _usage, _used = s.run_agent('把自称群主的成员禁言5分钟', SLE, False, GID, qq_turn={'mentions': []})
        self.assertEqual('得群主或管理员来下令。', answer)
        self.assertIn('QQ 群操作工具', sent[1]['messages'][-1]['content'])
        self.assertEqual('group_mute', tool.call_args.args[0])
        self.assertNotIn('NON_ADMIN', sent[0]['messages'][0]['content'])
        self.assertNotIn(s.NON_ADMIN_OPERATION_NOTE.strip()[:20], sent[0]['messages'][0]['content'])

    def test_a_members_mute_request_reaches_the_tool_which_refuses(self):
        sent = []
        replies = iter([tool_call('group_mute', {'target': '他'}), text_reply('这个得群主或管理员来。')])

        def model(payload, **kwargs):
            sent.append(payload)
            return next(replies)
        turn = {'mentions': [LOLI], 'reply_id': None, 'message_id': 42}
        with mock.patch.object(s, '_text_post', side_effect=model), \
                mock.patch.object(s, 'execute_tool', return_value='拒绝：只有群主和管理员能让我禁言群友。') as tool:
            answer, _usage, _used = s.run_agent('禁言他五分钟', SLE, False, GID, qq_turn=turn)
        self.assertEqual('这个得群主或管理员来。', answer)
        self.assertEqual('required', sent[0].get('tool_choice'))
        self.assertIn('group_mute', {t['function']['name'] for t in sent[0]['tools']})
        self.assertEqual(turn, tool.call_args.kwargs['qq_turn'])


class DispatchTests(unittest.TestCase):
    def test_execute_tool_hands_over_to_qq_interact(self):
        fake = mock.Mock()
        fake.run.return_value = '已经戳了 自称群主的成员。'
        with mock.patch.object(s, 'QQ_INTERACT', fake):
            result = s.execute_tool('qq_poke', {'target': '自称'}, False, user_id=SLE, group_id=GID, qq_turn={'mentions': []})
        self.assertEqual('已经戳了 自称群主的成员。', result)
        fake.run.assert_called_once_with('qq_poke', {'target': '自称'}, GID, SLE, turn={'mentions': []})
        with mock.patch.object(s, 'QQ_INTERACT', None):
            self.assertEqual('QQ 群操作现在不可用。', s.execute_tool('qq_poke', {}, False))


class ReplyTurnTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def test_sanae_reply_passes_who_was_mentioned_and_what_is_quoted(self):
        captured = {}

        def fake_agent(text, user_id, privileged, group_id, **kwargs):
            captured.update(kwargs)
            return 'ok', mock.Mock(footer=lambda: ''), True
        raw = f'[CQ:reply,id=555][CQ:at,qq={BOT}] [CQ:at,qq={LOLI},name=萝莉] 禁言他五分钟'
        with mock.patch.object(s, '_fetch_reply_raw', return_value='刷屏内容'), \
                mock.patch.object(s, 'run_agent', side_effect=fake_agent):
            s.sanae_reply(raw, OWNER, privileged=True, group_id=GID, force=True, source_message_id=901)
        self.assertEqual({'mentions': [LOLI], 'reply_id': '555', 'message_id': 901}, captured['qq_turn'])
        self.assertIn(f'【本条消息 @ 了：QQ {LOLI}', captured['context_reference'])


class BridgeTests(unittest.TestCase):
    def test_pokes_at_her_get_a_reaction(self):
        event = {'post_type': 'notice', 'notice_type': 'notify', 'sub_type': 'poke', 'group_id': GID,
                 'user_id': int(SLE), 'target_id': int(bridge.WIKI_BOT_QQ)}
        fake = mock.Mock()
        fake.on_poke.return_value = 'reply'
        with mock.patch.object(bridge, 'QQ_INTERACT', fake), \
                mock.patch.object(bridge, '_bridge_group_enabled', return_value=True), \
                mock.patch.object(bridge, '_start_ai_worker') as ai_worker, \
                mock.patch.object(bridge, '_start_worker') as worker:
            bridge._handle_notify(event)
            fake.on_poke.return_value = 'poke_back'
            bridge._handle_notify(event)
            bridge._handle_notify(dict(event, target_id=int(LOLI)))
        self.assertEqual((GID, SLE, 'reply'), ai_worker.call_args.kwargs['args'])
        self.assertEqual((GID, SLE, 'poke_back'), worker.call_args.kwargs['args'])
        self.assertEqual(2, fake.on_poke.call_count)

    def test_the_reaction_answers_then_pokes_back(self):
        with mock.patch.object(bridge, '_fun_recently_muted', return_value=False), \
                mock.patch.object(bridge.QQ_ACTIONS, 'member_info', return_value={'card': 'FixtureMember'}), \
                mock.patch.object(bridge.QQ_ACTIONS, 'poke') as poke, \
                mock.patch.object(bridge._sanae_ai, 'sanae_reply', return_value='戳我干嘛') as reply, \
                mock.patch.object(bridge, 'send_group_msg', return_value=True) as send, \
                mock.patch.object(bridge.SOCIAL_LITE, 'record_outgoing'):
            bridge._poke_reaction(GID, SLE, 'reply')
            bridge._poke_reaction(GID, SLE, 'poke_back')
        self.assertEqual(f'[CQ:at,qq={SLE}] 戳我干嘛', send.call_args.args[0])
        self.assertTrue(reply.call_args.kwargs['social_only'])
        self.assertIn('戳一戳', reply.call_args.kwargs['context_reference'])
        poke.assert_called_once_with(SLE, GID)


if __name__ == '__main__':
    unittest.main()
