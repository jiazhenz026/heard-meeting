"""NEW-103 · Playback state — the window the buffer reads to reject our own voice.

`FR-014`: never act on the system's own voice. The mechanism is deliberately
dumb, because a smart one would have to understand what was said: a line we
sent to TTS is remembered for as long as it could plausibly still be in the
air, plus `CONFIG.playback_echo_guard_s` of slack, and any transcript that
matches one of those lines is the microphone hearing the speaker rather than a
person (docs/07-technical-risk.md §7.2).

Playback state is tracked SEPARATELY from turn state on purpose
(docs/06-implementation.md §6.5.3): the turn is over long before the audio is,
so nothing here may be derived from whether a turn is running.

Nothing in this module does I/O. It is a clock and a small list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from heard.config import CONFIG, Config
from heard.contracts import PlaybackState, now

# Conversational TTS lands around 165 wpm. We never learn the real duration —
# the audio plays in the browser (docs/06-implementation.md §6.5.3) and the
# board sends nothing back — so the end of playback is an estimate, and it is
# deliberately generous: guessing long costs a swallowed transcript, guessing
# short costs the system acting on its own voice.
_WORDS_PER_SECOND = 2.75
_MIN_SPEECH_S = 1.2
_TAIL_S = 0.4


def estimate_duration_s(text: str) -> float:
    """How long `text` will take to say. An estimate; see the note above."""
    words = max(1, len(text.split()))
    return max(_MIN_SPEECH_S, words / _WORDS_PER_SECOND + _TAIL_S)


def normalize(text: str) -> str:
    """Strip everything an ASR and a TTS could reasonably disagree about."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


@dataclass(slots=True)
class _Line:
    text: str
    norm: str
    started_at: float
    ends_at: float


class Playback:
    """The `speaking` flag and the recent-line window, and nothing else.

    `Voice` (speech/tts.py) writes to it; `core/buffer.py` reads it through the
    `Speaker` protocol (CT-009). Single loop, single writer, so no lock.
    """

    def __init__(self, config: Config = CONFIG) -> None:
        self._config = config
        self._lines: list[_Line] = []

    # -- writes, from speech/tts.py ---------------------------------------

    def note(self, text: str, duration_s: float | None = None) -> None:
        """Record that `text` is being spoken, ending `duration_s` from now.

        Called twice per line: once when `say` hands the text over, so the
        guard is armed while synthesis is still in flight, and again when the
        audio actually goes out. The second call extends the window from the
        new `now`, which is exactly what we want — the first call only has to
        cover the gap.
        """
        text = text.strip()
        if not text:
            return
        at = now()
        ends = at + (duration_s if duration_s is not None else estimate_duration_s(text))
        norm = normalize(text)
        for line in self._lines:
            if line.norm == norm:
                line.ends_at = max(line.ends_at, ends)
                return
        self._lines.append(_Line(text=text, norm=norm, started_at=at, ends_at=ends))

    def clear(self) -> None:
        """Reset step 3 (docs/07-technical-risk.md §7.3): a stalled TTS must
        not keep the transcript gated forever."""
        self._lines.clear()

    # -- reads, from core/buffer.py and the serializer ---------------------

    def playback_state(self) -> PlaybackState:
        self._prune()
        at = now()
        return PlaybackState(
            speaking=any(line.ends_at > at for line in self._lines),
            recent_lines=[line.text for line in self._lines],
        )

    def speaking(self) -> bool:
        self._prune()
        at = now()
        return any(line.ends_at > at for line in self._lines)

    def recent_lines(self) -> list[str]:
        """The text window a returning commit is matched against."""
        self._prune()
        return [line.text for line in self._lines]

    def is_echo(self, text: str) -> bool:
        """Is this transcript the system hearing itself?

        Containment either way, because Scribe will happily hand back half a
        line as one segment, and a loose token overlap for the case where it
        mangles a word in the middle. Short fragments are never echoes: "yes"
        appearing inside a line we spoke is not evidence of anything.
        """
        candidate = normalize(text)
        if len(candidate) < 6:
            return False
        for line in self.recent_lines():
            spoken = normalize(line)
            if not spoken:
                continue
            if candidate in spoken or spoken in candidate:
                return True
            a, b = set(candidate.split()), set(spoken.split())
            if a and b and len(a & b) / len(a | b) >= 0.7:
                return True
        return False

    # -- internals ---------------------------------------------------------

    def _prune(self) -> None:
        cutoff = now() - self._config.playback_echo_guard_s
        # A line is kept for `playback_echo_guard_s` PAST the end of playback,
        # which is the whole point of the guard: the commit arrives after the
        # audio finished, not during it.
        self._lines = [line for line in self._lines if line.ends_at > cutoff]
