"""The few shapes shared across modules. Small on purpose."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Target = Literal["line", "floor"]
EventKind = Literal["utterance", "wake"]


def now() -> float:
    return time.time()


def mint_id(prefix: str = "s") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@dataclass(slots=True)
class Signal:
    """What the STT producer hands to the scribe. Partials never appear here."""

    kind: EventKind
    payload: dict[str, Any]
    from_playback: bool = False
    at: float = field(default_factory=now)


@dataclass(slots=True)
class PlaybackState:
    speaking: bool
    recent_lines: list[str] = field(default_factory=list)
