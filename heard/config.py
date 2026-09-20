"""Every tunable in one place, read from the environment.

Ported from the kitchen build. A missing key fails at startup, not three
minutes into a meeting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def _f(key: str, default: float) -> float:
    v = os.getenv(key)
    return float(v) if v not in (None, "") else default


def _i(key: str, default: int) -> int:
    v = os.getenv(key)
    return int(v) if v not in (None, "") else default


def _s(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _b(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v in (None, ""):
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(slots=True)
class Config:
    # -- providers ---------------------------------------------------------
    nvidia_api_key: str = field(repr=False, default_factory=lambda: _s("NVIDIA_API_KEY"))
    nvidia_base_url: str = field(
        default_factory=lambda: _s("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    )
    #: The Nemotron behind the classifier and the notes agent. Copied from
    #: build.nvidia.com; NVIDIA renames these, so it is configuration.
    model_id: str = field(
        default_factory=lambda: _s("NEMOTRON_MODEL_ID", "nvidia/nemotron-3-super-120b-a12b")
    )
    elevenlabs_api_key: str = field(repr=False, default_factory=lambda: _s("ELEVENLABS_API_KEY"))
    elevenlabs_voice_id: str = field(
        default_factory=lambda: _s("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
    )
    tts_model_id: str = field(default_factory=lambda: _s("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5"))

    #: STT: "auto" tries Scribe and falls back to the board's own recogniser.
    #: TTS: "auto" is ElevenLabs, then a pre-rendered file, then silence.
    stt_provider: str = field(default_factory=lambda: _s("HEARD_STT", "auto"))
    tts_provider: str = field(default_factory=lambda: _s("HEARD_TTS", "auto"))

    #: The front desk and the researchers run on the Claude Agent SDK. Empty
    #: means the CLI's default model. Aliases ("opus", "sonnet") are accepted.
    frontdesk_model: str = field(default_factory=lambda: _s("HEARD_FRONTDESK_MODEL", ""))
    research_model: str = field(default_factory=lambda: _s("HEARD_RESEARCH_MODEL", ""))
    frontdesk_effort: str = field(default_factory=lambda: _s("HEARD_FRONTDESK_EFFORT", "low"))
    research_effort: str = field(default_factory=lambda: _s("HEARD_RESEARCH_EFFORT", "low"))
    #: "off" runs the process without a front desk: cards and notes only.
    frontdesk: str = field(default_factory=lambda: _s("HEARD_FRONTDESK", "auto"))

    # -- the clocks --------------------------------------------------------
    #: Classifier: fires on new speech, floored and ceilinged.
    classify_floor_s: float = field(default_factory=lambda: _f("HEARD_CLASSIFY_FLOOR_S", 2.0))
    classify_ceiling_s: float = field(default_factory=lambda: _f("HEARD_CLASSIFY_CEILING_S", 5.0))
    #: Notes agent: rewrites notes.md this often, when there is new speech.
    notes_every_s: float = field(default_factory=lambda: _f("HEARD_NOTES_EVERY_S", 20.0))
    #: NIM call ceiling. The classifier is on the 5-second path.
    nim_timeout_s: float = field(default_factory=lambda: _f("HEARD_NIM_TIMEOUT_S", 8.0))
    nim_max_tokens: int = field(default_factory=lambda: _i("HEARD_NIM_MAX_TOKENS", 600))
    nim_disable_thinking: bool = field(default_factory=lambda: _b("HEARD_DISABLE_THINKING", True))
    max_rpm: int = field(default_factory=lambda: _i("HEARD_MAX_RPM", 30))

    # -- the harness -------------------------------------------------------
    #: `asked` holds this long after the classifier flagged a direct address.
    asked_window_s: float = field(default_factory=lambda: _f("HEARD_ASKED_WINDOW_S", 20.0))
    #: One unsolicited line per this many seconds.
    unsolicited_gap_s: float = field(default_factory=lambda: _f("HEARD_UNSOLICITED_GAP_S", 90.0))
    #: The floor: released on a silence this long in the transcript stream.
    floor_gap_s: float = field(default_factory=lambda: _f("HEARD_FLOOR_GAP_S", 0.8))
    #: No gap in this long: the line is dropped to a highlight on its card.
    floor_timeout_s: float = field(default_factory=lambda: _f("HEARD_FLOOR_TIMEOUT_S", 15.0))
    max_sentences: int = field(default_factory=lambda: _i("HEARD_MAX_SENTENCES", 2))
    #: Concurrent researchers.
    max_research: int = field(default_factory=lambda: _i("HEARD_MAX_RESEARCH", 3))
    research_timeout_s: float = field(default_factory=lambda: _f("HEARD_RESEARCH_TIMEOUT_S", 180.0))
    #: How long after a line is spoken a matching transcript is the mic
    #: hearing the speaker.
    playback_echo_guard_s: float = field(default_factory=lambda: _f("HEARD_ECHO_GUARD_S", 2.0))

    # -- runtime -----------------------------------------------------------
    host: str = field(default_factory=lambda: _s("HEARD_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _i("HEARD_PORT", 8000))
    data_dir: str = field(default_factory=lambda: _s("HEARD_DATA_DIR", str(REPO_ROOT / "data")))

    # -- readiness ---------------------------------------------------------
    @property
    def has_nvidia(self) -> bool:
        return bool(self.nvidia_api_key)

    @property
    def has_elevenlabs(self) -> bool:
        return bool(self.elevenlabs_api_key)

    def __repr__(self) -> str:  # noqa: D105
        shown = []
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name.endswith(("_key", "_token", "_secret")):
                v = f"<set:{len(v)}>" if v else "<unset>"
            shown.append(f"{f.name}={v!r}")
        return f"Config({', '.join(shown)})"

    def report(self) -> list[str]:
        """One line per provider, printed at startup. Never prints a key."""
        return [
            f"  classifier  {'NIM · ' + self.model_id if self.has_nvidia else 'NO KEY — no cards, no notes'}",
            f"  notes       {'NIM · ' + self.model_id if self.has_nvidia else 'off'}",
            f"  front desk  {'off' if self.frontdesk == 'off' else 'Claude Agent SDK · ' + (self.frontdesk_model or 'default')}",
            f"  research    Claude Agent SDK · {self.research_model or 'default'} · WebSearch",
            f"  speech in   {'ElevenLabs Scribe' if self.has_elevenlabs and self.stt_provider != 'browser' else 'browser Web Speech'}",
            f"  speech out  {'ElevenLabs TTS' if self.has_elevenlabs and self.tts_provider != 'off' else 'OFF — nothing will be spoken'}",
        ]


CONFIG = Config()
