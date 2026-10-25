"""Local turn coordination tests; fake time/timers, no model or QQ calls."""
import contextvars
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conversation_turns import ConversationTurns


class Clock:
    def __init__(self):
        self.now = 100.0
        self.timers = []

    def __call__(self):
        return self.now

    def timer(self, delay, callback):
        clock = self
        class Timer:
            def __init__(self):
                self.due = clock.now + delay
                self.cancelled = False
                self.started = False
                self.daemon = False

            def start(self):
                self.started = True
                clock.timers.append(self)

            def cancel(self):
                self.cancelled = True

            def run(self):
                callback()
        return Timer()

    def advance(self, seconds):
        target = self.now + seconds
        while True:
            active = [timer for timer in self.timers if timer.started and
                      not timer.cancelled and timer.due <= target]
            if not active:
                break
            timer = min(active, key=lambda row: row.due)
            self.now = timer.due
            timer.started = False
            timer.run()
        self.now = target


def event(mid='1', text='早苗，接个梗', uid='11', gid='22', reference='', mention=True):
    segments = []
    raw = ''
    if reference:
        segments.append({'type': 'reply', 'data': {'id': reference}})
        raw += '[CQ:reply,id=' + reference + ']'
    if mention:
        segments.append({'type': 'at', 'data': {'qq': '99'}})
        raw += '[CQ:at,qq=99]'
    segments.append({'type': 'text', 'data': {'text': text}})
    return {'post_type': 'message', 'message_type': 'group', 'group_id': gid,
            'user_id': uid, 'self_id': '99', 'message_id': mid,
            'raw_message': raw + text, 'message': segments, 'sender': {'nickname': '测试成员'}}


class TurnsTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.turns = ConversationTurns(('22', '23'), clock=self.clock, timer_factory=self.clock.timer)
        self.addCleanup(self.turns.close)
        self.sent = []

    def put(self, item=None, dispatch=None, **kwargs):
        return self.turns.submit(item or event(), dispatch or self.sent.append,
                                 chat_only=True, **kwargs)

    def token(self, index=-1):
        return self.sent[index]['_conversation_turn']

    def test_same_user_burst_waits_quiet_window_then_merges_in_order(self):
        original = event('1', '前半句')
        self.assertTrue(self.put(original))
        self.clock.advance(.7)
        self.assertTrue(self.put(event('2', '后半句')))
        self.clock.advance(.7)
        self.assertEqual([], self.sent)
        self.clock.advance(.3)
        self.assertEqual(1, len(self.sent))
        self.assertIn('前半句\n[CQ:at,qq=99]后半句', self.sent[0]['raw_message'])
        self.assertEqual(('1', '2'), self.token().source_ids)
        self.assertIs(self.sent[0], self.token().merged_event)
        self.assertTrue(self.token().current())
        self.assertNotIn('_conversation_turn', original)

    def test_continuous_burst_has_three_second_total_wait_limit(self):
        self.put(event('1'))
        for index in range(2, 5):
            self.clock.advance(.8)
            self.put(event(str(index)))
        self.clock.advance(.60001)
        self.assertEqual(1, len(self.sent))
        self.assertEqual(('1', '2', '3', '4'), self.token().source_ids)

    def test_users_and_groups_are_separate(self):
        for mid, uid, gid in [('1', '11', '22'), ('2', '12', '22'), ('3', '11', '23')]:
            self.put(event(mid, uid=uid, gid=gid))
        self.clock.advance(1)
        self.assertEqual(3, len(self.sent))
        self.put(event('4', uid='11', gid='22'))
        self.assertFalse(self.token(0).current())
        self.assertTrue(self.token(1).current())
        self.assertTrue(self.token(2).current())

    def test_different_reply_targets_neither_merge_nor_cancel(self):
        self.put(event('1', reference='100'))
        self.put(event('2', reference='101'))
        self.clock.advance(1)
        self.assertEqual(2, len(self.sent))
        self.put(event('3', reference='100'))
        self.assertFalse(self.token(0).current())
        self.assertTrue(self.token(1).current())

    def test_same_source_id_is_accepted_without_rescheduling_or_repeating(self):
        self.put(event('1'))
        first = self.clock.timers[-1]
        self.assertTrue(self.put(event('1', '重复投递')))
        self.assertIs(first, self.clock.timers[-1])
        self.clock.advance(1)
        self.assertEqual(('1',), self.token().source_ids)
        self.assertTrue(self.put(event('1')))
        self.clock.advance(1)
        self.assertEqual(1, len(self.sent))

    def test_undirected_correction_reuses_active_prompt_and_original_at(self):
        self.put(event('1', '我没改配置'))
        self.clock.advance(1)
        old = self.token()
        self.assertTrue(self.put(event('2', '不对，我改了十几项', mention=False), directed=False))
        self.assertFalse(old.current())
        self.clock.advance(1)
        self.assertEqual(('1', '2'), self.token().source_ids)
        self.assertIn('我没改配置', self.sent[-1]['raw_message'])
        self.assertIn('不对，我改了十几项', self.sent[-1]['raw_message'])
        self.assertTrue(any(row.get('type') == 'at' for row in self.sent[-1]['message']))

    def test_undirected_ordinary_text_or_correction_without_active_turn_not_accepted(self):
        self.assertFalse(self.put(event('1', '不对，我改了', mention=False), directed=False))
        self.put(event('2'))
        self.assertFalse(self.put(event('3', '随便接一句', mention=False), directed=False))
        self.clock.advance(1)
        self.token().finish()
        self.assertFalse(self.put(event('4', '补充一下，我改了', mention=False), directed=False))

    def test_unquoted_correction_with_two_active_reference_lanes_is_ambiguous(self):
        self.put(event('1', reference='100'))
        self.put(event('2', reference='101'))
        self.assertFalse(self.put(event('3', '不对，我说错了', mention=False), directed=False))
        self.assertTrue(self.put(event('4', '不对，我说错了', reference='100', mention=False), directed=False))

    def test_correction_addressed_to_someone_else_does_not_interrupt(self):
        self.put(event('1'))
        self.clock.advance(1)
        other = event('2', '不对，我说错了', mention=False)
        other['message'].insert(0, {'type': 'at', 'data': {'qq': '88'}})
        self.assertFalse(self.put(other, directed=False))
        self.assertTrue(self.token().current())

    def test_undirected_correction_preserves_original_dispatch_route(self):
        social, direct = [], []
        self.put(event('1', '早苗，接个梗', mention=False), dispatch=social.append)
        self.clock.advance(1)
        self.put(event('2', '不对，我说错了', mention=False),
                 dispatch=direct.append, directed=False)
        self.clock.advance(1)
        self.assertEqual(2, len(social))
        self.assertEqual([], direct)
        self.assertEqual(('1', '2'), social[-1]['_conversation_turn'].source_ids)

    def test_new_direct_question_after_eight_seconds_does_not_replay_old_prompt(self):
        self.put(event('1', '前一个话题'))
        self.clock.advance(1)
        self.clock.advance(8.01)
        self.put(event('2', '换个话题'))
        self.clock.advance(1)
        self.assertEqual(('2',), self.token().source_ids)
        self.assertNotIn('前一个话题', self.sent[-1]['raw_message'])

    def test_direct_supplement_during_generation_keeps_unsaved_prompt(self):
        self.put(event('1', '今天吃面'))
        self.clock.advance(1)
        original = self.token()
        self.clock.advance(7.9)
        self.put(event('2', '加个蛋'))
        self.assertFalse(original.current())
        self.clock.advance(1)
        self.assertEqual(('1', '2'), self.token().source_ids)
        self.assertLess(self.sent[-1]['raw_message'].index('今天吃面'),
                        self.sent[-1]['raw_message'].index('加个蛋'))

    def test_explicit_correction_after_eight_seconds_still_keeps_active_prompt(self):
        self.put(event('1', '今天吃面'))
        self.clock.advance(1)
        self.clock.advance(8.01)
        self.put(event('2', '不对，今天吃饭', mention=False), directed=False)
        self.clock.advance(1)
        self.assertEqual(('1', '2'), self.token().source_ids)

    def test_requires_caller_chat_classification_and_enabled_group(self):
        self.assertFalse(self.turns.submit(event(), self.sent.append))
        self.assertFalse(self.put(event(gid='24')))
        self.assertFalse(self.put(event(mid='missing')))
        private = event()
        private['message_type'] = 'private'
        self.assertFalse(self.put(private))

    def test_commands_and_media_not_coordinated(self):
        for text in ['!问 什么是玻色子', '！重启', '/help', '早苗，!问 怎么回事']:
            self.assertFalse(self.put(event(text=text)))
        for kind in ['image', 'record', 'video', 'file', 'forward', 'face']:
            item = event()
            item['message'].append({'type': kind, 'data': {'file': 'test'}})
            self.assertFalse(self.put(item))

    def test_expired_tokens_cannot_send_commit_or_begin_tools(self):
        self.put()
        self.clock.advance(1)
        token = self.token()
        self.clock.advance(300)
        called = []
        self.assertFalse(token.current())
        self.assertFalse(token.commit_if_current(lambda: called.append('write')))
        self.assertFalse(token.protect_tools())
        self.assertEqual([], called)

    def test_forget_cancels_pending_and_inflight_only_matching_person(self):
        self.put(event('1'))
        self.put(event('2', uid='12'))
        self.clock.advance(1)
        self.put(event('3'))
        self.turns.forget('22', '11')
        self.clock.advance(1)
        self.assertEqual(2, len(self.sent))
        self.assertFalse(self.token(0).current())
        self.assertTrue(self.token(1).current())
        self.turns.forget('22')
        self.assertFalse(self.token(1).current())

    def test_retraction_invalidates_whole_merged_answer(self):
        self.put(event('1'))
        self.put(event('2'))
        self.clock.advance(1)
        self.turns.retract('22', '1')
        self.assertFalse(self.token().current())

    def test_callback_exception_and_queue_rejection_finish_token(self):
        observed = []
        def fail(item):
            observed.append(item['_conversation_turn'])
            raise RuntimeError('synthetic dispatch failure')
        self.put(event('1'), dispatch=fail)
        self.clock.advance(1)
        self.assertFalse(observed[0].current())
        self.put(event('2'), dispatch=lambda item: (observed.append(item['_conversation_turn']) or False))
        self.clock.advance(1)
        self.assertFalse(observed[1].current())
        self.assertEqual(2, self.turns.dispatch_failures)
        self.put(event('3'))
        self.clock.advance(1)
        self.assertTrue(self.token().current())

    def test_latest_contextvars_are_preserved_for_callback(self):
        group = contextvars.ContextVar('test_reply_group', default='none')
        observed = []
        group.set('22')
        self.put(event('1'), dispatch=lambda _: observed.append(group.get()))
        group.set('23')
        self.put(event('2', gid='23'), dispatch=lambda _: observed.append(group.get()))
        group.set('outside')
        self.clock.advance(1)
        self.assertEqual(['22', '23'], observed)
        self.assertEqual('outside', group.get())

    def test_stale_request_cannot_start_tools_but_started_tools_are_not_cancelled(self):
        self.put(event('1'))
        self.clock.advance(1)
        first = self.token()
        self.put(event('2'))
        self.assertFalse(first.protect_tools())
        self.clock.advance(1)
        second = self.token()
        self.assertTrue(second.protect_tools())
        self.put(event('3', '不对，换个说法'))
        self.assertTrue(second.current())
        self.clock.advance(1)
        self.assertEqual(('3',), self.token().source_ids)
        self.turns.forget('22')
        self.assertTrue(second.current())
        second.finish()
        self.assertFalse(second.current())

    def test_current_commit_writes_once_and_stale_commit_is_skipped(self):
        self.put(event('1'))
        self.clock.advance(1)
        token = self.token()
        writes = []
        self.assertTrue(token.commit_if_current(lambda: writes.append('first')))
        self.put(event('2'))
        self.assertFalse(token.commit_if_current(lambda: writes.append('stale')))
        self.assertEqual(['first'], writes)

    def test_close_cancels_timers_and_rejects_more_submissions(self):
        self.put()
        old_timer = self.clock.timers[-1]
        self.turns.close()
        self.assertTrue(old_timer.cancelled)
        old_timer.run()  # Even an already-awakened cancelled callback cannot dispatch.
        self.assertEqual([], self.sent)
        self.assertFalse(self.put(event('2')))

    def test_capacity_and_seen_ids_are_bounded(self):
        small = ConversationTurns(('22',), clock=self.clock, timer_factory=self.clock.timer, max_lanes=2)
        self.addCleanup(small.close)
        for index in range(80):
            small.submit(event(str(index + 1), uid=str(index + 10)), self.sent.append, chat_only=True)
        self.assertEqual(2, len(small._lanes))
        self.assertLessEqual(len(small._seen), 32)
        self.assertLessEqual(sum(not timer.cancelled for timer in self.clock.timers), 2)

    def test_stale_cancelled_timer_does_not_dispatch_new_version_early(self):
        self.put(event('1'))
        stale = self.clock.timers[-1]
        self.clock.advance(.5)
        self.put(event('2'))
        stale.run()
        self.assertEqual([], self.sent)
        self.clock.advance(1)
        self.assertEqual(1, len(self.sent))


if __name__ == '__main__':
    unittest.main()
