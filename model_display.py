"""Small QQ display cleanup for model prose; this is not a Markdown renderer."""
import re

_FENCE = re.compile(r'^[ ]{0,3}(`{3,}|~{3,})(.*)$')
_BOLD = re.compile(r'(?<![A-Za-z0-9_*\\])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![A-Za-z0-9_*])', re.S)
_OPEN_BOLD = re.compile(r'(?<![A-Za-z0-9_*\\])\*\*(?=\S)')
_TITLE = re.compile(r'(?m)^[ ]{0,3}#{1,6}[ \t]+')
_LIST = re.compile(r'^([ ]{0,3})[-*][ \t]+(?=\S)')
# Positive prose evidence, not a list of mathematical notations to recognise.
# Ambiguous single words, paths, code and formula-looking lines keep their sign.
_CJK_PROSE = re.compile(r'[\u3400-\u9fff][\u3400-\u9fffA-Za-z0-9 \t，。！？、；：：“”‘’《》〈〉【】…,.!?;:\'"]*')
_ENGLISH_PROSE = re.compile(r'[A-Za-z]{2,}(?:[ \t]+[A-Za-z]{2,})+[,.!?;:]?')


def _list_prose(line):
    marker = _LIST.match(line)
    if not marker:
        return line
    body = line[marker.end():].rstrip('\r\n')
    # Check original bold-list structure before stripping the paired markers.
    if _BOLD.match(body) or _CJK_PROSE.fullmatch(body) or _ENGLISH_PROSE.fullmatch(body):
        return marker[1] + line[marker.end():]
    return line


def _prose(text):
    """Protect exact inline code spans before changing prose punctuation."""
    codes, pieces, pos, complete = [], [], 0, True
    tokens = list(re.finditer(r'`+', text))
    index = 0
    while index < len(tokens):
        opening = tokens[index]
        closing = next((i for i in range(index + 1, len(tokens))
                        if tokens[i].group() == opening.group()), None)
        end = tokens[closing].end() if closing is not None else len(text)
        pieces.append(text[pos:opening.start()])
        marker = '\x00CODE' + str(len(codes)) + '\x00'
        pieces.append(marker)
        codes.append((marker, text[opening.start():end]))
        pos = end
        if closing is None:
            complete = False
            break
        index = closing + 1
    pieces.append(text[pos:])
    protected = ''.join(pieces)
    protected = ''.join(_list_prose(line) for line in protected.splitlines(keepends=True))
    protected = _BOLD.sub(r'\1', protected)
    complete = complete and not _OPEN_BOLD.search(protected)
    protected = _TITLE.sub('', protected)
    for marker, code in codes:
        protected = protected.replace(marker, code)
    return protected, complete


def _render(text):
    result, prose, fence = [], [], None
    complete = True

    def flush():
        nonlocal complete
        if prose:
            rendered, closed = _prose(''.join(prose))
            result.append(rendered)
            complete = complete and closed
            prose.clear()

    for line in text.splitlines(keepends=True):
        marker = _FENCE.match(line.rstrip('\r\n'))
        if fence:
            result.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
        elif marker:
            flush()
            result.append(line)
            fence = (marker[1][0], len(marker[1]))
        elif line.startswith(('    ', '\t')):
            flush()
            result.append(line)
        else:
            prose.append(line)
    flush()
    return ''.join(result), bool(complete and fence is None)


def plain_model_text(text):
    """Remove paired bold, list and title markers; leave code bytes untouched."""
    return _render(text)[0]


def ready_stream_text(text):
    """Delay an unfinished code/bold span instead of splitting its formatting."""
    rendered, complete = _render(text)
    return rendered if complete else None


def strip_reply_period(text):
    """Omit a bubble's final Chinese full stop without editing code or inner prose."""
    body = text.rstrip()
    if not body.endswith('。'):
        return text
    content = re.sub(r'^\[CQ:at,qq=\d+\] ?', '', body)
    if content.rsplit('\n', 1)[-1].startswith(('    ', '\t', '>')) or ready_stream_text(content) is None:
        return text
    return body.rstrip('。') + text[len(body):]
