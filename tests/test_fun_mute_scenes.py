"""AI-judged joke mutes: batching, the bridge's hard limits, and model I/O validation.

Offline: the model calls are mocks; no QQ action reaches the network.
"""
import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest import mock

import fun_mute
from tests.test_fusion_integration import FusionCase, bridge, s

GID = 1001
MEMBER = '20001'
REAL_OBSERVE = bridge._fun_mute_observe  # captured before FusionCase stubs it


def item(mid, text='来点涩图', uid=MEMBER, **extra):
    row = {'id': str(mid), 'uid': uid, 'sender': 'k' + uid[-3:], 'name': '群友',
           'time': '20:00:00', 'text': text, 'addressed_bot': False, 'mentions_others': False}
    row.update(extra)
    return row


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class JudgeBatchingTests(FusionCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.calls, self.applied = [], []
        self.decisions = []

        def judge(context, batch):
            self.calls.append((context, batch))
            return list(self.decisions)

        self.judge = fun_mute.FunMuteJudge(judge, lambda row, d: self.applied.append((row['id'], d)),
                                           quiet=3, max_wait=10, context_size=3, batch_size=2,
                                           max_pending=4, clock=self.clock, log=lambda line: None)

    def test_waits_for_a_pause_then_judges_with_earlier_chat_as_context(self):
        self.judge.observe(item(1, '早上好', uid='30001'), eligible=False)   # admin: context only
        self.judge.observe(item(2, '今天开服吗'))
        self.assertEqual(0, self.judge.process_once())                    # chat still going
        self.clock.now += 3
        self.assertEqual(1, self.judge.process_once())
        context, batch = self.calls[0]
        self.assertEqual(['1'], [row['id'] for row in context])
        self.assertEqual(['2'], [row['id'] for row in batch])
        self.assertNotIn('uid', batch[0])                                  # the model never sees QQ ids
        self.assertIn('addressed_bot', batch[0])
        self.assertNotIn('addressed_bot', context[0])

    def test_busy_chat_is_judged_after_max_wait_in_batches(self):
        for mid in range(1, 6):
            self.judge.observe(item(mid))
            self.clock.now += 2.5                                          # never a 3 s pause
        self.assertEqual(['2', '3', '4', '5'], [r['id'] for r in self.judge._pending])  # cap 4
        self.assertEqual(2, self.judge.process_once())                     # 12.5 s >= max_wait
        self.assertEqual(['2', '3'], [row['id'] for row in self.calls[0][1]])
        self.assertEqual(2, self.judge.process_once(force=True))
        self.assertEqual(['2', '3'], [row['id'] for row in self.calls[1][0]][-2:])

    def test_decisions_apply_only_to_messages_in_the_batch(self):
        self.judge.observe(item(7))
        self.decisions = [{'id': '7', 'scene': 'adult', 'seconds': 300},
                          {'id': '999', 'scene': 'adult', 'seconds': 300}]
        self.judge.process_once(force=True)
        self.assertEqual(['7'], [mid for mid, _ in self.applied])


class NoticeAndClampTests(FusionCase):
    def test_clamp_by_scene(self):
        self.assertEqual(60, fun_mute.clamp(5, 'adult'))
        self.assertEqual(900, fun_mute.clamp(99999, 'adult'))
        self.assertEqual(3600, fun_mute.clamp(99999, 'self'))
        self.assertEqual(1800, fun_mute.clamp('1800', 'self'))
        self.assertEqual(300, fun_mute.clamp('lots', 'self'))
        self.assertEqual(['1分钟', '5分钟', '1小时', '90秒'],
                         [fun_mute.duration_text(x) for x in (60, 300, 3600, 90)])

    def test_clean_notice(self):
        self.assertEqual('涩图没有，冷静五分钟有。', fun_mute.clean_notice('“涩图没有，冷静五分钟有。”\n第二行'))
        self.assertEqual('先冷静一下', fun_mute.clean_notice('[CQ:at,qq=1] @某人 先冷静一下'))
        self.assertEqual('', fun_mute.clean_notice('[SILENT]'))
        self.assertEqual('', fun_mute.clean_notice('长' * 61))
        self.assertEqual('', fun_mute.clean_notice('<｜｜DSML｜｜ calls>', rejects=s._leaked_tool_markup))


class BridgeApplyTests(FusionCase):
    def setUp(self):
        super().setUp()
        for name, value in (('_FUN_ROLE_CACHE', {}), ('_FUN_MUTED_UNTIL', {}), ('_FUN_LAST_MUTE', {}),
                            ('_FUN_MUTE_TIMES', bridge.collections.deque(maxlen=64)),
                            ('_FUN_RECENT_NOTICES', bridge.collections.deque(maxlen=6))):
            self.stack.enter_context(mock.patch.object(bridge, name, value))
        self.actions = self.stack.enter_context(mock.patch.object(bridge, 'QQ_ACTIONS'))
        self.roles = {bridge.WIKI_BOT_QQ: 'admin', MEMBER: 'member'}
        self.actions.member_info.side_effect = lambda gid, uid: {'role': self.roles[str(uid)]}
        self.notice = self.stack.enter_context(mock.patch.object(
            s, 'write_fun_mute_notice', return_value='片没有，五分钟冷静套餐有。'))
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.state_path = str(Path(self.temp) / 'fun-mute-state.json')
        self.stack.enter_context(mock.patch.object(bridge, 'FUN_MUTE_STATE_PATH', self.state_path))
        self.record = self.stack.enter_context(mock.patch.object(bridge.SOCIAL_LITE, 'record_outgoing'))
        self.stack.enter_context(mock.patch.object(bridge, '_FUN_STATS', {
            'since': bridge.time.time(), 'batches': 0, 'messages': 0, 'dormant': 0, 'decisions': 0,
            'applied': 0, 'skipped': 0, 'errors': 0}))

    def apply(self, scene='adult', seconds=300, row=None):
        bridge._fun_apply(row or item(1), {'id': '1', 'scene': scene, 'seconds': seconds, 'reason': 'r'})

    def test_mute_then_model_worded_notice_to_the_group(self):
        self.apply()
        self.actions.mute.assert_called_once_with(GID, MEMBER, 300)
        self.assertEqual('片没有，五分钟冷静套餐有。', self.send.call_args.args[0])
        args = self.notice.call_args.args
        self.assertEqual(('来点涩图', 'adult', '5分钟', 'r', []), args)
        self.assertTrue(bridge._fun_recently_muted(GID, MEMBER))

    def test_notice_enters_her_own_context_as_a_mute(self):
        self.apply()
        self.record.assert_called_once_with(GID, '（禁言 5分钟）片没有，五分钟冷静套餐有。',
                                            action='mute', reply_to_user_id=MEMBER)

    def test_limits_survive_a_restart(self):
        self.apply()
        for table in (bridge._FUN_LAST_MUTE, bridge._FUN_MUTED_UNTIL):
            table.clear()
        bridge._FUN_MUTE_TIMES.clear()
        self.assertTrue(bridge._fun_load_state())
        self.assertIn(MEMBER, bridge._FUN_LAST_MUTE)
        self.assertTrue(bridge._fun_recently_muted(GID, MEMBER))
        self.assertEqual(1, len(bridge._FUN_MUTE_TIMES))
        self.assertEqual(['片没有，五分钟冷静套餐有。'], list(bridge._FUN_RECENT_NOTICES)[-1:])
        self.apply()                                                  # still in cooldown
        self.assertEqual(1, self.actions.mute.call_count)

    def test_old_state_entries_expire_on_load(self):
        old = bridge.time.time() - 7200
        Path(self.state_path).write_text(json.dumps({'last_mute': {MEMBER: old}, 'mute_times': [old],
            'muted_until': {f'{GID}:{MEMBER}': old}, 'recent_notices': ['旧台词']}), encoding='utf-8')
        self.assertTrue(bridge._fun_load_state())
        self.assertEqual({}, dict(bridge._FUN_LAST_MUTE))
        self.assertFalse(bridge._fun_recently_muted(GID, MEMBER))
        self.assertEqual(0, len(bridge._FUN_MUTE_TIMES))

    def test_hourly_summary_line(self):
        bridge._FUN_STATS['since'] -= 3601
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bridge._fun_stat('batches')
        self.assertIn('[fun-mute] last hour: batches=1 messages=0', out.getvalue())
        self.assertEqual(0, bridge._FUN_STATS['batches'])

    def test_durations_are_clamped(self):
        self.apply(scene='self', seconds=99999)
        self.actions.mute.assert_called_once_with(GID, MEMBER, 3600)

    def test_admin_target_and_missing_notice_are_skipped(self):
        self.roles[MEMBER] = 'admin'
        self.apply()
        self.actions.mute.assert_not_called()
        self.roles[MEMBER] = 'member'
        self.notice.return_value = '[SILENT]'
        self.apply()
        self.actions.mute.assert_not_called()
        self.send.assert_not_called()
        self.assertFalse(bridge._fun_recently_muted(GID, MEMBER))

    def test_cooldown_and_hourly_cap(self):
        self.apply()
        self.apply()
        self.assertEqual(1, self.actions.mute.call_count)                  # same member, 15 min
        for n in range(2, 20):
            uid = str(30000 + n)
            self.roles[uid] = 'member'
            self.apply(row=item(n, uid=uid))
        self.assertEqual(bridge.FUN_MUTE_HOURLY_CAP, self.actions.mute.call_count)

    def test_failed_ban_sends_nothing_and_releases_the_member(self):
        self.actions.mute.side_effect = RuntimeError('no permission')
        self.apply()
        self.send.assert_not_called()
        self.assertFalse(bridge._fun_recently_muted(GID, MEMBER))

    def test_judge_is_dormant_while_bot_is_not_admin(self):
        self.roles[bridge.WIKI_BOT_QQ] = 'member'
        with mock.patch.object(s, 'judge_fun_mute') as model:
            self.assertEqual([], bridge._fun_judge([], [item(1)]))
        model.assert_not_called()


class BridgeObserveTests(FusionCase):
    def setUp(self):
        super().setUp()
        self.judge = mock.Mock()
        self.stack.enter_context(mock.patch.object(bridge, '_fun_mute_observe', REAL_OBSERVE))
        self.stack.enter_context(mock.patch.object(bridge, 'FUN_MUTE_JUDGE', self.judge))
        self.stack.enter_context(mock.patch.object(bridge, 'FUN_MUTE_AI_ENABLED', True))

    def observe(self, text, uid=MEMBER, gid=GID):
        bridge._fun_mute_observe({'group_id': gid, 'user_id': uid, 'message_id': 5,
                                  'sender': {'card': '阿明'}, 'raw_message': text}, text, False)

    def test_members_are_judged_others_are_context_and_commands_are_skipped(self):
        self.observe('来点涩图')
        row, = self.judge.observe.call_args.args
        self.assertTrue(self.judge.observe.call_args.kwargs['eligible'])
        self.assertEqual(('5', MEMBER, '阿明', '来点涩图'), (row['id'], row['uid'], row['name'], row['text']))
        self.observe('来点涩图', uid=bridge.WIKI_BOT_QQ)
        self.assertFalse(self.judge.observe.call_args.kwargs['eligible'])
        self.judge.reset_mock()
        self.observe('!mods')
        self.observe('[CQ:image,file=x]')
        self.observe('来点涩图', gid=1)
        self.judge.observe.assert_not_called()

    def test_switched_off_means_nothing_is_queued(self):
        with mock.patch.object(bridge, 'FUN_MUTE_AI_ENABLED', False):
            self.observe('禁言我10分钟')
        self.judge.observe.assert_not_called()


class ModelIOTests(FusionCase):
    def response(self, payload):
        return {'choices': [{'message': {'content': json.dumps(payload, ensure_ascii=False)}}]}

    def test_judge_validates_ids_scenes_and_one_per_sender(self):
        batch = [item(1, sender='a'), item(2, sender='a'), item(3, sender='b')]
        for row in batch:
            row.pop('uid')
        reply = self.response({'mutes': [
            {'id': '1', 'scene': 'self', 'seconds': '600', 'reason': '本人要求'},
            {'id': '2', 'scene': 'adult', 'seconds': 300},          # same sender again
            {'id': '3', 'scene': 'weird', 'seconds': 'x'},          # unknown scene, bad seconds
            {'id': '404', 'scene': 'adult'}, 'junk']})
        with mock.patch.object(s, '_post', return_value=reply) as post:
            decisions = s.judge_fun_mute([], batch)
        self.assertEqual([('1', 'self', 600), ('3', 'other', 300)],
                         [(d['id'], d['scene'], d['seconds']) for d in decisions])
        request = post.call_args.args[2]
        self.assertEqual({'type': 'disabled'}, request['thinking'])
        self.assertEqual({'type': 'json_object'}, request['response_format'])
        self.assertIn('只对 judge 里的消息做决定', request['messages'][0]['content'])

    def test_judge_with_nothing_to_judge_makes_no_call(self):
        with mock.patch.object(s, '_post') as post:
            self.assertEqual([], s.judge_fun_mute([], []))
        post.assert_not_called()

    def test_notice_uses_the_persona_and_returns_model_text(self):
        reply = {'choices': [{'message': {'content': '如你所愿，十分钟口球套餐送达。'}}]}
        with mock.patch.object(s, '_text_post', return_value=reply) as post:
            text = s.write_fun_mute_notice('禁言我10分钟', 'self', '10分钟', '本人要求', ['上一句'])
        self.assertEqual('如你所愿，十分钟口球套餐送达。', text)
        payload = post.call_args.args[0]
        self.assertTrue(payload['messages'][0]['content'].startswith(s.SANAE_PERSONALITY))
        self.assertIn('上一句', payload['messages'][1]['content'])
