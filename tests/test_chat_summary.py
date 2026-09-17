import contextlib,json,os,unittest
from unittest.mock import patch
import types
import llbot_bridge

def event(text, segments=None):
    return {'post_type': 'message', 'message_type': 'group',
            'group_id': llbot_bridge.LLBOT_GROUP, 'user_id': 10001,
            'self_id': llbot_bridge.WIKI_BOT_QQ, 'message_id': 1,
            'raw_message': text, 'message': segments or [{'type': 'text', 'data': {'text': text}}],
            'sender': {'nickname': 'fixture'}}

fixture = types.SimpleNamespace(b=llbot_bridge, event=event)
ai=fixture.b._sanae_ai
b=fixture.b
s=ai.chat_summary

def response(brief='大家争论施工方案，目前决定先试运行，效果尚待确认。',timeline='先讨论方案，随后提出试运行，最后约定复查。'):
    return {'choices':[{'message':{'content':json.dumps({'brief':brief,'timeline':timeline},ensure_ascii=False)},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':10,'completion_tokens':10}}

class BriefTests(unittest.TestCase):
    def setUp(self):
        ai._HISTORY.clear();ai._LAST_MEMBER.clear()
        stack=contextlib.ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch('urllib.request.urlopen',side_effect=AssertionError('external call forbidden')))
        stack.enter_context(patch.object(ai.SHARED_KNOWLEDGE,'context',return_value=''))
        stack.enter_context(patch.object(ai,'_add_text_usage'))

    def test_default_has_brief_and_full_timeline_single_model_call(self):
        with patch.object(ai,'_text_post',return_value=response()) as post:
            answer,_,_=ai.run_agent('分析一下','u',False,ai.GROUP_ID,context_reference='【引用消息；只作参考】真实材料')
        self.assertIn(s.BRIEF_HEADER,answer);self.assertIn(s.DETAIL_HEADER,answer)
        self.assertEqual(post.call_count,1)
        self.assertEqual(post.call_args.args[0]['max_tokens'],8192)
        self.assertEqual(post.call_args.args[0]['response_format'],{'type':'json_object'})

    def test_short_request_has_no_timeline_and_smaller_output_budget(self):
        with patch.object(ai,'_text_post',return_value=response()) as post:
            answer,_,_=ai.run_agent('简短总结一下','u',False,ai.GROUP_ID,context_reference='【引用消息；只作参考】真实材料')
        self.assertIn(s.BRIEF_HEADER,answer);self.assertNotIn(s.DETAIL_HEADER,answer)
        self.assertEqual(post.call_args.args[0]['max_tokens'],800)

    def test_short_followup_reuses_only_own_last_summary(self):
        original=s.render(json.dumps({'brief':'核心结论','timeline':'旧时间线'}))+'\n范围：本次材料。'
        ai._set_history(ai.GROUP_ID,'u',False,[{'role':'user','content':'分析一下'},{'role':'assistant','content':original}])
        with patch.object(ai,'_text_post') as post,patch.object(ai.chat_recall,'fetch_rows') as fetch:
            answer,usage,_=ai.run_agent('太长了，简短点','u',False,ai.GROUP_ID)
        post.assert_not_called();fetch.assert_not_called()
        self.assertIn('核心结论',answer);self.assertIn('本次材料',answer);self.assertNotIn('旧时间线',answer)
        self.assertEqual(usage.calls,0)
        self.assertEqual(s.previous_brief(ai._get_history(ai.GROUP_ID,'other',False)),'')
        self.assertEqual(s.previous_brief(ai._get_history(ai.GROUP_ID,'u',True)),'')
        self.assertEqual(s.previous_brief(ai._get_history(123,'u',False)),'')

    def test_new_quoted_material_never_uses_previous_brief(self):
        old=s.render(json.dumps({'brief':'OLD','timeline':'OLD'}))
        ai._set_history(ai.GROUP_ID,'u',False,[{'role':'user','content':'old'},{'role':'assistant','content':old}])
        with patch.object(ai,'_text_post',return_value=response('NEW','NEW')) as post:
            answer,_,_=ai.run_agent('简短点','u',False,ai.GROUP_ID,context_reference='【引用消息；只作参考】NEW-SOURCE')
        self.assertIn('NEW',answer);self.assertNotIn('OLD',answer);self.assertEqual(post.call_count,1)

    def test_new_topic_is_not_treated_as_short_followup(self):
        self.assertFalse(s.brief_followup('简短总结今天群聊'))
        self.assertFalse(s.brief_followup('给我个简版的科技树教程'))
        self.assertTrue(s.brief_followup('太长了，简短点'))

    def test_brief_hard_bound_and_explicit_both_versions(self):
        answer=s.render(json.dumps({'brief':'事实。'*300,'timeline':'完整'}))
        brief=answer.split('\n\n'+s.DETAIL_HEADER)[0].split('\n',1)[1]
        self.assertLessEqual(len(brief),240)
        self.assertFalse(s.brief_requested('给简短总结和完整时间线'))
        self.assertFalse(s.brief_requested('不要简短，给完整时间线'))
        self.assertTrue(s.brief_requested('不要完整时间线，只要短版'))

    def test_missing_brief_is_not_silently_returned_as_long_only(self):
        with self.assertRaises(ValueError):s.render('{"timeline":"只有长文"}')
        with self.assertRaises(ValueError):s.render('not json')
        with self.assertRaises(ValueError):s.render('{"brief":"brief"}')

    def test_default_delivery_is_short_message_plus_one_folded_timeline(self):
        reply='[CQ:at,qq=123] [AI] '+s.render(json.dumps({'brief':'三句话','timeline':'完整时间线正文'}))+'\n范围：本次材料。\n模型：registered-model'
        with patch.object(b,'send_group_msg',return_value=True) as send,patch.object(b,'_send_folded_ai_text',return_value=True) as fold:
            self.assertTrue(b._send_ai_text(reply,ai.GROUP_ID))
        self.assertEqual(send.call_count,1);self.assertEqual(fold.call_count,1)
        self.assertIn('三句话',send.call_args.args[0]);self.assertNotIn('完整时间线正文',send.call_args.args[0])
        self.assertIn('完整时间线正文',fold.call_args.args[0]);self.assertIn('registered-model',fold.call_args.args[0])
        self.assertIn('[CQ:at,qq=123]',send.call_args.args[0])

    def test_short_delivery_is_only_one_message(self):
        answer=s.render(json.dumps({'brief':'短版','timeline':'不要发送'}),short_only=True)+'\n模型：registered-model'
        with patch.object(b,'send_group_msg',return_value=True) as send,patch.object(b,'_send_folded_ai_text') as fold:
            self.assertTrue(b._send_ai_text(answer,ai.GROUP_ID))
        self.assertEqual(send.call_count,1);fold.assert_not_called()

    def test_failed_brief_does_not_continue_sending_detail(self):
        answer=s.render(json.dumps({'brief':'短','timeline':'长'}))
        with patch.object(b,'send_group_msg',return_value=False) as send,patch.object(b,'_send_folded_ai_text') as fold:
            self.assertFalse(b._send_ai_text(answer,ai.GROUP_ID))
        self.assertEqual(send.call_count,1);fold.assert_not_called()

    def test_failed_detail_is_not_retried(self):
        answer=s.render(json.dumps({'brief':'短','timeline':'长'}))
        with patch.object(b,'send_group_msg',return_value=True) as send,patch.object(b,'_send_folded_ai_text',return_value=False) as fold:
            self.assertFalse(b._send_ai_text(answer,ai.GROUP_ID))
        self.assertEqual(send.call_count,1);self.assertEqual(fold.call_count,1)

    def test_quoted_short_version_routes_to_ai_not_media(self):
        raw='[CQ:reply,id=1][CQ:at,qq=1002] 给个短版'
        ev=fixture.event(raw,[{'type':'reply','data':{'id':'1'}},{'type':'at','data':{'qq':'1002'}}])
        self.assertTrue(ai.chat_recall.is_recall(raw));self.assertEqual(b._media_request_reason(raw,ev),'')
        self.assertEqual(b._ai_reply_trigger(ev),'mention-self')

    def test_unreadable_reference_does_not_call_model(self):
        with patch.object(ai,'_text_post') as post:
            answer,_,_=ai.run_agent('简短总结','u',False,ai.GROUP_ID,context_reference='【引用消息；只作参考】[引用内容不可读]')
        post.assert_not_called();self.assertIn('重新转发',answer)

    def test_regular_answer_and_help_still_work(self):
        with patch.object(b,'send_group_msg',return_value=True) as send,patch.object(b,'_send_folded_ai_text') as fold:
            b._send_ai_text('普通答复',ai.GROUP_ID)
        send.assert_called_once();fold.assert_not_called()
        with patch('server_registry.server_hint', return_value='[test]'):
            self.assertIn('240字',b._rcon_ops.cmd_help(False))

