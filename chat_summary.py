"""Concise overview plus optional full timeline, from one evidence-bound model call."""
import json
import re

BRIEF_HEADER = '【简短总结】'
DETAIL_HEADER = '【完整时间线】'
BRIEF_CHARS = 240
BRIEF_TOKENS = 800


def brief_requested(text):
    text = str(text or '')
    # An explicit request for both versions must retain the timeline.
    if re.search(r'简短.*(?:和|加上|以及|同时).*(?:完整|详细|时间线)|(?:完整|详细|时间线).*(?:加上|以及|同时).*简短', text):
        return False
    if re.search(r'(?:不要|不用|无需)(?:再|太|过于)?(?:简短|简要|简版|短版|精简)', text):
        return False
    return bool(re.search(r'简短|简要|简版|短版|精简|简洁|只(?:要|说|看).{0,4}(?:重点|结论|概览)|一句话', text))


def brief_followup(text):
    """Only a standalone request to shorten the previous answer, not a new topic."""
    return bool(re.fullmatch(r'\s*(?:(?:太长了|太啰嗦了)[，,。！!\s]*)?(?:请)?(?:再)?(?:简短点|简短一点|简要点|精简一下|给(?:我)?(?:个|一个)?(?:简短版|简版|短版)|只说重点)[。！!？?\s]*',str(text or '')))


def compact(text, maximum=BRIEF_CHARS):
    text = str(text or '').strip()
    if len(text) <= maximum:
        return text
    head = text[:maximum-1]
    boundary = max(head.rfind('。'), head.rfind('！'), head.rfind('？'), head.rfind('\n'))
    return (head[:boundary+1] if boundary >= maximum//2 else head) + '…'


def instruction(short_only):
    return ('输出一个JSON对象，必须含字符串brief和timeline两个字段；不要输出JSON外的文字。'
            'brief是可独立阅读的简短总结，最多240个中文字符，优先80至180字、2至4个短要点；'
            '先给最终结论或当前状态，再补至多两个关键原因或待确认事项；不能只列题目。'
            '短版禁止逐人逐时序复述，不要罗列全部人名和时间戳；读者应能一眼看懂重点。'
            + ('用户只要短版：timeline必须是空字符串；不要附详细时间线。' if short_only else
               'timeline是完整版本，保留材料中的主要时间线、不同观点、证据和未确定之处；不要重复brief。'
               '仅按可见记录的先后梳理，原文没有时间戳时不得编造具体时刻；资料很少时简洁写清即可。'))


def render(content, short_only=False):
    value = str(content or '').strip()
    if value.startswith('```'):
        value = re.sub(r'^```(?:json)?\s*|\s*```$', '', value)
    data = json.loads(value)
    if not isinstance(data, dict) or not isinstance(data.get('brief'), str) or not data['brief'].strip():
        raise ValueError('summary missing brief')
    brief = compact(data['brief'])
    result = BRIEF_HEADER + '\n' + brief
    if not short_only:
        detail = data.get('timeline')
        if not isinstance(detail, str) or not detail.strip():
            raise ValueError('summary missing timeline')
        result += '\n\n' + DETAIL_HEADER + '\n' + detail.strip()
    return result


def previous_brief(history):
    if not history:
        return ''
    last = history[-1]
    text = str(last.get('content') or '')
    if last.get('role') != 'assistant' or not text.startswith(BRIEF_HEADER+'\n'):
        return ''
    brief = text.split('\n\n'+DETAIL_HEADER,1)[0].split('\n范围：',1)[0]
    scope = text.rsplit('\n范围：',1)
    return brief + ('\n范围：'+scope[-1] if len(scope)==2 else '')
