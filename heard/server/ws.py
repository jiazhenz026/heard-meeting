"""The board socket. Ported from the kitchen build, envelopes renamed.

    down  state    the whole snapshot, after every change (debounced)
    down  partial  an in-progress transcript segment, strip only
    down  audio    a spoken line, its reason, and the audio or null

    up    hello · audio_chunk · transcript · inject · reset

Reconnect resyncs in full: the board asks for nothing and tracks no version.
A slow board is dropped rather than allowed to stall the one loop.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import itertools
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect

from heard.contracts import now

log = logging.getLogger("heard.server.ws")

_SEND_QUEUE_CAP = 64
_ids = itertools.count(1)
UP_TYPES = {"hello", "audio_chunk", "transcript", "inject", "reset"}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(slots=True)
class Wiring:
    state: Callable[[], Any]
    on_inject: Callable[[str, str], Any] | None = None
    on_audio_chunk: Callable[[bytes], Any] | None = None
    on_transcript: Callable[[str, bool], Any] | None = None
    on_reset: Callable[[], Any] | None = None


@dataclass(slots=True, eq=False)
class _Conn:
    id: int
    ws: WebSocket
    queue: asyncio.Queue[dict[str, Any]]
    writer: asyncio.Task[None] | None = field(default=None, repr=False)


class Hub:
    def __init__(self, wiring: Wiring) -> None:
        self._wiring = wiring
        self._conns: set[_Conn] = set()

    @property
    def connections(self) -> int:
        return len(self._conns)

    async def push(self, state: dict[str, Any]) -> None:
        await self._broadcast({"type": "state", **state})

    async def send_partial(self, text: str) -> None:
        await self._broadcast({"type": "partial", "text": text})

    async def send_audio(self, text: str, target: str, audio_b64: str | None) -> None:
        await self._broadcast({"type": "audio", "text": text, "target": target, "audio_b64": audio_b64})

    async def endpoint(self, websocket: WebSocket) -> None:
        await websocket.accept()
        conn = _Conn(id=next(_ids), ws=websocket, queue=asyncio.Queue(maxsize=_SEND_QUEUE_CAP))
        conn.writer = asyncio.create_task(self._write(conn), name=f"ws-{conn.id}")
        self._conns.add(conn)
        log.info("board %s connected (%s open)", conn.id, len(self._conns))
        await self.resync(conn)
        try:
            while True:
                message = await websocket.receive_json()
                await self._on_message(conn, message)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("board %s: socket error", conn.id)
        finally:
            self._conns.discard(conn)
            if conn.writer is not None:
                conn.writer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await conn.writer
            log.info("board %s disconnected (%s open)", conn.id, len(self._conns))

    async def resync(self, conn: _Conn) -> None:
        try:
            state = await _maybe_await(self._wiring.state())
        except Exception:
            log.exception("could not build state for resync")
            return
        if isinstance(state, dict):
            self._enqueue(conn, {"type": "state", **state})

    async def _on_message(self, conn: _Conn, message: Any) -> None:
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind not in UP_TYPES:
            log.warning("board %s: unknown up-message %r", conn.id, kind)
            return
        if kind == "hello":
            await self.resync(conn)
        elif kind == "inject":
            if self._wiring.on_inject is not None:
                await _maybe_await(self._wiring.on_inject(str(message.get("text") or ""),
                                                          str(message.get("speaker") or "?")))
        elif kind == "audio_chunk":
            if self._wiring.on_audio_chunk is None:
                return
            try:
                pcm = base64.b64decode(message.get("pcm_b64") or "")
            except Exception:
                return
            await _maybe_await(self._wiring.on_audio_chunk(pcm))
        elif kind == "transcript":
            if self._wiring.on_transcript is not None:
                await _maybe_await(self._wiring.on_transcript(str(message.get("text") or ""),
                                                              bool(message.get("final"))))
        elif kind == "reset":
            if self._wiring.on_reset is not None:
                log.warning("board %s: RESET", conn.id)
                await _maybe_await(self._wiring.on_reset())

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        if "now" not in payload:
            payload["now"] = now()
        for conn in list(self._conns):
            self._enqueue(conn, payload)

    def _enqueue(self, conn: _Conn, payload: dict[str, Any]) -> None:
        try:
            conn.queue.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("board %s is not reading; dropping it", conn.id)
            self._conns.discard(conn)
            if conn.writer is not None:
                conn.writer.cancel()

    async def _write(self, conn: _Conn) -> None:
        try:
            while True:
                payload = await conn.queue.get()
                await conn.ws.send_json(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._conns.discard(conn)
            with contextlib.suppress(Exception):
                await conn.ws.close()
