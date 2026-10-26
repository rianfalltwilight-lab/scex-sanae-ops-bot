import io
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

import llbot_bridge as bridge
import sanae_ai as ai
import sanae_local_ops as local
from bridge_runtime import EventInbox, WorkQueue


class ManualExecutor:
    def __init__(self, capacity=128): self.items, self.capacity = [], capacity
    def submit(self, callback):
        if len(self.items) >= self.capacity: return False
        self.items.append(callback)
        return True


class IntakeTests(unittest.TestCase):
    def setUp(self):
        bridge._dup_ring.clear()
        self.jobs = ManualExecutor()
        self.inbox = EventInbox(executor=self.jobs)

    def event(self, mid=1, media=False):
        event = {'post_type':'message','message_type':'group','group_id':bridge.LLBOT_GROUP,
                 'user_id':'fixture-member','self_id':'99','message_id':mid,
                 'raw_message':'[CQ:at,qq=99] hello'}
        if media:
            event['message'] = [{'type':'at','data':{'qq':'99'}},
                                {'type':'record','data':{'url':'https://invalid.test/audio'}}]
        return event

    def post(self, event, raw=None, headers=None):
        body = json.dumps(event).encode() if raw is None else raw
        h = object.__new__(bridge.BridgeHandler)
        h.rfile = io.BytesIO(body)
        h.headers = headers or {'Content-Length':str(len(body))}
        h.server = types.SimpleNamespace(inbox=self.inbox)
        h.client_address = ('127.0.0.1',1)
        h._respond = Mock()
        with patch.object(bridge, 'CALLBACK_TOKEN', ''):
            h.do_POST()
        return h

    def test_ack_precedes_event_work(self):
        h = self.post(self.event())
        self.assertEqual(h._respond.call_args.args[0],200)
        self.assertEqual(len(self.jobs.items),1)

    def test_same_message_id_at_is_not_queued_twice(self):
        self.post(self.event()); self.post(self.event())
        self.assertEqual(len(self.jobs.items),1)

    def test_new_message_id_with_same_text_is_accepted(self):
        self.post(self.event(1)); self.post(self.event(2))
        self.assertEqual(len(self.jobs.items),2)

    def test_private_retransmission_is_deduplicated(self):
        event=self.event();event['message_type']='private'
        self.post(event);self.post(event)
        self.assertEqual(len(self.jobs.items),1)

    def test_full_inbox_returns_503_without_claiming_id(self):
        self.jobs.capacity=0
        h=self.post(self.event())
        self.assertEqual(h._respond.call_args.args[0],503)
        self.jobs.capacity=1
        self.post(self.event())
        self.assertEqual(len(self.jobs.items),1)

    def test_invalid_json_or_non_object_rejected(self):
        for body in (b'[]', b'broken', b'null'):
            self.assertEqual(self.post(None,raw=body)._respond.call_args.args[0],400)
        self.assertEqual(len(self.jobs.items),0)

    def test_malformed_or_short_body_rejected(self):
        for body, headers in [(b'x',{'Content-Length':'3'}),
                              (b'-1\r\n',{'Transfer-Encoding':'chunked'}),
                              (b'3\r\nab',{'Transfer-Encoding':'chunked'})]:
            self.assertEqual(self.post(None,raw=body,headers=headers)._respond.call_args.args[0],413)

    def test_valid_chunked_callback(self):
        body=json.dumps(self.event()).encode()
        chunked=(hex(len(body))[2:].encode()+b';fixture=1\r\n'+body+b'\r\n0\r\n\r\n')
        self.assertEqual(self.post(None,raw=chunked,headers={'Transfer-Encoding':'chunked'})._respond.call_args.args[0],200)

    def test_member_media_routes_without_name_error(self):
        event=self.event(media=True)
        h=object.__new__(bridge.BridgeHandler)
        empty=types.SimpleNamespace(append=lambda *a:None,observe=lambda *a:None,command=lambda *a,**k:None)
        with patch.multiple(bridge,SOCIAL_ARCHIVE=empty,
                            FEEDBACK_STORE=empty,STICKER_CATALOG=empty), \
             patch.object(bridge,'_start_ai_worker') as submit, \
             patch.object(bridge,'_selection_reply',return_value=None), \
             patch.object(bridge,'_binding_command',return_value=None), \
             patch.object(ai,'knowledge_command',return_value=None):
            h._process_event(event)
            self.assertEqual(submit.call_count,1)
            self.assertEqual(submit.call_args.args[0].__name__,'_media_reply_thread')

    def test_relay_switch_roundtrip(self):
        with tempfile.TemporaryDirectory() as td, patch.object(bridge,'CHAT_RELAY_STATE',str(Path(td)/'relay.json')):
            bridge._set_chat_relay(False,'fixture-admin')
            self.assertFalse(bridge._chat_relay_enabled())
            bridge._set_chat_relay(True,'fixture-admin')
            self.assertTrue(bridge._chat_relay_enabled())

    def test_private_forward_keeps_delivery_but_omits_body_from_logs(self):
        h=object.__new__(bridge.BridgeHandler)
        with patch.object(bridge,'OWNERS',{'fixture-owner'}),patch.object(bridge,'send_private_msg',return_value=True) as send,patch('builtins.print') as log:
            h._forward_private({'user_id':'fixture-member','raw_message':'SYNTHETIC_PRIVATE_SENTINEL'})
            self.assertIn('SYNTHETIC_PRIVATE_SENTINEL',send.call_args.args[1])
            self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL',str(log.call_args_list))

    def test_dedup_ttl_and_capacity_are_bounded(self):
        inbox=EventInbox(ttl=10,max_ids=2,executor=self.jobs)
        with patch('bridge_runtime.time.monotonic',return_value=0):
            for mid in range(3):self.assertEqual(inbox.submit(self.event(mid),lambda:None),'accepted')
        self.assertEqual(len(inbox._seen),2)
        with patch('bridge_runtime.time.monotonic',return_value=11):
            self.assertEqual(inbox.submit(self.event(2),lambda:None),'accepted')

    def test_worker_capacity_and_exception_isolation(self):
        work=WorkQueue(1,1,'fixture-worker'); gate=threading.Event(); active=threading.Event();done=threading.Event()
        def blocking():active.set();gate.wait(3)
        try:
            self.assertTrue(work.submit(blocking));self.assertTrue(active.wait(1))
            self.assertTrue(work.submit(lambda:done.set()))
            self.assertFalse(work.submit(lambda:None))
        finally:gate.set()
        self.assertTrue(done.wait(2))
        work.queue.join()
        self.assertTrue(work.submit(lambda:(_ for _ in ()).throw(ValueError('synthetic'))))
        work.queue.join();done.clear()
        self.assertTrue(work.submit(done.set));self.assertTrue(done.wait(1))


class LocalToolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ('a','b'):
            (self.root/name/'logs').mkdir(parents=True)
            (self.root/name/'mods').mkdir()
            (self.root/name/'logs/latest.log').write_text(name+'\npassword=fixture-secret\n',encoding='utf-8')
        self.servers={name:{'id':name,'prefix':'['+name+']','server_dir':str(self.root/name)} for name in ('a','b')}
        self.lookup=patch.object(local.server_registry,'resolve_server',side_effect=self.servers.get)
        self.lookup.start();self.addCleanup(self.lookup.stop)

    def test_reads_selected_canonical_root_and_redacts_secrets(self):
        for name in ('a','b'):
            result=local.execute_local_read('read_server_log',{}, {'id':name,'server_dir':'forged'},True)
            self.assertIn('\n'+name+'\n',result)
            self.assertNotIn('fixture-secret',result)

    def test_disabled_target_is_rejected(self):
        with self.assertRaises(ValueError):local.execute_local_read('read_server_log',{}, {'id':'removed'},True)

    def test_members_cannot_read_logs(self):
        self.assertIn('拒绝',local.execute_local_read('read_server_log',{}, {'id':'a'},False))

    def test_generic_file_tools_not_exposed_and_hard_rejected(self):
        names={t['function']['name'] for t in ai.TOOLS_ADMIN}
        self.assertFalse(names & local.DISABLED_FILE_TOOLS)
        for name in local.DISABLED_FILE_TOOLS:
            self.assertIn('已停用',ai.execute_tool(name,{},True,selected_server=self.servers['a'],allow_config_write=True))

    def test_ai_log_route_uses_local_reader(self):
        self.assertIn('\na\n',ai.execute_tool('read_server_log',{},True,selected_server={'id':'a'}))

    def test_incident_uses_local_telemetry_not_external_script(self):
        with patch.dict(os.environ,{'OPS_STATE_BASE':str(self.root/'ops-state')}):
            result=ai.execute_tool('incident_postmortem',{'window':'1h'},True,selected_server={'id':'a'})
        self.assertIn('[a]',result)
        self.assertTrue((self.root/'ops-state/a/postmortem-latest.json').is_file())

    def test_incident_rejects_invalid_window_and_member(self):
        self.assertIn('管理员',ai.execute_tool('incident_postmortem',{},False,selected_server={'id':'a'}))
        self.assertIn('window',ai.execute_tool('incident_postmortem',{'window':'forever'},True,selected_server={'id':'a'}))

    def test_tail_bounds_large_file(self):
        path=self.root/'a/logs/latest.log'
        path.write_bytes(b'old\n'*400000+b'new\n')
        self.assertEqual(local.tail(path,max_bytes=4096,lines=1),'new')

    def test_path_escape_is_rejected(self):
        with self.assertRaises(ValueError):local.inside(self.root/'a','../b/logs/latest.log')
