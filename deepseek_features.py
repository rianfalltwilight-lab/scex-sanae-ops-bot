"""Small DeepSeek-only adapters; no credentials, model calls, or QQ side effects on import.

Official contracts checked 2026-10-17:
https://api-docs.deepseek.com/guides/tool_calls/
https://api-docs.deepseek.com/guides/thinking_mode/
https://api-docs.deepseek.com/api/create-chat-completion/
https://api-docs.deepseek.com/api/fetch/

Strict tools require https://api.deepseek.com/beta/chat/completions. Keep the
original schema for execution validation and strip_optional_nulls; removing
unsupported schema limits does not remove local argument-validation limits.
"""
from copy import deepcopy
import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request

from request_runtime import check_active as _check_active, remaining_timeout

FETCH_ENDPOINT = 'https://api.deepseek.com/v1/tools/fetch'
_UNSUPPORTED_LIMITS = ('minLength', 'maxLength', 'minItems', 'maxItems')


def _strict_schema(schema):
    result = deepcopy(schema)
    for key in _UNSUPPORTED_LIMITS:
        result.pop(key, None)
    for key in ('anyOf', 'oneOf', 'allOf'):
        if key in result:
            result[key] = [_strict_schema(item) for item in result[key]]
    if isinstance(result.get('items'), dict):
        result['items'] = _strict_schema(result['items'])
    for key in ('$defs', '$def', 'definitions'):
        if key in result:
            result[key] = {name: _strict_schema(value) for name, value in result[key].items()}
    if result.get('type') == 'object' or 'properties' in result:
        properties = result.get('properties', {})
        originally_required = set(result.get('required', []))
        converted = {}
        for name, value in properties.items():
            value = _strict_schema(value)
            if name not in originally_required:
                # A null optional field means "omitted" when executing with
                # the original schema. Never replace it with a guessed default.
                value = {'anyOf': [value, {'type': 'null'}]}
            converted[name] = value
        result.update(type='object', properties=converted, required=list(properties),
                      additionalProperties=False)
    return result


def strict_tools(tools):
    """Deep-copy function tools to the supported strict subset; caller selects /beta."""
    result = deepcopy(tools)
    for tool in result:
        if tool.get('type') != 'function':
            raise ValueError('strict_tools accepts function tools only')
        function = tool['function']
        function['strict'] = True
        function['parameters'] = _strict_schema(function.get('parameters', {'type': 'object'}))
    return result


def strip_optional_nulls(arguments, original_parameters):
    """Restore omitted-option semantics; preserve required nulls and false/0/empty string.

    Use the ORIGINAL parameters, not strict_tools' transformed schema. This is
    normalization, not validation or authorization.
    """
    if isinstance(arguments, dict):
        properties = original_parameters.get('properties', {})
        required = set(original_parameters.get('required', []))
        return {key: strip_optional_nulls(value, properties.get(key, {}))
                for key, value in arguments.items() if value is not None or key in required}
    if isinstance(arguments, list):
        item_schema = original_parameters.get('items', {})
        return [strip_optional_nulls(value, item_schema) for value in arguments]
    return arguments


def choose_effort(text, *, default='low'):
    """A routing policy, not an assertion about answer quality; punctuation alone is irrelevant."""
    if default not in {'low', 'high', 'max'}:
        raise ValueError('effort must be low, high, or max')
    value = str(text or '')
    explicit = re.search(r'(?:思考强度|推理强度|reasoning[_ ]effort|effort)\s*[:=：]?\s*(low|high|max)\b', value, re.I)
    if explicit:
        return explicit.group(1).lower()
    if re.search(r'快速回答|简单说|别想太久|不用深入|不要(?:深度|深入)思考', value):
        return 'low'
    if re.search(r'最高(?:思考|推理)强度|最大(?:思考|推理)强度|尽最大努力|用\s*max\s*(?:思考|推理)|严密证明|穷尽所有情况', value, re.I):
        return 'max'
    if re.search(r'深入分析|深度思考|认真推导|逐步推导|完整证明|详细论证', value):
        return 'high'
    technical = re.findall(r'根因|并发|竞态|事务|死锁|架构|一致性|性能|算法|复杂度|内存|源码|迁移|回滚', value)
    if len(set(technical)) >= 2 and re.search(r'分析|设计|比较|排查|推导|审查|评估', value):
        return 'high'
    if len(value) > 4000 and re.search(r'分析|推导|论证|比较|评估', value):
        return 'high'
    return default


