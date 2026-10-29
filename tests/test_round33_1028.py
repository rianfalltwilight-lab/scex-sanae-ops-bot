"""Round 33 (10-28 afternoon review): chat replies stay out of questions to someone else, the
research snapshot ignores leftover temp save files, and the "正在处理" notice waits 20 s. Offline."""
import base64
import gzip
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import test_yachiyo_runtime as runtime_tests
import remote_server_reader
from social_lite import SocialLite

s = runtime_tests.s


class AddressedElsewhereTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.lite = SocialLite(Path(temp.name) / 'state.json', Path(temp.name) / 'activity.log', s.SANAE_QQ,
                               ambient_reply_weight=1.0)

    def score(self, raw):
        return self.lite._score({'group_id': 1, 'user_id': 2002, 'raw_message': raw, 'time': 1},
                                self.lite._group(1))

    def test_a_question_to_someone_else_is_left_alone(self):
        # 10-28 14:17 "@Alice 上次要加啥mod来着？" -> "最近这几十条里没见提过…"
        self.assertEqual((0, 'addressed-elsewhere'), self.score('[CQ:at,qq=1574074286] 上次要加啥mod来着？'))
        self.assertIn('question', self.score('上次要加啥mod来着？')[1])
        direct = self.score(f'[CQ:at,qq=1574074286] [CQ:at,qq={s.SANAE_QQ}] 你们知道吗？')
        self.assertIn('direct', direct[1])
        self.assertIn('direct', self.score('[CQ:at,qq=1574074286] 早苗你知道吗？')[1])


def nbt_player(research):
    def name(text):
        data = text.encode()
        return struct.pack('>H', len(data)) + data
    inner = b''.join(b'\x01' + name(key) + b'\x01' for key in research) + b'\x00'
    body = (b'\x0a' + name('NeoForgeData') + b'\x0a' + name('ThaumcraftResearch') + inner
            + b'\x03' + name('ThaumcraftWarp') + struct.pack('>i', 2) + b'\x00' + b'\x00')
    return gzip.compress(b'\x0a' + name('') + body)


class ResearchSnapshotTests(unittest.TestCase):
    def test_leftover_temp_saves_are_not_counted(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name) / 'world' / 'playerdata'
        folder.mkdir(parents=True)
        uuid = '96e5dac8-01a4-3e77-918b-2371ee33d935'
        (folder / f'{uuid}.dat').write_bytes(nbt_player(['basics', 'alumentum']))
        (folder / f'{uuid}-14321109007495561602.dat').write_bytes(nbt_player(['basics'])[:40])   # truncated temp copy
        (folder / f'{uuid}.dat_old').write_bytes(b'old')
        script = remote_server_reader._RESEARCH_SCRIPT.replace(
            '__CONFIG__', base64.b64encode(json.dumps({'root': temp.name}).encode()).decode())
        out = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout)
        self.assertEqual(0, data['errors'])
        self.assertEqual(['alumentum', 'basics'], data['players'][uuid]['research'])
        self.assertEqual([2, 0, 0], data['players'][uuid]['warp'])
        (folder / '00000000-0000-0000-0000-000000000001.dat').write_bytes(b'not gzip')
        data = json.loads(subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=60).stdout)
        self.assertEqual(1, data['errors'])        # a real player file that cannot be read still counts


class InterimTests(unittest.TestCase):
    def test_default_delay(self):
        self.assertEqual(20.0, s.INTERIM_DELAY_SECONDS)


def chat_rows():
    people = [('2011', 'Alice', 'member'), ('2003', 'FixtureMember', 'member'),
              ('2012', 'FixtureOther', 'member'), ('2002', 'FixtureAdmin', 'admin')]
    rows = []
    for i in range(40):
        uid, card, role = people[i % 4]
        rows.append({'group_id': 1001, 'user_id': int(uid), 'time': 1793100000 + i, 'message_id': i,
                     'sender': {'card': card, 'role': role},
                     'message': [{'type': 'text', 'data': {'text': f'{card} 的第 {i} 条：星辉的ae附属' if uid == '2011' else f'闲聊 {i}'}}]})
    return rows


class NamedRecallTests(unittest.TestCase):
    def test_a_named_member_narrows_the_sample(self):
        import chat_recall
        ask = '早苗翻一下聊天记录,alice要求加什么mod'
        sample = chat_recall.evidence(chat_rows(), 1001, '2002', ask, now=1793100100)
        self.assertEqual('2011', sample['stats']['target'])
        self.assertEqual(10, sample['stats']['sample_messages'])
        self.assertIn('只含问题点名的 QQ 2011（名片 Alice）', sample['context'])
        self.assertEqual('2012', chat_recall.evidence(chat_rows(), 1001, '2002', '翻一下聊天记录 FixtureOther说了啥',
                                                           now=1793100100)['stats']['target'])
        for text in ('早苗总结一下群聊', '翻一下聊天记录，大家说服务器卡吗', '翻一下聊天记录 FixtureAdmin 说了啥'):
            with self.subTest(text=text):
                self.assertFalse(chat_recall.evidence(chat_rows(), 1001, '2002', text,
                                                      now=1793100100)['stats'].get('target'))

    def test_a_specific_question_about_one_person_gets_the_short_answer(self):
        from unittest import mock
        import time as time_module
        sent = []

        def model(payload, **kwargs):
            sent.append(payload)
            return {'choices': [{'message': {'content': json.dumps({'brief': {'conclusion': '星辉的ae附属', 'points': []}},
                                                                    ensure_ascii=False)}}]}
        with mock.patch.object(s.qq_chat_reader, 'fetch_group_rows', return_value=(chat_rows(), {})), \
                mock.patch.object(s, '_text_post', side_effect=model), mock.patch.object(s, '_add_text_usage'), \
                mock.patch.object(s.chat_recall.time, 'time', return_value=1793100100):
            s._recall_reply('早苗翻一下聊天记录,alice要求加什么mod', '2002', 1001, s.Budget(60))
            s._recall_reply('早苗总结一下群聊', '2002', 1001, s.Budget(60))
        self.assertIn(s.chat_summary.instruction(True), sent[0]['messages'][0]['content'])
        self.assertIn(s.chat_summary.instruction(False), sent[1]['messages'][0]['content'])
        del time_module


if __name__ == '__main__':
    unittest.main()
