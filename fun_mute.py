"""AI-judged joke mutes for the social group.

The bridge feeds every text message of the configured group in. Messages from
ordinary members are batched; once the chat pauses (or a batch has waited long
enough) a model decides which of them, if any, clearly deserve a joke mute,
with the earlier chat as context. No keyword or pattern rule decides a mute.

The bridge keeps the hard limits: who may be muted, duration bounds, per-member
cooldown, an hourly cap, and that the bot really is a group admin. It also asks
the model for the notice text; a decision without a usable notice is dropped.
"""
from __future__ import annotations

import itertools
import re
import threading
import time
from collections import deque

MIN_SECONDS = 60
SELF_MAX_SECONDS = 3600      # a member may ask for up to an hour
OTHER_MAX_SECONDS = 900      # anything the model imposes stays short
NOTICE_MAX_CHARS = 60

_CQ = re.compile(r"\[CQ:[^\]]*\]")
_AT = re.compile(r"^@\S+\s*")


def clamp(seconds, scene: str = "self") -> int:
    try:
        value = int(float(seconds))
    except (TypeError, ValueError):
        value = 300
    high = SELF_MAX_SECONDS if scene == "self" else OTHER_MAX_SECONDS
    return max(MIN_SECONDS, min(high, value))


def duration_text(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}小时"
    if seconds % 60 == 0:
        return f"{seconds // 60}分钟"
    return f"{seconds}秒"


def clean_notice(text, rejects=None) -> str:
    """First line of the model's notice, without CQ codes, quotes or a leading @.

    Returns '' when nothing usable remains, when it is too long, or when the
    optional rejects(text) check flags it (e.g. leaked tool-call markup).
    """
    text = _CQ.sub("", str(text or ""))
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    line = _AT.sub("", line).strip().strip("\"'“”‘’「」『』").strip()
    if not line or "[SILENT]" in line.upper() or len(line) > NOTICE_MAX_CHARS:
        return ""
    if rejects is not None and rejects(line):
        return ""
    return line


class FunMuteJudge:
    """Batch messages and hand them to judge_fn; apply_fn acts on each decision.

    judge_fn(context, batch) -> [{"id", "scene", "seconds", "reason"}]
    apply_fn(item, decision) -> None
    Both run on the judge's own thread, never on the event intake thread.
    """

    def __init__(self, judge_fn, apply_fn, *, quiet=3.0, max_wait=10.0, context_size=20,
                 batch_size=12, max_pending=36, clock=time.monotonic, log=print, label="fun-mute"):
        self._judge, self._apply, self._log = judge_fn, apply_fn, log
        self.label = str(label)
        self.quiet, self.max_wait = float(quiet), float(max_wait)
        self.context_size, self.batch_size = int(context_size), int(batch_size)
        self.max_pending = int(max_pending)
        self._clock = clock
        self._seq = itertools.count()
        self._timeline = deque(maxlen=self.context_size + self.max_pending + 8)
        self._pending = []
        self._first = self._last = None
        self._cond = threading.Condition()
        self._thread = None

    @staticmethod
    def _public(row, judged=False):
        out = {key: row[key] for key in ("id", "sender", "name", "time", "text") if key in row}
        if judged:
            out["addressed_bot"] = bool(row.get("addressed_bot"))
            out["mentions_others"] = bool(row.get("mentions_others"))
        return out

    def observe(self, item, eligible=True):
        """Record one message; eligible ones wait for the next judgment."""
        row = dict(item, _seq=next(self._seq))
        with self._cond:
            self._timeline.append(row)
            if not eligible:
                return
            now = self._clock()
            self._pending.append(row)
            if len(self._pending) > self.max_pending:
                dropped = len(self._pending) - self.max_pending
                del self._pending[:dropped]
                self._log(f"[{self.label}] backlog: dropped {dropped} unjudged messages")
            self._first = now if self._first is None else self._first
            self._last = now
            self._cond.notify()

    def pending(self):
        with self._cond:
            return len(self._pending)

    def _wait_seconds(self, now):
        return min(self.quiet - (now - self._last), self.max_wait - (now - self._first))

    def process_once(self, force=False):
        """Judge one batch if due (or forced); returns the number of messages judged."""
        with self._cond:
            if not self._pending:
                return 0
            now = self._clock()
            if not force and self._wait_seconds(now) > 0:
                return 0
            batch = self._pending[:self.batch_size]
            self._pending = self._pending[self.batch_size:]
            self._first = self._last = (now if self._pending else None)
            first = batch[0]["_seq"]
            context = [self._public(row) for row in self._timeline if row["_seq"] < first]
            context = context[-self.context_size:]
        decisions = self._judge(context, [self._public(row, judged=True) for row in batch]) or []
        by_id = {row["id"]: row for row in batch}
        for decision in decisions:
            item = by_id.get(str(decision.get("id") or ""))
            if item is not None:
                self._apply(item, decision)
        return len(batch)

    def _loop(self):
        while True:
            with self._cond:
                while not self._pending:
                    self._cond.wait()
                wait = self._wait_seconds(self._clock())
                if wait > 0:
                    self._cond.wait(wait)
                    continue
            try:
                self.process_once(force=True)
            except Exception as exc:  # one bad batch must not stop the judge
                self._log(f"[{self.label}] judge failed: {type(exc).__name__}")

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name=f"{self.label}-judge", daemon=True)
            self._thread.start()
        return self._thread
