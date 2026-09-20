"""NEW-102 · Text to speech — ElevenLabs streaming, with two fallbacks.

`FR-013`: speak a line with a target, without blocking the turn.

Three rules from docs/06-implementation.md §6.5.3, in order of how much damage
breaking them does:

1. **`speak()` returns before the audio does.** A TTS round trip plus playback
   runs into seconds and a turn that waited would block every event behind it.
   Synthesis is therefore a task, and the only thing `speak()` does
   synchronously is arm the echo guard.
2. **The exact string the classifier chose.** No wording is generated here.
3. **The audio plays on the board**, over the socket that already exists, so
   there is one output device and one place a browser can refuse to make a
   sound.

Failure is a ladder, never silence:

    ElevenLabs stream  →  audio/prerendered/<slug>.mp3  →  audio_b64: null

The last rung sends the line with no audio attached, which CT-007 defines as
"no audio was produced". A line the classifier decided
to say must never turn into nothing at all.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import httpx

from heard.config import CONFIG, Config
from heard.contracts import PlaybackState, Target

from .playback import Playback, estimate_duration_s

log = logging.getLogger("heard.speech.tts")

#: Confirmed against https://elevenlabs.io/docs/api-reference/text-to-speech/convert-as-stream
_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"

#: mp3 because the board plays it in an <audio>/WebAudio element and every
#: browser decodes mp3. `optimize_streaming_latency` is the documented knob;
#: 3 trades a little quality for time-to-first-byte, which is what a kitchen
#: notices.
_OUTPUT_FORMAT = "mp3_44100_128"
_LATENCY_HINT = 3

#: Extra window handed to the echo guard while synthesis is in flight, so a
#: transcript arriving between `speak()` and the audio going out is still
#: matched against the line (speech/playback.py).
_SYNTH_SLACK_S = 2.0

#: The board is the output device, so nothing here should ever sit for long.
_HTTP_TIMEOUT_S = 10.0

PRERENDERED_DIR = Path(__file__).resolve().parent.parent / "audio" / "prerendered"


def slug(text: str) -> str:
    """`audio/prerendered/<slug>.mp3` — the beat-sheet lines, pre-rendered."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


SendAudio = Callable[[str, Target, str | None], Awaitable[None]]
"""How a line reaches the board: the CT-007 `audio` envelope, minus the type."""


class Speaks(Protocol):
    async def speak(self, text: str, target: Target) -> None: ...
    def playback_state(self) -> PlaybackState: ...


