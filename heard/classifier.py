"""The classifier. Small, fast, JSON-only, one job: sort what was just said.

Fires on new speech, floored at `classify_floor_s`, forced at
`classify_ceiling_s`. It creates placeholder cards DIRECTLY — the card has to
be on the board within 5 s and a front-desk wake would blow that. Everything
else it finds goes to the front desk as a signal.

It is allowed to be wrong. A bad pass costs one stray card or one unnecessary
wake, never an utterance: it cannot speak.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Awaitable, Callable

from . import nim
from .config import CONFIG, Config
from .contracts import now
from .store import Store, Utterance

log = logging.getLogger("heard.classifier")

SYSTEM = """You are the classifier inside Heard!, an ambient assistant that sits in a working meeting (a hackathon team deciding what to build, a product discussion, a lab meeting). You read the last few seconds of transcript and sort it. You never speak and never decide what happens next; you only describe.

Return ONE JSON object and nothing else:
{
  "new_subject": {"title": "...", "named_by": "speaker"|"heard", "one_liner": "...", "anchor": "u0012"} | null,
  "questions": [{"text": "...", "subject": "<existing card title or the new title or null>", "worth_investigating": true|false}],
  "addressed": true|false,
  "intent": "opinion"|"how_built"|"lookup"|"other"|null,
  "request": "<what they asked Heard to do, verbatim-ish>"|null,
  "salience": 0|1|2
}

