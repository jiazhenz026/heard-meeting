"""Nemotron through NIM. One client, one call shape, loose JSON parsing.

The parsing is ported from the kitchen build: reasoning models wrap thinking in
<think> blocks, some answer with a fence, some with prose in front. A brace
scanner finds the JSON wherever it is, which matters because the model id is
configuration and changes between runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from typing import Any

from .config import CONFIG

log = logging.getLogger("heard.nim")

_client: Any = None
_CALLS: deque[float] = deque()
#: After a 429 nothing is sent until this moment: retrying into a throttled
#: key only digs the hole deeper.
_cool_until: float = 0.0


def cooling() -> float:
    """Seconds left in the post-429 cooldown, 0 when clear."""
    return max(0.0, _cool_until - time.monotonic())


class RateLimited(RuntimeError):
    pass


def _get_client() -> Any:
    """One AsyncOpenAI, built lazily and reused."""
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(
            base_url=CONFIG.nvidia_base_url,
            api_key=CONFIG.nvidia_api_key or "missing",
            timeout=CONFIG.nim_timeout_s,
            max_retries=0,
        )
    return _client


def _pace() -> None:
    t = time.monotonic()
    if t < _cool_until:
        raise RateLimited(f"cooling down after a 429; {(_cool_until - t):.0f}s left")
    while _CALLS and t - _CALLS[0] > 60.0:
        _CALLS.popleft()
    if len(_CALLS) >= CONFIG.max_rpm:
        raise RateLimited(f"{len(_CALLS)} NIM calls in the last minute; ceiling {CONFIG.max_rpm}")
    _CALLS.append(t)


async def chat(system: str, user: str, *, max_tokens: int | None = None, timeout: float | None = None) -> str:
    """One completion. Raises on timeout, rate limit or transport failure."""
    if not CONFIG.has_nvidia:
        raise RuntimeError("NVIDIA_API_KEY is unset")
    _pace()
    request: dict[str, Any] = {
        "model": CONFIG.model_id,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max_tokens or CONFIG.nim_max_tokens,
    }
    if CONFIG.nim_disable_thinking:
        request["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    started = time.perf_counter()
    budget = timeout or CONFIG.nim_timeout_s
    client = _get_client().with_options(timeout=budget)
    try:
        resp = await asyncio.wait_for(client.chat.completions.create(**request), timeout=budget + 1.0)
    except Exception as exc:
        code = getattr(exc, "status_code", None)
        if code == 429:
            # The provider's window is full. Stop sending for a few seconds;
            # the caller keeps its lines and tries again after the cooldown.
            global _cool_until
            _cool_until = time.monotonic() + CONFIG.nim_cooldown_s
            log.warning("nim 429; cooling down %.0fs", CONFIG.nim_cooldown_s)
            raise
        if code != 503:
            raise
        # A 503 is a busy replica, not our quota: one short retry is worth it.
        log.warning("nim 503; retrying once after 1.5 s")
        await asyncio.sleep(1.5)
        _CALLS.append(time.monotonic())
        resp = await asyncio.wait_for(client.chat.completions.create(**request), timeout=budget + 1.0)
    return _text_of(resp, started)


def _text_of(resp: Any, started: float) -> str:
    text = ""
    choices = getattr(resp, "choices", None) or []
    if choices:
        text = getattr(choices[0].message, "content", None) or ""
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    log.debug("nim %.0f ms · %d chars", (time.perf_counter() - started) * 1000, len(text))
    return text


async def chat_json(system: str, user: str, **kw: Any) -> dict[str, Any] | None:
    text = await chat(system, user, **kw)
    blobs = json_blobs(strip_reasoning(text))
    for b in blobs:
        if isinstance(b, dict):
            return b
    log.warning("nim returned no JSON object: %r", text[:200])
    return None


# -- parsing ---------------------------------------------------------------

_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_OPEN_THINK = re.compile(r"<(think|thinking|reasoning)>", re.IGNORECASE)
_FENCE = re.compile(r"```(?:json|markdown|md)?\s*(.*?)```", re.DOTALL)


def strip_reasoning(text: str) -> str:
    text = _THINK.sub(" ", text)
    if _OPEN_THINK.search(text):
        text = _OPEN_THINK.split(text)[0]
    return text.strip()


def strip_fence(text: str) -> str:
    m = _FENCE.search(text)
    return m.group(1).strip() if m else text.strip()


def json_blobs(text: str) -> list[Any]:
    out: list[Any] = []
    for candidate in [m.group(1) for m in _FENCE.finditer(text)] + [text]:
        depth, start, in_str, esc = 0, None, False, False
        for i, ch in enumerate(candidate):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "[{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        out.append(json.loads(candidate[start : i + 1]))
                    except json.JSONDecodeError:
                        pass
                    start = None
                elif depth < 0:
                    depth = 0
        if out:
            return out
    return out
