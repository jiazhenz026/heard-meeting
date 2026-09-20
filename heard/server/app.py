"""The one process. FastAPI app, lifespan, background tasks, board.

    STT → Scribe (store.commit) → classifier → front desk → sub-agents
                                → notes agent
    say → harness → floor → TTS → board

One loop, one origin: the built board and the socket are served from here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from heard.classifier import Classifier
from heard.config import CONFIG, Config
from heard.contracts import Signal, now
from heard.frontdesk import FrontDesk
from heard.harness import Harness
from heard.notes import NotesAgent
from heard.research import Researcher
from heard.speech.playback import Playback
from heard.speech.stt import Stt
from heard.speech.tts import Voice
from heard.store import Store

from .ws import Hub, Wiring

log = logging.getLogger("heard.server.app")

ROOT = Path(__file__).resolve().parent.parent.parent
BOARD_DIST = ROOT / "board" / "dist"

HEARD_CARD_SUMMARY = (
    "Heard! is an ambient meeting agent: a plain-code scribe writes the transcript, a small Nemotron classifier "
    "sorts each few seconds of speech and lands a placeholder card in under 5 s, a Nemotron notes agent keeps live "
    "meeting notes, and one resident front-desk agent on the Claude Agent SDK decides whether to ignore, investigate "
    "with a web-searching sub-agent, or speak — and a plain-code harness only lets it speak when asked or when a "
    "finding changes the decision. Speech in and out is ElevenLabs."
)
HEARD_CARD_NOTES = [
    "Scribe: no model. STT commit → anchored markdown line. Cannot hallucinate, cannot fail.",
    "Classifier: Nemotron via NVIDIA NIM, JSON only, every 2–5 s. Creates placeholder cards directly.",
    "Notes agent: Nemotron via NIM, rewrites notes.md every ~20 s. The shared context window.",
    "Front desk: one long-lived Claude Agent SDK session. Tools: recall · investigate · say · note.",
    "Sub-agents: Claude Agent SDK runs with WebSearch/WebFetch. Result → HTML page + card summary.",
    "Harness: say needs reason=asked|finding, checked in code; 1 unsolicited line / 90 s; waits for a gap.",
    "Speech: ElevenLabs Scribe v2 realtime in, ElevenLabs TTS out, echo tagging on the way back in.",
    "Board: React + Vite over one WebSocket; full-state resync on every push.",
]


@dataclass(slots=True)
class Settings:
    host: str = CONFIG.host
    port: int = CONFIG.port
    audio: bool = True


class Heard:
    def __init__(self, settings: Settings, config: Config = CONFIG) -> None:
        self.settings = settings
        self.config = config
        self.store = Store(Path(config.data_dir))
        self.playback = Playback(config)
        self.hub: Hub | None = None
        self.voice: Voice | None = None
        self.stt: Stt | None = None
        self.harness = Harness(self.store, None, config=config)
        self.researcher = Researcher(self.store, on_done=self._on_finding, config=config)
        self.frontdesk = FrontDesk(self.store, self.harness, self.researcher, config=config)
        self.classifier = Classifier(self.store, on_signal=self.frontdesk.on_signal, config=config)
        self.notes = NotesAgent(self.store, config=config)
        self.tasks: list[asyncio.Task[None]] = []

    # -- assembly ----------------------------------------------------------

    def seed(self) -> None:
        c = self.store.create_card("Heard!", named_by="speaker", seeded=True,
                                   one_liner="the meeting board that listens, goes and finds out, and speaks up")
        c.summary = HEARD_CARD_SUMMARY
        c.notes = list(HEARD_CARD_NOTES)
        c.status = "READY"
        c.page = True
        self.store.card_page_path(c.id).write_text(_heard_page(), encoding="utf-8")

    def build(self) -> Hub:
        self.hub = Hub(Wiring(
            state=self.state,
            on_inject=self.on_inject,
            on_audio_chunk=self.on_audio_chunk,
            on_transcript=self.on_transcript,
            on_reset=self.on_reset,
        ))
        self.voice = Voice(
            send_audio=self.hub.send_audio, playback=self.playback,
            config=self.config if self.settings.audio else _silent(self.config),
        )
        self.harness.voice = self.voice
        self.stt = Stt(on_partial=self.on_partial, on_signal=self.on_signal,
                       playback=self.playback, config=self.config)
        self.store.on_change(self.push_board)
        return self.hub

    def start(self) -> None:
        assert self.stt is not None
        self._spawn("stt", self.stt.run())
        self._spawn("classifier", self.classifier.run())
        self._spawn("notes", self.notes.run())
        self._spawn("frontdesk", self.frontdesk.run())

    def _spawn(self, name: str, coro: Any) -> None:
        self.tasks.append(asyncio.create_task(_supervise(name, coro), name=name))

    async def stop(self) -> None:
        await self.frontdesk.close()
        await self.researcher.close()
        await self.classifier.close()
        await self.notes.close()
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.stt is not None:
            await self.stt.close()
        if self.voice is not None:
            await self.voice.close()

    # -- state -----------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        snap = self.store.snapshot(speaking=self.playback.speaking())
        snap["health"] = self.health()
        return snap

    def health(self) -> dict[str, Any]:
        return {
            "stt": self.stt.mode if self.stt else None,
            "tts": self.voice.mode if self.voice else None,
            "frontdesk": self.frontdesk.mode,
            "frontdesk_busy": self.frontdesk.busy,
            "classifier_fires": self.classifier.fires,
            "classifier_error": self.classifier.last_error,
            "notes_passes": self.notes.passes,
            "notes_error": self.notes.last_error,
            "frontdesk_error": self.frontdesk.last_error,
            "wakes": self.frontdesk.wakes,
            "research_running": len(self.store.running_tasks()),
        }

    async def push_board(self) -> None:
        if self.hub is not None:
            await self.hub.push(self.state())

    # -- inputs ----------------------------------------------------------------

    async def on_partial(self, text: str) -> None:
        self.store.heard_partial(text)
        if self.hub is not None:
            await self.hub.send_partial(text)

    async def on_signal(self, signal: Signal) -> None:
        """Every committed utterance, from any STT provider or /inject."""
        if signal.from_playback:
            self.store.log_event("echo", signal.payload.get("text", ""))
            return
        text = str(signal.payload.get("text") or "").strip()
        if not text:
            return
        self.store.commit(text, str(signal.payload.get("speaker") or "?"))
        self.classifier.notify()

    async def on_inject(self, text: str, speaker: str = "?") -> None:
        await self.on_signal(Signal(kind="utterance", payload={"text": text, "speaker": speaker}))

    async def on_audio_chunk(self, pcm: bytes) -> None:
        if self.stt is not None:
            await self.stt.feed(pcm)

    async def on_transcript(self, text: str, final: bool) -> None:
        if self.stt is not None:
            await self.stt.on_transcript(text, final)

    async def on_reset(self) -> None:
        """Back to an empty meeting. The front desk session is kept."""
        self.store.transcript.clear()
        self.store.cards.clear()
        self.store.tasks.clear()
        self.store.said.clear()
        self.store.log.clear()
        self.store.asked = None
        self.store.expanded = None
        self.store.focus = None
        self.store.notes = ""
        self.store.notes_version = 0
        self.store.started_at = now()
        self.store.reset_files()
        self.playback.clear()
        self.seed()
        self.store.touch()

    async def _on_finding(self, task: Any) -> None:
        await self.frontdesk.on_finding(task)


def _silent(config: Config) -> Config:
    from dataclasses import replace

    return replace(config, tts_provider="off")


async def _supervise(name: str, coro: Any) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("background task %s died", name)


class InjectBody(BaseModel):
    text: str
    speaker: str = "?"


def create_app(settings: Settings | None = None, config: Config = CONFIG) -> FastAPI:
    settings = settings or Settings()
    heard = Heard(settings, config)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        heard.seed()
        app.state.hub = heard.build()
        heard.start()
        try:
            yield
        finally:
            await heard.stop()

    app = FastAPI(title="Heard!", lifespan=lifespan)
    app.state.heard = heard

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "now": now(), "boards": heard.hub.connections if heard.hub else 0, **heard.health()}

    @app.get("/transcript.md", response_class=PlainTextResponse)
    async def transcript_md() -> str:
        return heard.store.transcript_path.read_text(encoding="utf-8")

    @app.get("/notes.md", response_class=PlainTextResponse)
    async def notes_md() -> str:
        return heard.store.notes

    @app.get("/cards/{card_id}", response_class=HTMLResponse)
    async def card_page(card_id: str) -> str:
        path = heard.store.card_page_path(card_id)
        if not path.is_file():
            raise HTTPException(404, "no page for this card yet")
        return _light(path.read_text(encoding="utf-8"))

    @app.post("/inject")
    async def inject(body: InjectBody) -> dict[str, Any]:
        await heard.on_inject(body.text, body.speaker)
        return {"ok": True, "utterances": len(heard.store.transcript)}

    @app.delete("/cards/{card_id}")
    async def delete_card(card_id: str) -> dict[str, Any]:
        if not heard.store.delete_card(card_id):
            raise HTTPException(404, "no such card")
        return {"ok": True}

    @app.post("/expand/{card_id}")
    async def expand(card_id: str) -> dict[str, Any]:
        heard.store.expand(card_id if card_id != "none" else None)
        return {"ok": True}

    @app.post("/reset")
    async def reset() -> dict[str, Any]:
        await heard.on_reset()
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        if heard.hub is None:
            await websocket.close()
            return
        await heard.hub.endpoint(websocket)

    if BOARD_DIST.is_dir():
        app.mount("/", StaticFiles(directory=BOARD_DIST, html=True), name="board")
    else:
        @app.get("/", response_class=HTMLResponse)
        async def placeholder() -> str:
            return "<h1>Heard!</h1><p>The board is not built. <code>cd board && npm run build</code>, or <code>npm run dev</code>.</p>"

    return app


_LIGHT_OVERRIDE = """<style id="heard-light">
  :root { color-scheme: light !important; }
  body { background: #ffffff !important; color: #1b1f24 !important; }
  h1, h2, h3, strong { color: #1b1f24 !important; }
  .kicker, .brief, .meta { color: #656c75 !important; }
  a { color: #2f5fb3 !important; }
  code, pre { background: #eef1f4 !important; color: #1b1f24 !important; }
  h2 { border-bottom-color: #e3e4e6 !important; }
</style></head>"""


def _light(html_text: str) -> str:
    """Pages written by an earlier build were dark. Serve every page light."""
    if 'id="heard-light"' in html_text:
        return html_text
    return html_text.replace("</head>", _LIGHT_OVERRIDE, 1)


def _heard_page() -> str:
    items = "".join(f"<li>{n}</li>" for n in HEARD_CARD_NOTES)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Heard! — how it is built</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin:0; background:#ffffff; color:#1b1f24; font:16px/1.55 'IBM Plex Sans', 'Helvetica Neue', Helvetica, Arial, sans-serif; }}
  main {{ max-width: 900px; margin: 0 auto; padding: 28px 32px 60px; }}
  .kicker {{ color:#5b6b7c; font-size:14px; }}
  h1 {{ font-size:28px; margin:6px 0 10px; }} h2 {{ color:#1b1f24; font-size:17px; margin:26px 0 8px; }}
  li {{ margin:6px 0; }} pre {{ background:#eef1f4; padding:14px; border-radius:6px; overflow:auto; font-size:13px; }}
</style></head><body><main>
<div class="kicker">How Heard! is built</div>
<h1>Five parts, two with no model in them</h1>
<p>{HEARD_CARD_SUMMARY}</p>
<h2>Pipeline</h2>
<pre>STT (ElevenLabs Scribe v2)
  → SCRIBE  plain code → transcript.md
      → CLASSIFIER  Nemotron/NIM, every 2–5 s → placeholder card (&lt; 5 s) + signal
      → NOTES AGENT Nemotron/NIM, every ~20 s → notes.md (shared context)
  → FRONT DESK  Claude Agent SDK, one resident session, woken by signals
      tools: recall · investigate → SUB-AGENT (WebSearch) → card page · say → HARNESS → floor → TTS · note</pre>
<h2>Components</h2><ul>{items}</ul>
<h2>Why it stays quiet</h2>
<ul><li>The scribe, classifier and notes agent have no mouth.</li><li>The front desk only wakes on a signal worth waking for.</li>
<li><code>say</code> must carry a reason the runtime can check against state.</li><li>One unsolicited line per 90 seconds.</li>
<li>Speech waits for a gap in the room, or goes to the card instead.</li></ul>
</main></body></html>"""
