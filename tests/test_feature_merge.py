import base64
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave
import zipfile
from unittest.mock import patch, Mock

import llbot_bridge as bridge
import sanae_ai as ai
import media_pipeline as media
import media_transport as transport
import request_runtime as requests
import mod_inventory as inventory
import ops_commands
from shared_knowledge import SharedKnowledge


def wav_bytes(frames=1000, channels=1):
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setparams((channels, 2, 24000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x11\x00' * frames * channels)
    return out.getvalue()


def response(text='', calls=()):
    return {'choices': [{'message': {'content': text, 'tool_calls': list(calls)}}]}


def call(name='read_server_log', args=None, ident='fixture'):
    return {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args or {})}}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name in ('_no_tool_explanation', '_generic_hypothetical', '_has_server_query_intent',
                     '_has_console_command_intent', '_has_confirmation_intent', '_has_cancellation_intent',
                     '_has_mutation_intent', '_has_config_write_intent', '_has_group_file_download_intent'):
            self.stack.enter_context(patch.object(ai, name, return_value=False))
        self.stack.enter_context(patch.object(ai, '_get_history', return_value=[]))
        self.history = self.stack.enter_context(patch.object(ai, '_set_history'))
        self.stack.enter_context(patch.object(ai, '_add_text_usage'))
        self.stack.enter_context(patch.object(ai.SHARED_KNOWLEDGE, 'context', return_value=''))
        self.tool = self.stack.enter_context(patch.object(ai, 'execute_tool', return_value='fixture result'))

    def run_agent(self, text='请读取日志并解释报错', **kwargs):
        defaults = {'selected_server': {'id': 'legacy'}, 'explicit_server': True}
        defaults.update(kwargs)
        return ai.run_agent(text, 'fixture-user', True, 'fixture-group', **defaults)

    def test_plan_continues_once_then_tool_and_answer(self):
        with patch.object(ai, '_text_post', side_effect=[response('我先读取日志'), response(calls=[call()]), response('确认结论')]) as post:
            answer, _, used = self.run_agent()
        self.assertEqual(answer, '确认结论')
        self.assertEqual(post.call_count, 3)
        self.assertEqual(self.tool.call_count, 1)
        self.assertTrue(used)

    def test_repeated_plan_stops_without_execution(self):
        with patch.object(ai, '_text_post', return_value=response('我先读取日志')) as post:
            answer, _, _ = self.run_agent()
        self.assertEqual(post.call_count, 2)
        self.tool.assert_not_called()
        self.assertIn('没有继续执行', answer)

    def test_normal_answer_has_no_recovery_request(self):
        with patch.object(ai, '_text_post', return_value=response('普通答复')) as post:
            self.run_agent('你好')
        self.assertEqual(post.call_count, 1)

    def test_refusal_is_not_a_plan(self):
        self.assertFalse(requests.looks_like_plan('无法读取日志，我先检查工具是否可用', '读取日志', True))
        self.assertFalse(requests.looks_like_plan('我先读取日志', '读取日志', False))

    def test_tool_count_is_bounded(self):
        with patch.object(ai, '_text_post', return_value=response(calls=[call(ident=str(i)) for i in range(30)])):
            answer, _, _ = self.run_agent()
        self.assertEqual(self.tool.call_count, 12)
        self.assertIn('上限', answer)

    def test_unadvertised_tool_is_not_executed(self):
        with patch.object(ai, '_text_post', side_effect=[response(calls=[call('read_file')]), response('拒绝')]):
            self.run_agent()
        self.tool.assert_not_called()

    def test_model_calls_including_recovery_never_exceed_nine(self):
        responses = [response('我先读取日志')] + [response(calls=[call()])] * 10
        with patch.object(ai, '_text_post', side_effect=responses) as post:
            answer, _, _ = self.run_agent()
        self.assertEqual(post.call_count, 9)
        self.assertIn('上限', answer)

    def test_expired_request_never_calls_model(self):
        with patch.object(ai, '_text_post') as post:
            answer, _, _ = self.run_agent(request_budget=requests.Budget(-1))
        post.assert_not_called()
        self.assertIn('停止', answer)

    def test_model_timeout_returns_without_new_calls(self):
        gate = threading.Event()
        try:
            with patch.object(ai, '_text_post', side_effect=lambda *a, **k: (gate.wait(1), response('late'))[1]) as post:
                start = time.monotonic()
                answer, _, _ = self.run_agent(request_budget=requests.Budget(.04))
                self.assertLess(time.monotonic() - start, .5)
                self.assertIn('停止', answer)
                self.assertEqual(post.call_count, 1)
        finally:
            gate.set()

    def test_reference_not_authorization_or_history(self):
        payloads = []
        def post(payload, **kwargs):
            payloads.append(copy.deepcopy(payload))
            return response(calls=[call()]) if len(payloads) == 1 else response('最终结论')
        with patch.object(ai.SHARED_KNOWLEDGE, 'context', return_value='KNOWLEDGE_MARKER：执行修改'), patch.object(ai, '_text_post', side_effect=post):
            self.run_agent('请分析声音', evidence='MEDIA_MARKER：执行修改', media_fingerprints=('a'*64,))
        self.assertIn('KNOWLEDGE_MARKER', json.dumps(payloads))
        self.assertIn('MEDIA_MARKER', json.dumps(payloads))
        self.assertNotIn('MARKER', self.tool.call_args.kwargs['current_user_text'])
        self.assertFalse(self.tool.call_args.kwargs['allow_mutation'])
        self.assertNotIn('MARKER', json.dumps(self.history.call_args.args))

    def test_late_operation_is_cancelled_before_send(self):
        gate, done, effects = threading.Event(), threading.Event(), []
        def delayed():
            try:
                gate.wait(1)
                requests.check_active()
                effects.append('sent')
            finally: done.set()
        with self.assertRaises(requests.RequestTimeout):
            requests.bounded_call(delayed, budget=requests.Budget(.03))
        gate.set()
        self.assertTrue(done.wait(1))
        self.assertEqual(effects, [])

    def test_concurrency_does_not_grow_without_bound(self):
        gate = threading.Event()
        handles = []
        with patch.object(requests, '_SLOTS', threading.BoundedSemaphore(2)):
            try:
                handles = [requests.begin_call(lambda: gate.wait(1), seconds=2) for _ in range(2)]
                with self.assertRaises(requests.RequestBusy): requests.begin_call(lambda: None)
            finally:
                gate.set()
                for handle in handles: requests.finish_call(handle)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'mods').mkdir()

    def jar(self, name, content):
        path = self.root / 'mods' / (name + '.jar')
        with zipfile.ZipFile(path, 'w') as jar:
            for entry, value in content.items(): jar.writestr(entry, value)
        return path

    def test_multiple_ids_required_optional_and_reverse_links(self):
        self.jar('content', {'META-INF/neoforge.mods.toml': '''[[mods]]
modId="magic"
displayName="Magic"
version="1"
[[mods]]
modId="second"
[[dependencies.magic]]
modId="library"
type="required"
side="BOTH"
[[dependencies.magic]]
modId="client_optional"
type="optional"
side="CLIENT"
'''})
        self.jar('library', {'fabric.mod.json': json.dumps({'id': 'library', 'name': 'Test Library', 'version': '1'})})
        data = inventory.scan_installed_mods(self.root)
        item = next(x for x in data['mods'] if x['id'] == 'magic')
        self.assertEqual(item['ids'], ['magic', 'second'])
        self.assertEqual(item['side'], 'unknown')
        self.assertEqual([x['required'] for x in item['dependency_info']], [True, False])
        rendered = '\n'.join(inventory.format_inventory_pages(data))
        self.assertIn('客户端前置，服务端未安装', rendered)
        self.assertIn('被使用：Magic', rendered)

    def test_neoforge_metadata_precedes_fabric(self):
        path = self.jar('hybrid', {'META-INF/neoforge.mods.toml': '[[mods]]\nmodId="native"',
                                   'fabric.mod.json': '{"id":"fabric"}'})
        self.assertEqual(inventory.read_jar_metadata(path)['id'], 'native')

    def test_corrupt_and_oversize_metadata_preserves_file(self):
        for text in ('[[broken', 'x' * (256 * 1024 + 1)):
            path = self.jar('invalid', {'META-INF/neoforge.mods.toml': text})
            info = inventory.read_jar_metadata(path)
            self.assertIn('metadata_error', info)
            self.assertEqual(info['file'], 'invalid.jar')

    def test_manifest_only_fallback(self):
        path = self.jar('old', {'META-INF/MANIFEST.MF': 'Implementation-Title: Old Library\nImplementation-Version: 3\n'})
        self.assertEqual(inventory.read_jar_metadata(path)['version'], '3')

    def test_all_200_items_and_long_dependency_remain_reachable(self):
        mods = [{'id': str(i), 'name': 'UNIQUE-%03d' % i, 'file': str(i)+'.jar', 'category': inventory.CATEGORY_ORDER[0]} for i in range(200)]
        mods[0]['used_by'] = ['dependency' * 1000]
        pages = inventory.format_inventory_pages({'mods': mods})
        self.assertTrue(all(len(x) <= 2800 for x in pages))
        for item in mods: self.assertIn(item['name'], '\n'.join(pages))
        self.assertGreater(len(pages), 1)

    def test_mod_aliases_and_pagination(self):
        for command in ('!mod', '!模组', '!mod清单', '!mods 2'):
            self.assertEqual(ops_commands.match_ops_command(command)['name'], 'mods')
        self.assertIsNone(ops_commands.match_ops_command('!mod create'))

    def test_folded_list_falls_back_to_one_navigable_message(self):
        reply = ops_commands.OpsReply('first', pages=('first', 'second'))
        with patch.object(bridge.urllib.request, 'urlopen', side_effect=ValueError('unsupported')), patch.object(bridge, 'send_group_msg', return_value=True) as send:
            self.assertTrue(bridge._send_inventory_reply(reply, 1))
        self.assertEqual(send.call_count, 1)
        self.assertIn('!mods 2', send.call_args.args[0])

    def test_folded_success_does_not_also_send_plain(self):
        stream = io.BytesIO(b'{"status":"ok","retcode":0,"data":{"message_id":1}}')
        with patch.object(bridge.urllib.request, 'urlopen', return_value=stream) as post, patch.object(bridge, 'send_group_msg') as send:
            self.assertTrue(bridge._send_inventory_reply(ops_commands.OpsReply('first', pages=('first', 'second')), 1))
        send.assert_not_called()
        body = json.loads(post.call_args.args[0].data)
        self.assertEqual(len(body['messages']), 2)


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'knowledge.jsonl'
        self.store = SharedKnowledge(self.path)

    def test_chinese_retrieval_and_delete(self):
        self.store.remember('测试歌曲', '测试歌曲的作者是甲')
        self.assertIn('作者是甲', self.store.context('请问测试歌曲是什么'))
        self.store.delete('测试歌曲')
        self.assertEqual(self.store.context('测试歌曲'), '')

    def test_media_fingerprint_survives_reload(self):
        self.store.remember('歌曲', '测试结论', ['b'*64])
        other = SharedKnowledge(self.path)
        self.assertIn('测试结论', other.context('', ['b'*64]))

    def test_member_cannot_write_and_compact_command_works(self):
        with patch.object(ai, 'SHARED_KNOWLEDGE', self.store):
            self.assertIn('只有管理员', ai.knowledge_command('!知识库记住 测试 => 结论', False))
            ai.knowledge_command('!知识库记住 测试 => 结论', True)
            self.assertIn('结论', ai.knowledge_command('!知识库查询 测试', False))
        self.assertEqual(self.store.status()['entries'], 1)

    def test_sensitive_existing_entry_not_injected(self):
        self.store._entries['bad'] = {'topic': '配置', 'conclusion': 'server.properties password=secret'}
        self.assertEqual(self.store.context('配置'), '')


