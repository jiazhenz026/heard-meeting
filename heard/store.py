"""The one store: transcript, notes, cards, tasks, said lines, log.

Two write disciplines. `transcript.md` is append-only and written by the
Scribe alone, so it needs no lock. Everything else is a plain in-memory record
mutated from the one asyncio loop; every mutation ends in `touch()`, which
schedules a board push.

The Scribe lives here too: `commit()` is the whole of it. No model, ~20 lines.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .contracts import mint_id, now

log = logging.getLogger("heard.store")

CardStatus = str  # PLACEHOLDER · INVESTIGATING · READY
TaskStatus = str  # RUNNING · DONE · FAILED


@dataclass(slots=True)
class Utterance:
    id: str
    at: float
    speaker: str
    text: str


@dataclass(slots=True)
class Card:
    id: str
    title: str
    named_by: str  # "speaker" | "heard"
    status: CardStatus
    created_at: float
    updated_at: float
    anchor: str | None = None
    one_liner: str = ""
    summary: str = ""
    page: bool = False
    highlight: str | None = None
    notes: list[str] = field(default_factory=list)
    seeded: bool = False


@dataclass(slots=True)
class Task:
    id: str
    card_id: str
    brief: str
    status: TaskStatus
    started_at: float
    finished_at: float | None = None
    summary: str = ""
    sources: list[str] = field(default_factory=list)
    status_line: str = "starting"
    spoken: bool = False
    unprompted: bool = True


@dataclass(slots=True)
class Said:
    id: str
    at: float
    text: str
    reason: str
    refs: list[str]
    evidence: list[str]
    delivered: bool


@dataclass(slots=True)
class Asked:
    at: float
    utterance_id: str
    text: str
    intent: str
    replied: bool = False


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.cards_dir = self.data_dir / "cards"
        self.cards_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_path = self.data_dir / "transcript.md"
        self.notes_path = self.data_dir / "notes.md"

        self.transcript: list[Utterance] = []
        self.notes: str = ""
        self.notes_version: int = 0
        self.cards: dict[str, Card] = {}
        self.tasks: dict[str, Task] = {}
        self.said: list[Said] = []
        self.log: deque[dict[str, Any]] = deque(maxlen=300)
        self.asked: Asked | None = None
        self.expanded: str | None = None
        #: The card the room is talking about right now, per the classifier.
        self.focus: str | None = None
        self.focus_at: float = 0.0
        #: "Currently working on": short items, ticked when done.
        self.working: list[dict[str, Any]] = []
        self.started_at = now()
        #: Last moment the room was audibly speaking: a partial or a commit.
        self.last_speech_at: float = 0.0
        self.partial: str = ""

        self._seq = 0
        self._on_change: Callable[[], Awaitable[None] | None] | None = None
        self._push_pending = False
        self.reset_files()

    # -- wiring ------------------------------------------------------------

    def on_change(self, fn: Callable[[], Awaitable[None] | None]) -> None:
        self._on_change = fn

    def touch(self) -> None:
        """Schedule one push for all the mutations of this loop iteration."""
        if self._on_change is None or self._push_pending:
            return
        self._push_pending = True
        loop = asyncio.get_event_loop()
        loop.call_later(0.05, self._fire)

    def _fire(self) -> None:
        self._push_pending = False
        if self._on_change is None:
            return
        try:
            r = self._on_change()
            if asyncio.iscoroutine(r):
                asyncio.ensure_future(r)
        except Exception:
            log.exception("push failed")

    def reset_files(self) -> None:
        self.transcript_path.write_text("# Transcript\n\n", encoding="utf-8")
        self.notes_path.write_text("", encoding="utf-8")

    # -- the Scribe --------------------------------------------------------

    def commit(self, text: str, speaker: str = "?") -> Utterance:
        """One committed utterance → one anchored line. Verbatim. Never edited."""
        self._seq += 1
        u = Utterance(id=f"u{self._seq:04d}", at=now(), speaker=speaker, text=text.strip())
        prev = self.transcript[-1] if self.transcript else None
        self.transcript.append(u)
        with self.transcript_path.open("a", encoding="utf-8") as f:
            if prev is not None and (u.at - prev.at > 20.0):
                f.write(f"\n## {time.strftime('%H:%M', time.localtime(u.at))}\n\n")
            elif prev is not None and prev.speaker != speaker:
                f.write("\n")
            f.write(f"**{speaker}**  `{u.id}`  {u.text}\n")
        self.last_speech_at = u.at
        self.partial = ""
        self.touch()
        return u

    def heard_partial(self, text: str) -> None:
        self.partial = text
        self.last_speech_at = now()

    def since(self, utterance_id: str | None) -> list[Utterance]:
        if utterance_id is None:
            return list(self.transcript)
        out: list[Utterance] = []
        seen = False
        for u in self.transcript:
            if seen:
                out.append(u)
            elif u.id == utterance_id:
                seen = True
        return out if seen else list(self.transcript)

    def tail(self, seconds: float) -> list[Utterance]:
        cutoff = now() - seconds
        return [u for u in self.transcript if u.at >= cutoff]

    @staticmethod
    def render(lines: list[Utterance]) -> str:
        return "\n".join(f"[{u.id}] {u.speaker}: {u.text}" for u in lines)

    # -- notes -------------------------------------------------------------

    def set_notes(self, text: str) -> None:
        self.notes = text
        self.notes_version += 1
        self.notes_path.write_text(text, encoding="utf-8")
        self.touch()

    # -- cards -------------------------------------------------------------

    def create_card(
        self, title: str, *, named_by: str = "speaker", anchor: str | None = None,
        one_liner: str = "", seeded: bool = False,
    ) -> Card:
        cid = _slug(title) or mint_id("c")
        base, n = cid, 2
        while cid in self.cards:
            cid = f"{base}-{n}"
            n += 1
        t = now()
        card = Card(
            id=cid, title=title.strip(), named_by=named_by, status="PLACEHOLDER",
            created_at=t, updated_at=t, anchor=anchor, one_liner=one_liner, seeded=seeded,
        )
        self.cards[cid] = card
        self.log_event("card", f"{card.title} ({named_by})")
        self.touch()
        return card

    def find_card(self, ref: str) -> Card | None:
        if ref in self.cards:
            return self.cards[ref]
        low = ref.strip().lower()
        for c in self.cards.values():
            if c.title.lower() == low or c.id == _slug(ref):
                return c
        for c in self.cards.values():
            if low and (low in c.title.lower() or c.title.lower() in low):
                return c
        return None

    def delete_card(self, card_id: str) -> bool:
        """Remove a card by hand, with its tasks and page."""
        card = self.cards.pop(card_id, None)
        if card is None:
            return False
        for tid in [t.id for t in self.tasks.values() if t.card_id == card_id]:
            self.tasks.pop(tid, None)
        try:
            self.card_page_path(card_id).unlink(missing_ok=True)
        except OSError:
            pass
        if self.focus == card_id:
            self.focus = None
        if self.expanded == card_id:
            self.expanded = None
        self.log_event("card", f"{card.title} removed by hand")
        self.touch()
        return True

    def card_page_path(self, card_id: str) -> Path:
        return self.cards_dir / f"{card_id}.html"

    def set_card_page(self, card_id: str, html: str, summary: str) -> None:
        card = self.cards[card_id]
        self.card_page_path(card_id).write_text(html, encoding="utf-8")
        card.page = True
        card.summary = summary
        card.status = "READY"
        card.updated_at = now()
        self.touch()

    # -- currently working on --------------------------------------------

    def work_start(self, key: str, text: str) -> None:
        """Add or refresh a short item. `key` lets the same job be ticked later."""
        text = " ".join(text.split())[:48]
        for w in self.working:
            if w["key"] == key and not w["done"]:
                w["text"] = text
                w["at"] = now()
                self.touch()
                return
        self.working.append({"key": key, "text": text, "done": False, "at": now(), "done_at": None})
        self.working = self.working[-8:]
        self.touch()

    def work_done(self, key: str) -> None:
        for w in self.working:
            if w["key"] == key and not w["done"]:
                w["done"] = True
                w["done_at"] = now()
        self.touch()

    def _prune_working(self) -> None:
        cutoff = now() - 8.0
        self.working = [w for w in self.working if not (w["done"] and (w["done_at"] or 0) < cutoff)]

    def set_focus(self, card_id: str | None) -> None:
        self.focus = card_id
        self.focus_at = now()
        self.touch()

    def expand(self, card_id: str | None) -> None:
        self.expanded = card_id
        self.touch()

    # -- tasks -------------------------------------------------------------

    def create_task(self, card_id: str, brief: str, *, unprompted: bool) -> Task:
        t = Task(id=mint_id("t"), card_id=card_id, brief=brief, status="RUNNING",
                 started_at=now(), unprompted=unprompted)
        self.tasks[t.id] = t
        card = self.cards.get(card_id)
        if card is not None and card.status == "PLACEHOLDER":
            card.status = "INVESTIGATING"
            card.updated_at = now()
        self.log_event("task", f"{brief} → {card_id}")
        self.touch()
        return t

    def running_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.status == "RUNNING"]

    def finish_task(self, task_id: str, *, summary: str, sources: list[str], failed: bool = False) -> Task:
        t = self.tasks[task_id]
        t.status = "FAILED" if failed else "DONE"
        t.finished_at = now()
        t.summary = summary
        t.sources = sources
        t.status_line = "failed" if failed else "done"
        card = self.cards.get(t.card_id)
        if card is not None and not any(x.status == "RUNNING" for x in self.tasks.values() if x.card_id == card.id):
            if card.status == "INVESTIGATING" and not card.page:
                card.status = "READY" if not failed else "PLACEHOLDER"
            card.updated_at = now()
        self.touch()
        return t

    # -- said / asked / log ------------------------------------------------

    def mark_asked(self, utterance_id: str, text: str, intent: str) -> None:
        self.asked = Asked(at=now(), utterance_id=utterance_id, text=text, intent=intent)
        self.touch()

    def record_said(self, text: str, reason: str, refs: list[str], evidence: list[str], delivered: bool) -> Said:
        s = Said(id=mint_id("say"), at=now(), text=text, reason=reason, refs=refs,
                 evidence=evidence, delivered=delivered)
        self.said.append(s)
        self.log_event("said" if delivered else "dropped", f"[{reason}] {text}")
        self.touch()
        return s

    def log_event(self, kind: str, text: str) -> None:
        self.log.append({"at": now(), "kind": kind, "text": text})
        self.touch()

    # -- snapshot ----------------------------------------------------------

    def snapshot(self, *, speaking: bool = False) -> dict[str, Any]:
        cards = sorted(self.cards.values(), key=lambda c: c.updated_at, reverse=True)
        tasks = sorted(self.tasks.values(), key=lambda t: t.started_at, reverse=True)
        return {
            "now": now(),
            "started_at": self.started_at,
            "speaking": speaking,
            "partial": self.partial,
            "transcript": [asdict(u) for u in self.transcript[-80:]],
            "notes": self.notes,
            "notes_version": self.notes_version,
            "cards": [asdict(c) for c in cards],
            "tasks": [asdict(t) for t in tasks[:30]],
            "said": [asdict(s) for s in self.said[-20:]],
            "log": list(self.log)[-60:],
            "asked": asdict(self.asked) if self.asked else None,
            "expanded": self.expanded,
            "focus": self.focus,
            "focus_at": self.focus_at,
            "working": self._working_view(),
        }

    def _working_view(self) -> list[dict[str, Any]]:
        self._prune_working()
        # In progress first, newest on top; then the ticked ones.
        active = sorted((w for w in self.working if not w["done"]), key=lambda w: -w["at"])
        done = sorted((w for w in self.working if w["done"]), key=lambda w: -(w["done_at"] or 0))
        return [dict(w) for w in active + done]


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")[:40]
