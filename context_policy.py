"""Application budgets in characters (input) and tokens (output), not model capacity."""
HISTORY_TURNS = 40
HISTORY_TTL = 6 * 60 * 60
HISTORY_CHARS = 128000
CHAT_HISTORY_TURNS = 12
CHAT_HISTORY_TTL = 2 * 60 * 60
CHAT_HISTORY_CHARS = 18000
QUESTION_CHARS = 24000
SOURCE_CHARS = 96000
CHAT_ROWS = 1000
FORWARD_CALLS = 24
FORWARD_DEPTH = 6
ANSWER_TOKENS = 8192
SUMMARY_TOKENS = 12288
SOCIAL_TOKENS = 4096
SERIOUS_TOKENS = 32768
LONG_QUESTION_CHARS = 180000
LONG_SOURCE_CHARS = 240000
LONG_TOKENS = 65536
MAX_TOKENS = 65536
REQUEST_SECONDS = 240
MODEL_SECONDS = 150
SERIOUS_REQUEST_SECONDS = 480
SERIOUS_MODEL_SECONDS = 300
LONG_REQUEST_SECONDS = 900
LONG_MODEL_SECONDS = 600
SOCIAL_REQUEST_SECONDS = 120
SOCIAL_MODEL_SECONDS = 90
READ_SECONDS = 30


def clip(text, limit):
    text = str(text or '')
    if len(text) <= limit:
        return text
    note = '\n[内容超过本次容量，后文截断]'
    return text[:max(0, limit-len(note))] + note


def history_tail(messages):
    """Keep complete recent turns; never leave an orphan assistant response."""
    pairs, used = [], 0
    for end in range(len(messages), 1, -2):
        pair = messages[end-2:end]
        size = sum(len(str(row.get('content', ''))) for row in pair)
        if used + size > HISTORY_CHARS or len(pairs) >= HISTORY_TURNS:
            break
        pairs.append([dict(row) for row in pair])
        used += size
    if not pairs and len(messages) == 1:
        return [dict(messages[0], content=clip(messages[0].get('content'), QUESTION_CHARS))]
    return [row for pair in reversed(pairs) for row in pair]


def chat_history_tail(messages):
    """Keep recent pairs, with explicitly marked verbatim excerpts at the boundary."""
    marker = '\n[旧对话原文摘录；中间省略，不是完整原话]\n'

    def excerpt(text, budget):
        text = str(text or '')
        if len(text) <= budget:
            return text
        # Re-clipping an excerpt keeps one omission marker, not nested markers.
        text = text.replace(marker, '')
        available = budget - len(marker)
        head = available // 2
        return text[:head] + marker + text[-(available-head):]

    pairs, used = [], 0
    for end in range(len(messages), 1, -2):
        if len(pairs) >= CHAT_HISTORY_TURNS:
            break
        pair = [dict(row) for row in messages[end-2:end]]
        texts = [str(row.get('content', '')) for row in pair]
        size = sum(map(len, texts))
        remaining = CHAT_HISTORY_CHARS - used
        if size > remaining:
            if remaining < 256:
                break
            allowance = min(remaining, 1200 if pairs else CHAT_HISTORY_CHARS // 2)
            # Give unused space from a short side to the longer side.
            budgets = [allowance // 2, allowance - allowance // 2]
            for index in range(2):
                spare = max(0, budgets[index] - len(texts[index]))
                budgets[index] -= spare
                budgets[1-index] += spare
            for row, text, budget in zip(pair, texts, budgets):
                row['content'] = excerpt(text, budget)
            size = sum(len(row['content']) for row in pair)
        pairs.append(pair)
        used += size
    return [row for pair in reversed(pairs) for row in pair]