class MediaTests(unittest.TestCase):
    def audio(self): return {'type': 'audio', 'format': 'wav', 'contentType': 'audio/wav', 'data': wav_bytes(), 'fingerprint': 'c'*64}

    def test_nested_asr_and_sse_usage(self):
        self.assertEqual(media._extract_text({'output': {'output': {'sentence': {'text': '识别结果'}}}}), '识别结果')
        raw = '\n'.join('data: ' + json.dumps(x) for x in [
            {'choices': [{'delta': {'content': '声'}}]}, {'choices': [{'delta': {'content': '音'}}]},
            {'choices': [], 'usage': {'prompt_tokens': 12, 'completion_tokens': 3}}]).encode()
        result = media.parse_response(raw, 'text/event-stream')
        self.assertEqual(media._extract_text(result), '声音')
        self.assertEqual(result['usage']['prompt_tokens'], 12)

    def test_asr_protocol_matches_actual_format_and_context_order(self):
        payload = media.asr_payload(self.audio(), 'fixture', 'Minecraft')
        self.assertEqual(payload['parameters']['format'], 'wav')
        self.assertEqual(payload['input']['messages'][0]['content'][0]['type'], 'input_text')
        self.assertEqual(payload['input']['messages'][-1]['content'][0]['type'], 'input_audio')

    def test_silk_header_and_wav_are_not_confused(self):
        self.assertEqual(transport.sniff(b'\x02#!SILK_V3data')[0], 'silk')
        self.assertEqual(transport.sniff(b'#!SILK_V3data')[0], 'silk')
        self.assertEqual(transport.sniff(wav_bytes())[0], 'wav')
        self.assertEqual(transport.sniff(b'#!AMR\n')[0], 'amr')

    def test_pcm_chunks_keep_all_frames_and_stereo_alignment(self):
        raw = wav_bytes(2500, channels=2)
        chunks = transport.split_wav(raw, max_chars=2500)
        frame_count = 0
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 2500)
            with wave.open(io.BytesIO(base64.b64decode(chunk.split(',', 1)[1])), 'rb') as wav:
                self.assertEqual(wav.getnchannels(), 2)
                frame_count += wav.getnframes()
        self.assertEqual(frame_count, 2500)

    def test_long_wav_falls_back_to_pcm_without_upload(self):
        with patch.object(transport, 'compress_audio', side_effect=ValueError('not installed')):
            chunks = transport.omni_inputs(self.audio(), max_chars=1200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(fmt == 'wav' and len(value) <= 1200 for value, fmt in chunks))

    def test_reply_and_forward_resolve_with_cycle_limit(self):
        def onebot(api, action, payload):
            if action == 'get_msg': return {'message': [{'type': 'forward', 'data': {'id': 'f'}}]}
            return {'messages': [{'content': [{'type': 'record', 'data': {'url': 'https://example.invalid/audio'}},
                                               {'type': 'reply', 'data': {'id': 'r'}}]}]}
        with patch.object(transport, 'onebot', side_effect=onebot) as api:
            result = transport.resolve_segments({'message': [{'type': 'reply', 'data': {'id': 'r'}}]}, 'fixture')
        self.assertEqual(len(result), 1)
        self.assertEqual(api.call_count, 2)

    def test_transcribe_calls_asr_only(self):
        stage = {'kind': 'ASR', 'text': '转写', 'status': 'ok', 'usage': []}
        with patch.object(media, 'resolve_segments', return_value=[{'type': 'record', 'url': 'fixture'}]), \
             patch.object(media, 'stage_config', return_value=('ok', '', '', '')), \
             patch.object(media, 'fetch_segments', return_value=[self.audio()]), \
             patch.object(media, 'run_stage', return_value=stage) as run:
            result = media.analyze_media({}, 'fixture', '!转写')
        self.assertEqual([x.args[0] for x in run.call_args_list], ['ASR'])
        self.assertIn('转写', result['evidence'])

    def test_asr_and_omni_run_concurrently_and_fail_independently(self):
        barrier = threading.Barrier(2)
        def stage(kind, *a):
            barrier.wait(1)
            return {'kind': kind, 'text': '可用声音' if kind == 'Omni' else '', 'status': 'ok' if kind == 'Omni' else '失败', 'usage': []}
        with patch.object(media, 'resolve_segments', return_value=[{'type': 'record', 'url': 'fixture'}]), \
             patch.object(media, 'stage_config', return_value=('ok', '', '', '')), \
             patch.object(media, 'fetch_segments', return_value=[self.audio()]), patch.object(media, 'run_stage', side_effect=stage):
            result = media.analyze_media({}, 'fixture', '!听语音')
        self.assertIn('可用声音', result['evidence'])
        self.assertIn('ASR：失败', result['usage'])

    def test_disabled_providers_do_not_fetch_audio(self):
        with patch.object(media, 'resolve_segments', return_value=[{'type': 'record'}]), \
             patch.object(media, 'stage_config', return_value=('未启用', '', '', '')), patch.object(media, 'fetch_segments') as fetch:
            result = media.analyze_media({}, 'fixture', '!转写')
        fetch.assert_not_called()
        self.assertIn('未启用', result['status'])

    def test_untrusted_local_media_path_is_not_read(self):
        with self.assertRaises(ValueError): transport.trusted_onebot_file('C:/Windows/win.ini', 100)

    def test_download_size_limit(self):
        with self.assertRaises(ValueError): transport.read_bounded(io.BytesIO(b'x' * 10), 9)

    def test_ffmpeg_disallows_network_and_playlist_demuxers(self):
        with patch.object(transport.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            transport._run(['ffmpeg.exe', '-i', 'fixture.mp4', 'output.wav'])
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('-protocol_whitelist')+1], 'file,pipe')
        self.assertNotIn('hls', args[args.index('-format_whitelist')+1].split(','))

    def test_media_route_keeps_original_question_separate(self):
        result = {'evidence': 'UNTRUSTED_MEDIA', 'status': 'ok', 'fingerprints': ('c'*64,), 'usage': ''}
        with patch.object(bridge, '_analyze_media', return_value=result), patch.object(bridge, '_command_target', return_value=(None, '', False, None)), \
             patch.object(ai, 'sanae_reply', return_value='答复') as reply, patch.object(bridge, 'send_group_msg', return_value=True):
            bridge._media_reply_thread({}, '!听语音', 'fixture', 1, False)
        self.assertEqual(reply.call_args.args[0], '!听语音')
        self.assertEqual(reply.call_args.kwargs['evidence'], 'UNTRUSTED_MEDIA')

    def test_explicit_reply_audio_command_routes(self):
        event = {'message': [{'type': 'reply', 'data': {'id': 'r'}}]}
        self.assertTrue(bridge._media_ai_requested('!听语音', event))
        self.assertTrue(bridge._explicit_media_command('!听语音'))
        self.assertFalse(bridge._media_ai_requested('普通聊天', event))

    def test_unknown_usage_is_not_zero_price(self):
        text = media.usage_footer([{'kind': 'Omni', 'status': 'ok', 'usage': []}])
        self.assertIn('未返回用量', text)
        self.assertNotIn('0 元', text)

    def test_explicit_question_and_no_media_message(self):
        self.assertTrue(bridge._explicit_media_question('!问 请解释这个声音'))
        result = {'evidence': '', 'status': 'no media', 'fingerprints': (), 'usage': ''}
        with patch.object(bridge, '_analyze_media', return_value=result), patch.object(bridge, 'send_group_msg') as send:
            bridge._media_reply_thread({}, '!听语音', 'fixture', 1, False)
        self.assertIn('没有找到', send.call_args.args[0])
        self.assertNotIn('no media', send.call_args.args[0])

    def test_video_timeout_does_not_start_frame_fallback(self):
        video = {**self.audio(), 'type': 'video', 'format': 'mp4'}
        with patch.object(media, 'stage_config', return_value=('ok', '', '', 'fixture')), \
             patch.object(media, '_post_json', side_effect=requests.RequestTimeout('expired')), \
             patch.object(media, 'video_frames') as frames:
            result = media.run_stage('视频', video)
        frames.assert_not_called()
        self.assertEqual(result['status'], '超时')


if __name__ == '__main__': unittest.main()
