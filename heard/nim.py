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


class RateLimited(RuntimeError):
    pass


_clients: dict[str, Any] = {}
_turn = 0
#: Per key: the moment it may be used again after a 429.
_benched: dict[str, float] = {}
_BENCH_S = 20.0


def _client_for(key: str) -> Any:
    from openai import AsyncOpenAI

    if key not in _clients:
        _clients[key] = AsyncOpenAI(base_url=CONFIG.nvidia_base_url, api_key=key,
                                    timeout=CONFIG.nim_timeout_s, max_retries=0)
    return _clients[key]


def _key_order() -> list[str]:
    """Every key, starting from the next one in rotation; benched keys last."""
    global _turn
    keys = CONFIG.nvidia_keys or ["missing"]
    start = _turn % len(keys)
    _turn += 1
    order = keys[start:] + keys[:start]
    t = time.monotonic()
    return sorted(order, key=lambda k: _benched.get(k, 0.0) > t)  # stable: fresh keys first


def key_state() -> dict[str, Any]:
    t = time.monotonic()
    keys = CONFIG.nvidia_keys
    return {"keys": len(keys), "benched": sum(1 for k in keys if _benched.get(k, 0.0) > t)}


def _pace() -> None:
    t = time.monotonic()
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
    resp = None
    last: Exception | None = None
    order = _key_order()
    for n, key in enumerate(order):
        client = _client_for(key).with_options(timeout=budget)
        try:
            resp = await asyncio.wait_for(client.chat.completions.create(**request), timeout=budget + 1.0)
            break
        except Exception as exc:
            last = exc
            code = getattr(exc, "status_code", None)
            if code == 429:
                # This key's window is full. Bench it and try the next one now.
                _benched[key] = time.monotonic() + _BENCH_S
                log.warning("nim 429 on key %d of %d%s", CONFIG.nvidia_keys.index(key) + 1 if key in CONFIG.nvidia_keys else 0,
                            len(order), "; trying the next key" if n + 1 < len(order) else "")
                continue
            if code == 503 and n + 1 == len(order):
                # A busy replica, and no other key to try: one short retry.
                log.warning("nim 503; retrying once after 2 s")
                await asyncio.sleep(2.0)
                resp = await asyncio.wait_for(client.chat.completions.create(**request), timeout=budget + 1.0)
                break
            if code == 503:
                continue
            raise
    if resp is None:
        assert last is not None
        raise last
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
