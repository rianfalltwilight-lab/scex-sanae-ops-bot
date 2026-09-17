"""Application budgets in characters (input) and tokens (output), not model capacity."""
HISTORY_TURNS = 20
HISTORY_TTL = 2 * 60 * 60
HISTORY_CHARS = 64000
QUESTION_CHARS = 24000
SOURCE_CHARS = 96000
CHAT_ROWS = 1000
FORWARD_CALLS = 24
FORWARD_DEPTH = 6
ANSWER_TOKENS = 4096
SUMMARY_TOKENS = 8192
SOCIAL_TOKENS = 800
REQUEST_SECONDS = 240
MODEL_SECONDS = 150
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