class Voice:
    """CT-008 · what `core/tools.py:say` reaches. Implements `Speaker`.

    `core/` never imports this module; it is handed an instance that satisfies
    the protocol in core/contracts.py.
    """

    def __init__(
        self,
        *,
        send_audio: SendAudio,
        playback: Playback | None = None,
        config: Config = CONFIG,
        prerendered_dir: Path = PRERENDERED_DIR,
    ) -> None:
        self._send_audio = send_audio
        self._playback = playback or Playback(config)
        self._config = config
        self._prerendered_dir = prerendered_dir
        self._client: httpx.AsyncClient | None = None
        self._tasks: set[asyncio.Task[None]] = set()

        requested = (config.tts_provider or "auto").lower()
        if requested == "off":
            self._mode = "off"
        elif requested == "browser":
            self._mode = "browser"
        elif config.has_elevenlabs:
            self._mode = "elevenlabs"
        elif requested == "elevenlabs":
            # Asked for explicitly with no key. main.py refuses to start in
            # this case; if we are reached anyway, degrade rather than crash
            # mid-service.
            log.error("HEARD_TTS=elevenlabs but ELEVENLABS_API_KEY is unset")
            self._mode = "browser"
        else:
            self._mode = "browser"
        log.info("tts provider: %s", self._mode)

    @property
    def mode(self) -> str:
        """`elevenlabs` · `browser` · `off`. Printed at startup."""
        return self._mode

    @property
    def playback(self) -> Playback:
        return self._playback

    # -- CT-008 ------------------------------------------------------------

    async def speak(self, text: str, target: Target = "line") -> None:
        """Hand text to TTS. Returns before the audio does — see §6.5.3.

        The echo guard is armed HERE, synchronously, before the task is
        spawned. If it were armed inside the task, a transcript arriving
        during synthesis would not be matched against a line we have already
        committed to saying.
        """
        text = (text or "").strip()
        if not text:
            return
        self._playback.note(text, estimate_duration_s(text) + _SYNTH_SLACK_S)

        task = asyncio.create_task(self._deliver(text, target), name="tts-deliver")
        # Keep a reference: a task with no strong reference can be garbage
        # collected mid-flight.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def playback_state(self) -> PlaybackState:
        """CT-009 · what the buffer reads to reject the system's own voice."""
        return self._playback.playback_state()

    # -- the ladder --------------------------------------------------------

    async def _deliver(self, text: str, target: Target) -> None:
        try:
            if self._mode == "off":
                # `--no-audio`: an operator asked for silence. The line still
                # enters playback state, so the buffer still suppresses any
                # transcript of a human reading it aloud, and it still shows
                # on stdout for a dev with no speakers.
                self._playback.note(text)
                log.info("[say/%s · silent] %s", target, text)
                return

            audio: bytes | None = None
            source = "browser"
            if self._mode == "elevenlabs":
                audio = await self._synthesize(text)
                if audio:
                    source = "elevenlabs"
            if audio is None:
                audio = await self.prerendered(text)
                if audio:
                    source = "prerendered"

            # Re-note from the real dispatch time: the first note covered the
            # synthesis gap, this one covers the audio itself.
            self._playback.note(text, estimate_duration_s(text))

            b64 = base64.b64encode(audio).decode("ascii") if audio else None
            log.info("[say/%s · %s] %s", target, source, text)
            await self._send_audio(text, target, b64)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Last resort. The board can still say it; going silent cannot be
            # recovered from on stage.
            log.exception("tts delivery failed, falling back to the board")
            try:
                await self._send_audio(text, target, None)
            except Exception:
                log.exception("could not reach the board with %r", text)

    async def _synthesize(self, text: str) -> bytes | None:
        """One streaming request. Returns the whole mp3, or None on any failure.

        Streaming buys time-to-first-byte from ElevenLabs, but the audio leaves
        here as one base64 blob in one `audio` envelope (CT-007), so the chunks
        are accumulated rather than forwarded. Streaming the socket too would
        need a second envelope kind, and the contract is frozen.
        """
        client = self._http()
        url = _TTS_URL.format(voice_id=self._config.elevenlabs_voice_id)
        params = {
            "output_format": _OUTPUT_FORMAT,
            "optimize_streaming_latency": _LATENCY_HINT,
        }
        headers = {
            "xi-api-key": self._config.elevenlabs_api_key,
            "accept": "audio/mpeg",
        }
        body: dict[str, Any] = {"text": text, "model_id": self._config.tts_model_id}
        speed = float(getattr(self._config, "tts_speed", 1.0) or 1.0)
        if speed != 1.0:
            body["voice_settings"] = {"speed": max(0.7, min(1.2, speed))}
        chunks: list[bytes] = []
        try:
            async with client.stream(
                "POST", url, params=params, headers=headers, json=body
            ) as response:
                if response.status_code != 200:
                    detail = (await response.aread())[:400].decode("utf-8", "replace")
                    log.error("elevenlabs tts %s: %s", response.status_code, detail)
                    return None
                async for chunk in response.aiter_bytes():
                    if chunk:
                        chunks.append(chunk)
        except Exception as exc:
            log.error("elevenlabs tts failed (%s): %s", type(exc).__name__, exc)
            return None
        audio = b"".join(chunks)
        return audio or None

    async def prerendered(self, text: str) -> bytes | None:
        """A known beat-sheet line, off disk, when synthesis fails.

        Read off the loop: it is a file read of a few tens of kilobytes, but
        §6.2 is unambiguous that nothing blocking belongs on this thread.
        """
        path = self._prerendered_dir / f"{slug(text)}.mp3"
        try:
            if not path.is_file():
                return None
            return await asyncio.to_thread(path.read_bytes)
        except Exception:
            log.exception("could not read prerendered %s", path)
            return None

    # -- lifecycle ---------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
        return self._client

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None