class StreamError(RuntimeError):
    """A failed/incomplete stream; partial is internal model state, never a chat message."""
    def __init__(self, message, partial):
        super().__init__(message)
        self.partial = partial


_COMPLETE_CONTENT = re.compile(r'.+?(?:[。！？!?]+[”’"」』）)]*|(?<!\d)\.(?=\s|$)|\n+)', re.S)


def collect_sse(response, on_content=None, check_active=None):
    """Collect a line-iterable HTTP response and always close it.

    Callbacks receive only completed content sentences/lines, never reasoning or
    tool arguments. The final unpunctuated tail stays in _stream.pending_content
    and in message.content for the caller's final delivery. No automatic retry.
    """
    active = check_active or _check_active
    content, reasoning, calls, metadata = [], [], {}, {}
    usage, finish, buffer, data_lines = None, None, '', []
    done, delivered = False, 0

    def snapshot():
        message = {'role': 'assistant', 'content': ''.join(content)}
        if reasoning:
            message['reasoning_content'] = ''.join(reasoning)
        if calls:
            message['tool_calls'] = [deepcopy(calls[index]) for index in sorted(calls)]
        result = {**metadata, 'object': 'chat.completion', 'choices': [
            {'index': 0, 'message': message, 'finish_reason': finish}],
            '_stream': {'done': done, 'delivered_content_chars': delivered, 'pending_content': buffer}}
        if usage is not None:
            result['usage'] = deepcopy(usage)
        return result

    def consume_event():
        nonlocal usage, finish, buffer, done, delivered
        if not data_lines:
            return
        data = '\n'.join(data_lines)
        data_lines.clear()
        if data.strip() == '[DONE]':
            done = True
            return
        chunk = json.loads(data)
        if chunk.get('error'):
            raise ValueError('DeepSeek stream reported an error')
        for key in ('id', 'model', 'created', 'system_fingerprint'):
            if key in chunk:
                metadata[key] = chunk[key]
        if isinstance(chunk.get('usage'), dict):
            usage = chunk['usage']
        for choice in chunk.get('choices', []):
            if choice.get('index', 0) != 0:
                continue
            delta = choice.get('delta') or {}
            if isinstance(delta.get('reasoning_content'), str):
                reasoning.append(delta['reasoning_content'])
            if isinstance(delta.get('content'), str):
                part = delta['content']
                content.append(part)
                buffer += part
                if on_content:
                    while True:
                        match = _COMPLETE_CONTENT.match(buffer)
                        if not match:
                            break
                        segment = match.group()
                        active()
                        on_content(segment)
                        delivered += len(segment)
                        buffer = buffer[len(segment):]
            for call in delta.get('tool_calls') or []:
                index = call['index']
                merged = calls.setdefault(index, {'id': '', 'type': 'function',
                    'function': {'name': '', 'arguments': ''}})
                if call.get('id'):
                    merged['id'] += call['id']
                if call.get('type'):
                    merged['type'] = call['type']
                function = call.get('function') or {}
                for key in ('name', 'arguments'):
                    if isinstance(function.get(key), str):
                        merged['function'][key] += function[key]
            if choice.get('finish_reason') is not None:
                finish = choice['finish_reason']

    try:
        for raw_line in response:
            active()
            line = raw_line.decode('utf-8') if isinstance(raw_line, bytes) else raw_line
            line = line.lstrip('\ufeff').rstrip('\r\n')
            if not line:
                consume_event()
                if done:
                    break
            elif line.startswith('data:'):
                data_lines.append(line[5:].lstrip(' '))
        if not done:
            consume_event()
        if not done or finish is None:
            raise ValueError('DeepSeek stream ended before a complete finish and [DONE]')
        if finish not in {'stop', 'tool_calls', 'length'}:
            raise ValueError('DeepSeek stream finished abnormally: ' + str(finish))
        return snapshot()
    except Exception as exc:
        raise StreamError('DeepSeek stream failed: ' + type(exc).__name__, snapshot()) from exc
    finally:
        response.close()


