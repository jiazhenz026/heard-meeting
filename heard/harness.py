"""The harness: the plain code between `say` and text-to-speech.

The model does not get to decide that it has something to say. It names a
reason, and the runtime checks the reason against state:

    asked    the classifier flagged a direct address in the last N seconds,
             and no reply has gone out for it
    finding  refs names a finished task whose result has not been spoken

Then a budget (one unsolicited line per 90 s), a sentence cap, and the floor:
speech waits for a gap in the transcript stream and is dropped to a card
highlight if none opens. Every layer fails towards silence.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .config import CONFIG, Config
from .contracts import now
from .store import Store

log = logging.getLogger("heard.harness")

REASONS = ("asked", "finding")


class Speaks(Protocol):
    async def speak(self, text: str, target: str = "line") -> None: ...


@dataclass(slots=True)
class Verdict:
    accepted: bool
    why: str
    text: str = ""


class Harness:
    def __init__(self, store: Store, voice: Speaks | None, *, config: Config = CONFIG) -> None:
        self.store = store
        self.voice = voice
        self.config = config
        self._last_unsolicited_at: float = 0.0
        self._floor_lock = asyncio.Lock()
        self.rejections: list[dict[str, Any]] = []

    # -- the check ---------------------------------------------------------

    def check(self, text: str, reason: str, refs: list[str]) -> Verdict:
        text = _tidy(text, self.config.max_sentences)
        if not text:
            return Verdict(False, "empty line")
        if reason not in REASONS:
            return Verdict(False, f"reason must be one of {REASONS}")
        if reason == "asked":
            a = self.store.asked
            if a is None or a.replied:
                return Verdict(False, "nobody asked: no open direct address")
            if now() - a.at > self.config.asked_window_s:
                return Verdict(False, f"the question was {now() - a.at:.0f}s ago; the moment passed")
            return Verdict(True, "asked", text)
        # finding
        task = None
        for ref in refs:
            t = self.store.tasks.get(ref)
            if t is not None:
                task = t
                break
        if task is None:
            return Verdict(False, "finding requires refs naming a task id")
        if task.status != "DONE":
            return Verdict(False, f"task {task.id} is {task.status}, not DONE")
        if task.spoken:
            return Verdict(False, f"task {task.id} was already spoken")
        gap = now() - self._last_unsolicited_at
        if gap < self.config.unsolicited_gap_s:
            return Verdict(False, f"budget: one unsolicited line per {self.config.unsolicited_gap_s:.0f}s; "
                                  f"{self.config.unsolicited_gap_s - gap:.0f}s to go")
        return Verdict(True, "finding", text)

    # -- the door ----------------------------------------------------------

    async def say(self, text: str, reason: str, refs: list[str] | None = None) -> Verdict:
        refs = refs or []
        v = self.check(text, reason, refs)
        if not v.accepted:
            self.rejections.append({"at": now(), "text": text, "reason": reason, "why": v.why})
            self.store.log_event("rejected", f"[{reason}] {text} — {v.why}")
            log.info("say rejected (%s): %s", v.why, text)
            return v
        # Commit the budget and the flags now, before the floor: a second
        # call arriving while this one waits must see the door closed.
        if reason == "asked" and self.store.asked is not None:
            self.store.asked.replied = True
        if reason == "finding":
            self._last_unsolicited_at = now()
            for ref in refs:
                t = self.store.tasks.get(ref)
                if t is not None:
                    t.spoken = True
        evidence = []
        for ref in refs:
            t = self.store.tasks.get(ref)
            if t is not None:
                evidence.extend(t.sources[:4])
        asyncio.create_task(self._floor(v.text, reason, refs, evidence))
        return v

    async def _floor(self, text: str, reason: str, refs: list[str], evidence: list[str]) -> None:
        async with self._floor_lock:
            started = now()
            while True:
                quiet = now() - self.store.last_speech_at
                if quiet >= self.config.floor_gap_s:
                    break
                if now() - started > self.config.floor_timeout_s:
                    self.store.record_said(text, reason, refs, evidence, delivered=False)
                    self._highlight(refs, text)
                    log.info("floor never opened; dropped to the card: %s", text)
                    return
                await asyncio.sleep(0.15)
            self.store.record_said(text, reason, refs, evidence, delivered=True)
            if self.voice is not None:
                await self.voice.speak(text, "line")
            log.info("[say/%s] %s", reason, text)

    def _highlight(self, refs: list[str], text: str) -> None:
        for ref in refs:
            t = self.store.tasks.get(ref)
            if t is not None and t.card_id in self.store.cards:
                self.store.cards[t.card_id].highlight = text
                self.store.touch()
                return


_SENT = re.compile(r"(?<=[.!?])\s+")


_MAX_WORDS = 45


def _tidy(text: str, max_sentences: int) -> str:
    text = " ".join((text or "").split())
    if not text:
        return ""
    parts = _SENT.split(text)
    out: list[str] = []
    for part in parts[:max_sentences]:
        if sum(len(x.split()) for x in out) + len(part.split()) > _MAX_WORDS and out:
            break
        out.append(part)
    text = " ".join(out).strip()
    words = text.split()
    if len(words) > _MAX_WORDS:
        text = " ".join(words[:_MAX_WORDS]).rstrip(",;:") + "."
    return text
