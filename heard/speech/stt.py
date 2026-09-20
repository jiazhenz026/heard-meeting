"""NEW-101 · Speech to text — Scribe v2 Realtime, or the browser.

`FR-012`: transcribe continuously; partials to the strip, commits to the
buffer. docs/06-implementation.md §6.3.1 adds the rule that matters most —
**nothing downstream re-segments.** Whatever Scribe decided was one segment
stays one segment, and repairing a bad split is the buffer's job.

Two implementations behind one interface, selected by `CONFIG.stt_provider`:

    elevenlabs  the Scribe realtime WebSocket, fed PCM16 arriving from the
                board over the CT-007 `audio_chunk` envelope
    browser     the board runs the Web Speech API and sends the CT-007
                `transcript` envelope up instead
    auto        try ElevenLabs, fall back to browser on any connect or auth
                failure, and log which one it landed on
    off         neither; injection through the debug panel still works

THE PROTOCOL BELOW IS THE DOCUMENTED ONE, not a guess. Confirmed against
https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime
(fetched at build time):

    endpoint  wss://api.elevenlabs.io/v1/speech-to-text/realtime
    auth      `xi-api-key` request header, or a single-use `token` query param
    up        {"message_type": "input_audio_chunk",
               "audio_base_64": "<b64>", "sample_rate": 16000}
    down      {"message_type": "session_started", "session_id": ..., ...}
              {"message_type": "partial_transcript",   "text": ...}
              {"message_type": "committed_transcript", "text": ...}
              {"message_type": "committed_transcript_with_timestamps", ...}
              {"message_type": "error"|"auth_error"|"quota_exceeded"|..., ...}

`commit_strategy=vad` leaves segmentation to the server, which is the whole
point of using Scribe rather than a voice activity detector of our own.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

from heard.config import CONFIG, Config
from heard.contracts import Signal

from .playback import Playback

log = logging.getLogger("heard.speech.stt")

REALTIME_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
MODEL_ID = "scribe_v2_realtime"

#: The board captures at 16 kHz mono PCM16 (CT-007 `audio_chunk`), which is
#: one of the documented `audio_format` values, so no resampling anywhere.
SAMPLE_RATE = 16_000
AUDIO_FORMAT = "pcm_16000"

#: Anything in this set means retrying will not help: the key is wrong, the
#: account is out, or the request is malformed. `auto` falls back on these
#: immediately rather than backing off into the demo.
_FATAL = frozenset({"auth_error", "quota_exceeded", "invalid_request"})

#: Bounded so a stalled socket cannot grow the queue without limit. Audio is
#: the one thing where dropping the OLDEST is right: stale mic audio is worth
#: less than fresh mic audio.
_AUDIO_QUEUE_CAP = 100

_RECONNECT_BACKOFF_S = (0.5, 1.0, 2.0, 4.0)

OnPartial = Callable[[str], Awaitable[None]]
OnSignal = Callable[[Signal], Any]


async def _maybe_await(value: Any) -> None:
    """Other tracks may hand us a sync or an async callback. Accept both."""
    if inspect.isawaitable(value):
        await value


class Stt:
    """The provider-selecting front door. One instance, one background task.

    `run()` is the task main.py starts; `feed()` and `on_transcript()` are
    called from server/ws.py as the board's messages arrive.
    """

    def __init__(
        self,
        *,
        on_partial: OnPartial,
        on_signal: OnSignal,
        playback: Playback | None = None,
        config: Config = CONFIG,
    ) -> None:
        self._on_partial = on_partial
        self._on_signal = on_signal
        self._playback = playback
        self._config = config
        self._requested = (config.stt_provider or "auto").lower()
        # The mode is decided here, not in run(), so /healthz and the startup
        # banner say something true before the socket has had a chance to open.
        if self._requested in ("off", "browser"):
            self._mode = self._requested
        elif config.has_elevenlabs:
            self._mode = "elevenlabs (connecting)"
        else:
            self._mode = "browser"
        self._audio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_AUDIO_QUEUE_CAP)
        self._closing = asyncio.Event()
        self._connected = False

    @property
    def mode(self) -> str:
        """Which provider it actually landed on: what the report prints."""
        return self._mode

    @property
    def connected(self) -> bool:
        return self._connected

    # -- the background task ----------------------------------------------

    async def run(self) -> None:
        """Owns the Scribe socket for the life of the process.

        In `browser` and `off` mode there is no socket, so this parks: the
        task still exists so main.py's supervision is uniform.
        """
        if self._requested == "off":
            self._mode = "off"
            log.info("stt provider: off — only injected utterances will arrive")
            await self._closing.wait()
            return

        if self._requested == "browser":
            self._mode = "browser"
            log.info("stt provider: browser (Web Speech API on the board)")
            await self._closing.wait()
            return

        if not self._config.has_elevenlabs:
            # `auto` with no key is the normal development case.
            self._mode = "browser"
            log.info(
                "stt provider: browser — no ELEVENLABS_API_KEY, "
                "the board's Web Speech API is the transcript source"
            )
            await self._closing.wait()
            return

        await self._run_elevenlabs()

    async def _run_elevenlabs(self) -> None:
        strict = self._requested == "elevenlabs"
        attempt = 0
        while not self._closing.is_set():
            # Scribe closes a socket that has had no audio for ~15 s. Holding
            # one open from startup means a clean close every 15 s and an
            # immediate reconnect — a service spent reconnecting, and every
            # reconnect drops the segment that was in flight. So: no audio, no
            # socket. The session opens on the first chunk and lives as long as
            # the room is talking.
            await self._wait_for_audio()
            if self._closing.is_set():
                return
            try:
                await self._session()
                attempt = 0  # a clean close is not a failure
            except _Fatal as exc:
                log.error("scribe refused the connection: %s", exc)
                if strict:
                    # Asked for explicitly: stay loud and keep retrying slowly
                    # rather than silently becoming a different system.
                    self._mode = "elevenlabs (down)"
                    await self._sleep(_RECONNECT_BACKOFF_S[-1])
                    continue
                self._fall_back("auth or quota")
                await self._closing.wait()
                return
            except Exception as exc:
                log.warning(
                    "scribe socket dropped (%s: %s)", type(exc).__name__, exc
                )
                if attempt >= len(_RECONNECT_BACKOFF_S) - 1 and not strict:
                    self._fall_back("connect failed")
                    await self._closing.wait()
                    return
            finally:
                self._connected = False
            if self._closing.is_set():
                return
            await self._sleep(_RECONNECT_BACKOFF_S[min(attempt, len(_RECONNECT_BACKOFF_S) - 1)])
            attempt += 1

    def _fall_back(self, why: str) -> None:
        self._mode = "browser"
        log.warning(
            "stt provider: falling back to the browser Web Speech API (%s)", why
        )

    async def _wait_for_audio(self) -> None:
        """Block until there is something to send, or we are shutting down."""
        while not self._closing.is_set() and self._audio.empty():
            await self._sleep(0.2)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._closing.wait(), timeout=seconds)

    # -- one Scribe session -----------------------------------------------

    async def _session(self) -> None:
        from websockets.asyncio.client import connect  # imported late: see §6.2

        query = urlencode(
            {
                "model_id": MODEL_ID,
                "audio_format": AUDIO_FORMAT,
                # Server-side VAD. §6.3.1: whatever Scribe decided was one
                # segment stays one segment.
                "commit_strategy": "vad",
            }
        )
        url = f"{REALTIME_URL}?{query}"
        headers = {"xi-api-key": self._config.elevenlabs_api_key}

        try:
            socket = await connect(url, additional_headers=headers, max_size=None)
        except TypeError:
            # websockets < 14 spells it `extra_headers`.
            socket = await connect(url, extra_headers=headers, max_size=None)

        async with socket:
            self._mode = "elevenlabs"
            self._connected = True
            log.info("stt provider: ElevenLabs Scribe (%s) connected", MODEL_ID)
            sender = asyncio.create_task(self._send_audio(socket), name="stt-send")
            try:
                await self._read(socket)
            finally:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender

    async def _send_audio(self, socket: Any) -> None:
        while True:
            pcm = await self._audio.get()
            await socket.send(
                json.dumps(
                    {
                        "message_type": "input_audio_chunk",
                        "audio_base_64": base64.b64encode(pcm).decode("ascii"),
                        "sample_rate": SAMPLE_RATE,
                    }
                )
            )

    async def _read(self, socket: Any) -> None:
        async for raw in socket:
            if isinstance(raw, bytes):
                continue  # the realtime API is JSON in both directions
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("scribe sent something that is not JSON")
                continue
            kind = message.get("message_type")
            if kind == "partial_transcript":
                await self._on_partial_text(message.get("text") or "")
            elif kind in ("committed_transcript", "committed_transcript_with_timestamps"):
                await self._on_commit(message.get("text") or "")
            elif kind == "session_started":
                log.debug("scribe session %s", message.get("session_id"))
            elif kind in _FATAL:
                raise _Fatal(message.get("error") or kind)
            elif kind == "error":
                log.error("scribe error: %s", message.get("error"))
            elif kind is not None:
                log.debug("scribe: unhandled %s", kind)

    # -- the two outputs ---------------------------------------------------

    async def _on_partial_text(self, text: str) -> None:
        """Forwards to the strip and goes no further (CT-001's docstring)."""
        text = text.strip()
        if text:
            await self._on_partial(text)

    async def _on_commit(self, text: str) -> None:
        """A committed segment becomes exactly one `utterance` Signal."""
        text = text.strip()
        if not text:
            return
        # contracts.py: `from_playback` is set by the STT producer so the
        # buffer can reject the system's own voice without having to ask what
        # the text means.
        echo = bool(self._playback and self._playback.is_echo(text))
        if echo:
            log.info("dropping our own voice: %r", text)
        signal = Signal(
            kind="utterance", payload={"text": text}, from_playback=echo
        )
        await _maybe_await(self._on_signal(signal))

    # -- inputs, called from server/ws.py ----------------------------------

    async def feed(self, pcm: bytes) -> None:
        """Push audio in — live from the board, or the demo track.

        A no-op in browser mode, where the board is already doing the
        transcription and the bytes are not wanted here. In Scribe mode the
        audio is queued whether or not the socket is up: `_connected` is false
        for the whole of a reconnect, which is precisely when someone is most
        likely to be mid-sentence. Queueing it is also what wakes the session.
        """
        if not pcm or self._mode.startswith("browser"):
            return
        try:
            self._audio.put_nowait(pcm)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._audio.get_nowait()  # stale audio is the cheapest thing to lose
            with contextlib.suppress(asyncio.QueueFull):
                self._audio.put_nowait(pcm)

    async def on_transcript(self, text: str, final: bool) -> None:
        """The browser path: CT-007 `{"type":"transcript","text":…,"final":…}`.

        Partials go to the strip, finals become `utterance` Signals — the same
        two outputs as the Scribe path, so nothing downstream can tell which
        provider produced a given segment.
        """
        if self._mode == "elevenlabs":
            # Both providers running at once would double every utterance.
            log.debug("ignoring browser transcript: Scribe is live")
            return
        if final:
            await self._on_commit(text)
        else:
            await self._on_partial_text(text)

    async def inject(self, text: str) -> None:
        """A typed utterance (DBG-102) entering where a real one enters."""
        await self._on_commit(text)

    async def close(self) -> None:
        self._closing.set()


class _Fatal(RuntimeError):
    """A failure retrying cannot fix: bad key, no quota, malformed request."""
