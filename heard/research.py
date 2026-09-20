"""Sub-agents. One `investigate` = one Claude Agent SDK run with web search.

We do not write a research loop. The SDK's `query()` runs a full agent with
WebSearch and WebFetch; its final message is the report. The report becomes an
HTML page under /cards/{id} and a two-sentence summary on the card, and the
front desk is woken with the summary.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any, Awaitable, Callable

import markdown

from .config import CONFIG, Config
from .contracts import now
from .store import Store, Task

log = logging.getLogger("heard.research")

RESEARCHER_PROMPT = """You are a researcher working for Heard!, an assistant sitting in a live meeting. You get one brief about a project, product idea or claim the room is discussing. Go and find out, fast, using web search. Prefer primary sources: Devpost and hackathon project pages for "has this been done", company sites and app stores for products, docs for technical claims.

Budget: at most 4 searches and 2 page fetches, then write. Speed matters more than completeness: the room decides in about a minute. Do not narrate what you are doing.

Return ONLY a markdown report in exactly this shape:

SUMMARY: <one or two sentences, the single most useful thing the room does not know. Concrete: names, counts, dates.>

# <report title>

## Verdict
<2-4 bullets. If the brief asks whether something has been done: say plainly DONE BEFORE / NOVEL / UNCLEAR, and list the closest existing things by name.>

## What exists
<bullets, each: **Name** — what it does — where (Devpost 2025, App Store, ...) — link>

## Notes for the room
<bullets: feasibility, audience, the strongest objection, anything the brief asked for>