def stream_post(url, key, payload, timeout=90, on_content=None, opener=None, check_active=None):
    """DeepSeek chat transport; uses the existing request-runtime deadline."""
    if url not in {'https://api.deepseek.com/chat/completions',
                   'https://api.deepseek.com/beta/chat/completions'}:
        raise ValueError('stream_post requires an official DeepSeek chat endpoint')
    body = deepcopy(payload)
    body['stream'] = True
    body['stream_options'] = {**(body.get('stream_options') or {}), 'include_usage': True}
    request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json; charset=utf-8', 'Accept': 'text/event-stream',
                 'Authorization': 'Bearer ' + key}, method='POST')
    active = check_active or _check_active
    active()
    response = (opener or urllib.request.urlopen)(request, timeout=remaining_timeout(timeout))
    return collect_sse(response, on_content=on_content, check_active=active)


def _public_url(url, resolver):
    if not isinstance(url, str) or not url or len(url) > 8192 or any(ord(c) <= 32 for c in url):
        raise ValueError('Fetch URL must be a bounded HTTP(S) URL without whitespace')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError('Fetch URL must have a public host and no credentials')
    host = parsed.hostname.encode('idna').decode('ascii').lower().rstrip('.')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    if '%' in host or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home', '.home.arpa')):
        raise ValueError('Fetch does not accept local hosts')
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        if '.' not in host or host.rsplit('.', 1)[-1].isdigit():
            raise ValueError('Fetch requires a public DNS name')
        # The official Fetch service resolves the target, not this machine.
        # Local DNS can legitimately return a TUN FakeIP. An injected resolver
        # is an optional extra local check, never evidence of server-side DNS.
        if resolver is None:
            addresses = None
        else:
            try:
                answers = resolver(host, port, type=socket.SOCK_STREAM)
                addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
            except (OSError, ValueError) as exc:
                raise ValueError('Fetch host failed the optional local DNS check') from exc
    if addresses is not None and (not addresses or any(not address.is_global for address in addresses)):
        raise ValueError('Fetch does not accept private, loopback, or reserved addresses')
    netloc = '[' + host + ']' if ':' in host else host
    if parsed.port is not None:
        netloc += ':' + str(parsed.port)
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, '')), host


def official_fetch(url, *, post, key, timeout=20, max_chars=24000, resolver=None):
    """Fetch Markdown via the fixed DeepSeek API, never directly from the target URL.

    Default checks reject explicit nonpublic IPs and internal hostname forms;
    no local DNS lookup is made. The Fetch service enforces its own resolution
    policy. resolver optionally adds a local DNS check, which is not evidence
    of the service's resolved address. Markdown and redirects remain untrusted.
    """
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 96000:
        raise ValueError('max_chars must be an integer from 1 to 96000')
    _check_active()
    source, host = _public_url(url, resolver)
    result = post(FETCH_ENDPOINT, key, {'url': source}, timeout=remaining_timeout(timeout))
    if not isinstance(result, dict) or not isinstance(result.get('content'), str) or not isinstance(result.get('title'), str):
        raise ValueError('DeepSeek Fetch returned an invalid response')
    return {'url': source, 'source_host': host, 'title': result['title'][:240],
            'content': result['content'][:max_chars], 'truncated': len(result['content']) > max_chars}