Rules:
- A "subject" is a distinct project, product idea, experiment, or proposal that someone is PITCHING in the new lines — they describe what it is or does in at least a sentence. At most ONE new subject per pass, and only if it does not match an existing card title (listed below; treat near-spellings and paraphrases as matches). NOT subjects, ever: reactions to an existing card ("I like that", "who is it for", "let's go with it"), small talk, logistics, talk about the meeting itself, and talk about Heard (the assistant). If the new lines only discuss an existing card, new_subject MUST be null. When in doubt, null: a missed subject costs nothing, a spurious card costs the board its credibility.
- If the speaker gave the idea a name, copy it EXACTLY as it appears in the transcript (do not respell it) and set named_by "speaker". If they described it without a name, invent a short, memorable product-style name (2 words max, no emoji) and set named_by "heard".
- "anchor" is the [uXXXX] id of the line where the subject was introduced.
- "questions" are things said out loud that could be checked or looked up: has it been done, who does this, is X true, how would we build Y. Mark worth_investigating true only for questions a web search could actually answer. An idea being pitched always implies at least "has this been done before?" and "who is it for?" — list those as questions on the new subject.
- "addressed" is true ONLY when someone speaks to Heard by name ("Heard", "hey Heard", "let's see what Heard thinks", "Heard, look up...") with a question or request. Precision over recall: if unsure, false. intent: "opinion" = what does Heard think of the current subject; "how_built" = how was Heard itself built / what is the tech stack; "lookup" = look something up; else "other".
- "salience" 2 = the room is converging on a decision or making a strong claim ("nobody does this", "let's go with it", "agreed"); 1 = substantive discussion; 0 = filler.
"""

Signal = dict[str, Any]


class Classifier:
    def __init__(
        self,
        store: Store,
        *,
        on_signal: Callable[[Signal], Awaitable[None] | None],
        config: Config = CONFIG,
    ) -> None:
        self.store = store
        self.on_signal = on_signal
        self.config = config
        self._last_seen: str | None = None
        self._wake = asyncio.Event()
        self._closing = asyncio.Event()
        self.fires = 0
        self.last_fire_at: float | None = None
        self.last_error: str | None = None

    def notify(self) -> None:
        """Called on every commit."""
        self._wake.set()

    async def run(self) -> None:
        if not self.config.has_nvidia:
            log.warning("classifier off: NVIDIA_API_KEY is unset")
            await self._closing.wait()
            return
        while not self._closing.is_set():
            await self._wake.wait()
            self._wake.clear()
            # Floor: let a burst of short utterances land as one pass. Ceiling:
            # a long monologue still gets sliced.
            started = now()
            while now() - started < self.config.classify_ceiling_s:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.config.classify_floor_s)
                    self._wake.clear()
                except asyncio.TimeoutError:
                    break
            try:
                await self.fire()
            except Exception as exc:  # a bad pass costs nothing but this pass
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("classifier pass failed: %s", self.last_error)

    async def close(self) -> None:
        self._closing.set()
        self._wake.set()

    async def fire(self) -> Signal | None:
        new = self.store.since(self._last_seen)
        if not new:
            return None
        self._last_seen = new[-1].id
        context = self.store.tail(45.0)
        # Context first (older), then the fresh span marked as such.
        older = [u for u in context if u.at < new[0].at][-12:]
        user = _prompt(older, new, self.store)
        self.fires += 1
        self.last_fire_at = now()
        result = await nim.chat_json(SYSTEM, user, max_tokens=500)
        if not result:
            return None
        signal = _normalise(result, new)
        # Placeholder card, directly, on the 5-second path.
        ns = signal.get("new_subject")
        if ns and ns.get("title") and self.store.find_card(ns["title"]) is None:
            card = self.store.create_card(
                ns["title"], named_by=ns.get("named_by") or "speaker",
                anchor=ns.get("anchor"), one_liner=ns.get("one_liner") or "",
            )
            signal["card_id"] = card.id
            log.info("card %s (%s) · %.1fs after commit", card.title, card.named_by,
                     now() - new[-1].at)
        elif ns:
            existing = self.store.find_card(ns["title"])
            signal["new_subject"] = None
            signal["card_id"] = existing.id if existing else None
        if signal.get("addressed"):
            self.store.mark_asked(new[-1].id, signal.get("request") or new[-1].text,
                                  signal.get("intent") or "other")
        self.store.log_event("classify", _describe(signal))
        r = self.on_signal(signal)
        if asyncio.iscoroutine(r):
            await r
        return signal


def _prompt(older: list[Utterance], new: list[Utterance], store: Store) -> str:
    titles = [f"- {c.title} (id {c.id})" for c in store.cards.values()]
    parts = []
    if titles:
        parts.append("Existing cards:\n" + "\n".join(titles))
    else:
        parts.append("Existing cards: none")
    if older:
        parts.append("Earlier context (already classified):\n" + store.render(older))
    parts.append("NEW since the last pass (classify this):\n" + store.render(new))
    return "\n\n".join(parts)


def _normalise(r: dict[str, Any], new: list[Utterance]) -> Signal:
    ns = r.get("new_subject")
    if isinstance(ns, dict) and isinstance(ns.get("title"), str) and ns["title"].strip():
        ns = {
            "title": ns["title"].strip()[:60],
            "named_by": "heard" if str(ns.get("named_by", "")).lower() == "heard" else "speaker",
            "one_liner": str(ns.get("one_liner") or "")[:200],
            "anchor": ns.get("anchor") if isinstance(ns.get("anchor"), str) else new[0].id,
        }
    else:
        ns = None
    qs = []
    for q in r.get("questions") or []:
        if isinstance(q, dict) and isinstance(q.get("text"), str) and q["text"].strip():
            qs.append({
                "text": q["text"].strip()[:200],
                "subject": q.get("subject") if isinstance(q.get("subject"), str) else None,
                "worth_investigating": bool(q.get("worth_investigating")),
            })
    return {
        "at": now(),
        "utterances": [u.id for u in new],
        "text": " ".join(u.text for u in new),
        "new_subject": ns,
        "questions": qs[:5],
        "addressed": bool(r.get("addressed")),
        "intent": r.get("intent") if isinstance(r.get("intent"), str) else None,
        "request": r.get("request") if isinstance(r.get("request"), str) else None,
        "salience": int(r.get("salience") or 0) if str(r.get("salience", "0")).isdigit() else 0,
    }


def _describe(s: Signal) -> str:
    bits = []
    if s.get("new_subject"):
        bits.append(f"subject: {s['new_subject']['title']}")
    if s.get("questions"):
        bits.append(f"{len(s['questions'])} question(s)")
    if s.get("addressed"):
        bits.append(f"addressed ({s.get('intent')})")
    bits.append(f"salience {s.get('salience', 0)}")
    return " · ".join(bits)


def to_json(s: Signal) -> str:
    return json.dumps({k: v for k, v in s.items() if k != "utterances"}, ensure_ascii=False)
