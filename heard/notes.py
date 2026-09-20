"""The notes agent. Always on. Rewrites notes.md every ~20 s from new speech.

Free-form markdown: a meeting is open-ended and forcing a schema on it produces
a worse record than a person would write. It writes whole versions, so a reader
never sees half a file. It may fail; the previous version stands.

`notes.md` is the shared context window: the front desk reads it instead of
re-reading the raw transcript.
"""

from __future__ import annotations

import asyncio
import logging

from . import nim
from .config import CONFIG, Config
from .contracts import now
from .store import Store

log = logging.getLogger("heard.notes")

SYSTEM = """You keep the live notes for a working meeting. You receive your previous notes and the transcript lines that arrived since. Rewrite the notes as ONE markdown document, whole, and return only the markdown (no preamble, no fence).

Shape it the way a sharp colleague would:
# <a title for the meeting, once you know what it is about>
_one-line summary of where the discussion stands right now_

## <one section per subject / idea / project being discussed>
- what it is, in the speaker's terms
- claims made, with who said them
- objections and open questions
- decisions, if any

## Open questions
- things asked out loud that nobody answered

## Decisions
- what the room agreed on, if anything

Rules: keep it short and specific; never invent; keep every earlier section unless it was clearly superseded; cite transcript ids in brackets like [u0012] after claims; the whole document must stay under ~600 words.
"""


class NotesAgent:
    def __init__(self, store: Store, *, config: Config = CONFIG) -> None:
        self.store = store
        self.config = config
        self._last_seen: str | None = None
        self._closing = asyncio.Event()
        self._kick = asyncio.Event()
        self.passes = 0
        self.last_error: str | None = None

    def notify(self) -> None:
        """A new card wants notes soon; do not wait the full interval."""
        self._kick.set()

    async def run(self) -> None:
        if not self.config.has_nvidia:
            log.warning("notes agent off: NVIDIA_API_KEY is unset")
            await self._closing.wait()
            return
        while not self._closing.is_set():
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=self.config.notes_every_s)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()
            if self._closing.is_set():
                return
            if nim.cooling() > 0:
                continue  # the classifier gets the quota first
            try:
                await self.pass_once()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("notes pass failed: %s", self.last_error)

    async def close(self) -> None:
        self._closing.set()
        self._kick.set()

    async def pass_once(self) -> bool:
        new = self.store.since(self._last_seen)
        if not new:
            return False
        user = (
            "PREVIOUS NOTES:\n" + (self.store.notes or "(none yet)") +
            "\n\nNEW TRANSCRIPT LINES:\n" + self.store.render(new)
        )
        started = now()
        text = await nim.chat(SYSTEM, user, max_tokens=1400, timeout=25.0)
        text = nim.strip_fence(nim.strip_reasoning(text))
        if len(text) < 20:
            log.warning("notes pass returned nothing usable")
            return False
        self._last_seen = new[-1].id
        self.passes += 1
        self.store.set_notes(text)
        log.info("notes v%d · %d lines consumed · %.1fs", self.store.notes_version, len(new), now() - started)
        return True