## Sources
- <url>
- <url>
"""


class Researcher:
    def __init__(
        self,
        store: Store,
        *,
        on_done: Callable[[Task], Awaitable[None] | None],
        config: Config = CONFIG,
    ) -> None:
        self.store = store
        self.on_done = on_done
        self.config = config
        self._running: dict[str, asyncio.Task[None]] = {}

    @property
    def pool_full(self) -> bool:
        return len(self._running) >= self.config.max_research

    def dispatch(self, card_id: str, brief: str, *, unprompted: bool) -> Task | None:
        if self.pool_full:
            return None
        task = self.store.create_task(card_id, brief, unprompted=unprompted)
        card = self.store.cards.get(card_id)
        self.store.work_start(f"task:{task.id}", f"Looking into {card.title if card else card_id}")
        t = asyncio.create_task(self._run(task), name=f"research-{task.id}")
        self._running[task.id] = t
        t.add_done_callback(lambda _t, tid=task.id: self._running.pop(tid, None))
        return task

    async def close(self) -> None:
        for t in list(self._running.values()):
            t.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)

    async def _run(self, task: Task) -> None:
        card = self.store.cards.get(task.card_id)
        title = card.title if card else task.card_id
        prompt = _brief(title, card.one_liner if card else "", task.brief)
        started = now()
        try:
            report = await asyncio.wait_for(_agent_report(task, prompt, self.store, self.config),
                                            timeout=self.config.research_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("research %s failed: %s: %s", task.id, type(exc).__name__, exc)
            self.store.finish_task(task.id, summary=f"research failed: {type(exc).__name__}", sources=[], failed=True)
            self.store.work_done(f"task:{task.id}")
            return
        summary, body, sources = _split(report)
        if not body.strip():
            self.store.finish_task(task.id, summary="research returned nothing", sources=[], failed=True)
            return
        page = _render_page(title, task.brief, body, sources, now() - started)
        if task.card_id in self.store.cards:
            self.store.set_card_page(task.card_id, page, summary)
        done = self.store.finish_task(task.id, summary=summary, sources=sources)
        self.store.work_done(f"task:{task.id}")
        log.info("research %s done in %.0fs · %d sources", task.id, now() - started, len(sources))
        if done is None:
            return  # the card was deleted while this ran; nothing to wake for
        r = self.on_done(done)
        if asyncio.iscoroutine(r):
            await r


def _brief(title: str, one_liner: str, brief: str) -> str:
    return (
        f"Subject: {title}" + (f" — {one_liner}" if one_liner else "") +
        f"\n\nBrief from the front desk: {brief}\n\n"
        "The room is deciding right now; what would change their mind?"
    )


async def _agent_report(task: Task, prompt: str, store: Store, config: Config) -> str:
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                                  TextBlock, ToolUseBlock, query)

    opts: dict[str, Any] = dict(
        system_prompt=RESEARCHER_PROMPT,
        tools=["WebSearch", "WebFetch"],
        allowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        setting_sources=[],
        cwd=str(store.data_dir),
        max_turns=10,
        effort=config.research_effort or None,
    )
    if config.research_model:
        opts["model"] = config.research_model
    options = ClaudeAgentOptions(**opts)

    searches = 0
    last_text = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    searches += 1
                    q = block.input.get("query") or block.input.get("url") or ""
                    task.status_line = f"{block.name.lower()}: {str(q)[:60]}"
                    store.touch()
                elif isinstance(block, TextBlock) and block.text.strip():
                    last_text = block.text
        elif isinstance(message, ResultMessage):
            if isinstance(message.result, str) and message.result.strip():
                last_text = message.result
    task.status_line = "writing"
    store.touch()
    return last_text


_SUMMARY = re.compile(r"^\s*SUMMARY:\s*(.+?)\s*$", re.MULTILINE)
_URL = re.compile(r"https?://[^\s)\]>*]+")


_MD = re.compile(r"(\*\*|__|`|\*|_(?=\w)|(?<=\w)_)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def plain(text: str) -> str:
    """A summary is spoken and shown on a note: no markdown in it."""
    return _MD.sub("", _MD_LINK.sub(r"\1", text)).strip()


def _split(report: str) -> tuple[str, str, list[str]]:
    m = _SUMMARY.search(report)
    summary = plain(m.group(1)) if m else ""
    body = _SUMMARY.sub("", report, count=1).strip() if m else report.strip()
    if not summary:
        # First non-heading line, capped.
        for line in body.splitlines():
            s = line.strip().lstrip("#-* ").strip()
            if len(s) > 20:
                summary = plain(s)[:220]
                break
    seen: list[str] = []
    for u in _URL.findall(report):
        u = u.rstrip(".,;")
        if u not in seen:
            seen.append(u)
    return summary, body, seen[:12]


def _render_page(title: str, brief: str, body_md: str, sources: list[str], secs: float) -> str:
    inner = markdown.markdown(body_md, extensions=["extra", "sane_lists"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)} — Heard!</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: light; }}
  body {{ margin: 0; background: #ffffff; color: #1b1f24; font: 16px/1.55 'IBM Plex Sans', 'Helvetica Neue', Helvetica, Arial, sans-serif; }}
  main {{ max-width: 860px; margin: 0 auto; padding: 28px 32px 60px; }}
  .kicker {{ color: #5b6b7c; font-size: 14px; }}
  h1 {{ font-size: 26px; margin: 6px 0 4px; }}
  h2 {{ font-size: 17px; color: #1b1f24; margin: 26px 0 8px; border-bottom: 1px solid #e2e7ec; padding-bottom: 6px; }}
  a {{ color: #2f5fb3; text-decoration: none; }} a:hover {{ text-decoration: underline; }}
  li {{ margin: 4px 0; }} code {{ background: #eef1f4; padding: 1px 5px; border-radius: 3px; }}
  .brief {{ color: #5b6b7c; font-style: italic; margin: 0 0 10px; }}
  .meta {{ color: #93a1af; font-size: 12px; margin-top: 30px; }}
</style></head><body><main>
<div class="kicker">Investigated by Heard!</div>
<h1>{html.escape(title)}</h1>
<p class="brief">{html.escape(brief)}</p>
{inner}
<p class="meta">{len(sources)} sources · {secs:.0f}s · researched by a sub-agent while the meeting continued</p>
</main></body></html>"""
