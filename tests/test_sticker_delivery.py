"""Offline sticker delivery regressions with a real temporary asset catalog."""
import base64
import json
import tempfile
import threading
from pathlib import Path
from unittest import mock

from sticker_catalog import StickerCatalog
from tests.test_fusion_integration import FusionCase, bridge, s


class LiveTurn:
    def __init__(self):
        self.live = True

    def current(self):
        return self.live

    def finish(self):
        pass


class StickerDeliveryTests(FusionCase):
    BODY = '哈哈，好耶，终于成功了。'
    REQUEST = '今天终于修好了，好耶'

    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'assets').mkdir()
        pixel = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7gcAAAAASUVORK5CYII=')
        entries = []
        for index in range(4):
            name = f'happy-{index}.png'
            (self.root / 'assets' / name).write_bytes(pixel)
            entries.append({'id': f'happy-{index}', 'file': 'assets/' + name,
                            'primary_mood': '开心', 'moods': ['开心'],
                            'semantic_confidence': 'high'})
        self.path = self.root / 'stickers.json'
        self.path.write_text(json.dumps({'entries': entries}, ensure_ascii=False), encoding='utf-8')
        self.catalog = StickerCatalog(self.path)
        self.now = 1_800_000_000
        self.gid = bridge.SOCIAL_LITE_GROUP
        for name, value in (('STICKER_CATALOG', self.catalog), ('SOCIAL_STICKER_MODE', 'auto'),
                            ('SOCIAL_STICKER_COOLDOWN', 45), ('_LAST_AUTO_STICKER', {}),
                            ('_PENDING_AUTO_STICKER', set())):
            self.stack.enter_context(mock.patch.object(bridge, name, value))
        self.stack.enter_context(mock.patch.object(bridge.time, 'time', side_effect=lambda: self.now))
        self.stack.enter_context(mock.patch.object(bridge.time, 'sleep'))
        self.stack.enter_context(mock.patch.object(bridge, '_expression_context', return_value=''))
        self.stack.enter_context(mock.patch.object(bridge, '_expression_sent'))
        self.stack.enter_context(mock.patch.object(bridge.FEEDBACK_STORE, 'style_override', return_value=''))
        self.stack.enter_context(mock.patch.object(bridge.SOCIAL_LITE, 'record_outgoing'))

    def history(self, gid=None):
        path = self.path.with_name('selection-history.json')
        data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        return data.get(str(self.gid if gid is None else gid), [])

    def model(self, body=None, *, footer='', model_answer=True, body_metadata=True):
        body = self.BODY if body is None else body

        def reply(*args, reply_metadata, **kwargs):
            reply_metadata['model_answer'] = model_answer
            if body_metadata:
                reply_metadata['reply_body'] = body
            return body + footer

        return self.stack.enter_context(mock.patch.object(s, 'sanae_reply', side_effect=reply))

    def ai(self, event=None):
        self.handler._sanae_reply_thread(
            event or self.event('[CQ:at,qq=1002] ' + self.REQUEST), True)

    def social(self, event=None):
        self.handler._social_reply_thread(event or self.event(self.REQUEST), '测试群聊上下文')

    def assert_unused(self):
        self.assertEqual(self.history(), [])
        self.assertNotIn(str(self.gid), bridge._LAST_AUTO_STICKER)
        # No failed/cancelled send may leave the next reply reserved out.
        with bridge._auto_sticker(self.gid, self.REQUEST, self.BODY) as selected:
            self.assertIsNotNone(selected)
        self.assertEqual(self.history(), [])

    def assert_sent_once(self):
        history = self.history()
        self.assertEqual(len(history), 1)
        self.assertIsNotNone(self.catalog.resolve(history[0]))
        self.assertEqual(bridge._LAST_AUTO_STICKER[str(self.gid)], self.now)
        with bridge._auto_sticker(self.gid, self.REQUEST, self.BODY) as selected:
            self.assertIsNone(selected)

    def test_short_model_body_with_long_usage_footer_attaches_image(self):
        footer = '\n———\n模型与用量：' + '这是测试用量明细。' * 30
        self.assertGreater(len(self.BODY + footer), 180)
        self.model(footer=footer)
        self.ai()
        self.send.assert_called_once()
        delivered = self.send.call_args.args[0]
        self.assertIn(self.BODY, delivered)
        self.assertIn(footer.removesuffix('。') + '\n[CQ:image,file=', delivered)
        self.assertIn('[CQ:image,file=', delivered)
        self.assert_sent_once()

    def test_stub_without_body_metadata_uses_reply_as_body(self):
        self.model(body_metadata=False)
        self.ai()
        self.send.assert_called_once()
        self.assertIn('[CQ:image,file=', self.send.call_args.args[0])
        self.assert_sent_once()

    def test_ai_failed_combined_receipt_does_not_consume_rotation_or_cooldown(self):
        self.model()
        self.send.return_value = False
        self.ai()
        self.send.assert_called_once()
        self.assertIn('[CQ:image,file=', self.send.call_args.args[0])
        self.assert_unused()

    def test_stale_after_selection_releases_without_sending(self):
        self.model()
        turn = LiveTurn()
        event = self.event('[CQ:at,qq=1002] ' + self.REQUEST)
        event['_conversation_turn'] = turn
        original = self.catalog.pick_for_send

        def selected_then_replaced(*args, **kwargs):
            selected = original(*args, **kwargs)
            turn.live = False
            return selected

        with mock.patch.object(self.catalog, 'pick_for_send', side_effect=selected_then_replaced):
            self.ai(event)
        self.send.assert_not_called()
        self.assert_unused()

    def test_missing_assets_sends_text_without_consuming_history(self):
        self.model()
        for asset in (self.root / 'assets').iterdir():
            asset.unlink()
        self.ai()
        self.send.assert_called_once()
        self.assertNotIn('[CQ:image', self.send.call_args.args[0])
        self.assertEqual(self.history(), [])
        self.assertNotIn(str(self.gid), bridge._LAST_AUTO_STICKER)

    def test_nonmodel_error_text_never_gets_auto_sticker(self):
        # Deliberately contains mood keywords: model provenance must win.
        self.model(body='哈哈，失误了，服务暂不可用。', model_answer=False)
        self.ai()
        self.social()
        self.assertEqual(self.send.call_count, 2)
        self.assertTrue(all('[CQ:image' not in call.args[0] for call in self.send.call_args_list))
        self.assert_unused()

    def test_model_exception_emits_only_error_notice_without_consuming(self):
        with mock.patch.object(s, 'sanae_reply', side_effect=TimeoutError('offline test')):
            self.ai()
        self.send.assert_called_once()
        self.assertNotIn('[CQ:image', self.send.call_args.args[0])
        self.assert_unused()

    def test_empty_or_silent_reply_does_not_consume(self):
        for text in (None, '[SILENT]'):
            with self.subTest(reply=text), mock.patch.object(s, 'sanae_reply', return_value=text):
                self.social()
        self.send.assert_not_called()
        self.assert_unused()

    def test_social_single_segment_success_commits_once(self):
        self.model()
        with mock.patch.object(bridge, 'split_social_reply', return_value=[self.BODY]):
            self.social()
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.send.call_args_list[0].args[0], self.BODY.rstrip('。'))
        self.assertTrue(self.send.call_args_list[1].args[0].startswith('[CQ:image,file='))
        self.assert_sent_once()

    def test_social_segmented_image_failure_does_not_consume(self):
        self.model()
        self.send.side_effect = [True, True, False]
        with mock.patch.object(bridge, 'split_social_reply', return_value=['哈哈，好耶。', '终于成功了。']):
            self.social()
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual([call.args[0] for call in self.send.call_args_list[:2]],
                         ['哈哈，好耶', '终于成功了'])
        self.assertTrue(self.send.call_args.args[0].startswith('[CQ:image,file='))
        self.assert_unused()

    def test_social_segmented_text_failure_never_sends_image(self):
        self.model()
        self.send.side_effect = [True, False]
        with mock.patch.object(bridge, 'split_social_reply', return_value=['哈哈，好耶。', '终于成功了。']):
            self.social()
        self.assertEqual(self.send.call_count, 2)
        self.assertTrue(all('[CQ:image' not in call.args[0] for call in self.send.call_args_list))
        self.assert_unused()

    def test_social_segmented_image_success_records_one_selection(self):
        self.model()
        with mock.patch.object(bridge, 'split_social_reply', return_value=['哈哈，好耶。', '终于成功了。']):
            self.social()
        self.assertEqual(self.send.call_count, 3)
        self.assertTrue(self.send.call_args.args[0].startswith('[CQ:image,file='))
        self.assert_sent_once()

    def test_helper_concurrent_group_reservation_is_released(self):
        entered, finish = threading.Event(), threading.Event()
        selected_by_worker, errors = [], []

        def reserve_in_worker():
            try:
                with bridge._auto_sticker(self.gid, self.REQUEST, self.BODY) as selected:
                    selected_by_worker.append(selected)
                    entered.set()
                    if not finish.wait(3):
                        raise AssertionError('test worker was not released')
            except BaseException as exc:
                errors.append(exc)
                entered.set()

        worker = threading.Thread(target=reserve_in_worker)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertFalse(errors)
            self.assertIsNotNone(selected_by_worker[0])
            with bridge._auto_sticker(self.gid, self.REQUEST, self.BODY) as overlap:
                self.assertIsNone(overlap)
            with bridge._auto_sticker(self.gid + 1, self.REQUEST, self.BODY) as other_group:
                self.assertIsNotNone(other_group)
        finally:
            finish.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assert_unused()

    def test_helper_exception_releases_without_consuming(self):
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with bridge._auto_sticker(self.gid, self.REQUEST, self.BODY) as selected:
                self.assertIsNotNone(selected)
                raise RuntimeError('fixture')
        self.assert_unused()

    def test_cooldown_starts_at_success_and_then_rotates(self):
        with bridge._auto_sticker(self.gid, self.REQUEST, self.BODY) as selected:
            first_id = selected['id']
            self.now += 30
            selected['delivered'] = True
        self.assert_sent_once()
        self.now += 44
        with bridge._auto_sticker(self.gid, self.REQUEST, self.BODY) as cooling:
            self.assertIsNone(cooling)
        self.now += 1
        with bridge._auto_sticker(self.gid, self.REQUEST, self.BODY) as ready:
            self.assertIsNotNone(ready)
            self.assertNotEqual(ready['id'], first_id)
        self.assertEqual(self.history(), [first_id])

    def test_delivery_history_write_failure_does_not_repeat_visible_reply(self):
        self.model()
        with mock.patch.object(self.catalog, 'mark_sent', side_effect=OSError('fixture disk full')):
            self.ai()
        self.send.assert_called_once()
        self.assertIn('[CQ:image,file=', self.send.call_args.args[0])
        self.assertIn(str(self.gid), bridge._LAST_AUTO_STICKER)
        with bridge._auto_sticker(self.gid, self.REQUEST, self.BODY) as cooling:
            self.assertIsNone(cooling)

    def test_real_reply_metadata_excludes_footer_and_preserves_streamed_body(self):
        usage = s._Usage()
        usage.model_answer = True
        usage.calls = 1
        usage.footer = lambda: '———\n测试用量明细' * 30
        body = '哈哈，好耶，终于成功了。'
        metadata = {}
        with mock.patch.object(s, 'run_agent', return_value=(body, usage, False)):
            output = s.sanae_reply(self.REQUEST, '2001', group_id=self.gid,
                                   privileged=True, force=True, social_only=True,
                                   include_usage_footer=True, reply_metadata=metadata)
        self.assertTrue(metadata['model_answer'])
        self.assertEqual(metadata['reply_body'], body)
        self.assertGreater(len(output), 180)
        prefix = '哈哈，这是一段已经发过的长正文。' * 20
        usage.streamed_text = prefix
        complete_body = prefix + body
        with mock.patch.object(s, 'run_agent', return_value=(complete_body, usage, False)):
            tail = s.sanae_reply(self.REQUEST, '2001', group_id=self.gid,
                                 privileged=True, force=True, social_only=True,
                                 include_usage_footer=False, reply_metadata=metadata)
        self.assertEqual(tail, body)
        self.assertEqual(metadata['reply_body'], complete_body)
        with bridge._auto_sticker(self.gid, self.REQUEST, metadata['reply_body']) as selected:
            self.assertIsNone(selected)
        self.assert_unused()
