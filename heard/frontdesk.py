"""The front desk: the one resident agent that decides what the room hears.

It runs on the Claude Agent SDK as a single long-lived session, so it keeps the
whole meeting in context. It is woken by a classifier signal or a returning
sub-agent; one wake at a time, pending wakes coalesce, a direct address jumps
the queue. It has four tools and nothing else:

    recall(query)               the full transcript, or a search over it
    investigate(card, brief)    dispatch a sub-agent; returns at once
    say(text, reason, refs)     speaks, if the harness lets it
    note(card, text)            pin a line on a card

It does not write the notes and does not create cards.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from .classifier import Signal, to_json
from .config import CONFIG, Config
from .contracts import now
from .harness import Harness
from .research import Researcher
from .store import Store, Task

log = logging.getLogger("heard.frontdesk")

SYSTEM = """You are Heard!, an ambient assistant sitting in a live working meeting — right now, a hackathon team deciding what to build. You are not a chatbot. Nobody is typing to you. You are woken with a short signal about what was just said, plus the live meeting notes, and you decide whether anything needs doing. Most of the time the answer is nothing.

What you hold to:
- The board is where your work goes. Speaking takes the floor from three people; it is a separate, worse thing you do rarely.
- You say nothing the room already knows. They were there.
- A finding arriving is not by itself a reason to speak. It is a reason to speak only if it changes what the room is about to do (they think nobody has done it and six people have; they are assuming X and X is false) — or if they asked.
- You never narrate: no "I'm looking into that", no "good point", no "just to confirm". If a line carries no fact, it does not get said.
- If you are not sure whether to speak, that is the answer.
- Late is worse than silent. If the moment passed, the card gets it; the room does not.

What you do on a wake:
1. If the signal introduces a new subject (a card was just created), call `investigate` on it, unprompted, with a brief that asks the two questions that matter for a hackathon idea: has this been done (check Devpost and past hackathons by name), and who is it for / who would pay. Add any specific question the room raised. Say nothing.
2. If the signal lists a question worth investigating about an existing card, `investigate` it. Say nothing.
3. If someone addressed you (addressed=true), answer with `say(reason="asked")`. Answer from the notes, the card summaries and the transcript — call `recall` first if you need more. Two short sentences, under 35 words total, facts first; anything longer is cut off mid-sentence by the runtime. intent "opinion" = give your read of the subject being discussed, grounded in any finding you have; intent "how_built" = describe how Heard! itself is built from the seeded "Heard!" card (call `recall` with query "Heard! card" if you need it); intent "lookup" = `investigate`, then say one sentence that you will report back is NOT allowed — say nothing, the task rail shows it.
4. If a sub-agent returned (the wake says so), decide: does the finding contradict something the room said or is about to act on? The clearest case: the room is agreeing to build something and the finding says it has been built before — say so, with names and a count, `say(reason="finding", refs=[task_id])`, under 35 words. If it merely adds colour, use `note` on the card or do nothing.
5. Otherwise do nothing.

Your tools may refuse a `say`; the refusal tells you why. Do not argue with it or retry the same line; put the fact on the card with `note` instead.

When you have nothing to do, reply with exactly: IGNORE
Never reply with prose meant for the room; only `say` reaches them.
"""


class FrontDesk:
    def __init__(
        self,
        store: Store,
        harness: Harness,
        researcher: Researcher,
        *,
        config: Config = CONFIG,
    ) -> None:
        self.store = store
        self.harness = harness
        self.researcher = researcher
        self.config = config
        self._client: Any = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._closing = asyncio.Event()
        self._busy = False
        self.wakes = 0
        self.last_wake_at: float | None = None
        self.last_error: str | None = None
        self.mode = "off" if config.frontdesk == "off" else "starting"

    @property
    def busy(self) -> bool:
        return self._busy

    # -- inputs --------------------------------------------------------------

    async def on_signal(self, signal: Signal) -> None:
        """From the classifier. Only some signals are worth a wake."""
        worth = (
            signal.get("new_subject") is not None
            or signal.get("addressed")
            or any(q.get("worth_investigating") for q in signal.get("questions") or [])
            or signal.get("salience", 0) >= 2
        )
        if not worth:
            return
        await self._queue.put({"kind": "signal", "signal": signal, "priority": bool(signal.get("addressed"))})

    async def on_finding(self, task: Task) -> None:
        await self._queue.put({"kind": "finding", "task_id": task.id, "priority": False})

    # -- the loop --------------------------------------------------------------

    async def run(self) -> None:
        if self.config.frontdesk == "off":
            log.warning("front desk off (HEARD_FRONTDESK=off)")
            await self._closing.wait()
            return
        try:
            await self._connect()
        except Exception as exc:
            self.mode = f"down ({type(exc).__name__})"
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.error("front desk could not start: %s", self.last_error)
            await self._closing.wait()
            return
        self.mode = "up"
        while not self._closing.is_set():
            first = await self._queue.get()
            # Coalesce whatever else is waiting into this wake.
            items = [first]
            while not self._queue.empty():
                items.append(self._queue.get_nowait())
            try:
                await self._wake(items)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("wake failed")

    async def close(self) -> None:
        self._closing.set()
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()

    # -- the session -----------------------------------------------------------

    async def _connect(self) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        server = self._tools()
        opts: dict[str, Any] = dict(
            system_prompt=SYSTEM,
            mcp_servers={"heard": server},
            allowed_tools=["mcp__heard__say", "mcp__heard__investigate", "mcp__heard__recall", "mcp__heard__note"],
            tools=[],  # no built-ins: it cannot read files, run commands or search on its own
            permission_mode="bypassPermissions",
            setting_sources=[],
            cwd=str(self.store.data_dir),
            max_turns=8,
            effort=self.config.frontdesk_effort or None,
        )
        if self.config.frontdesk_model:
            opts["model"] = self.config.frontdesk_model
        self._client = ClaudeSDKClient(options=ClaudeAgentOptions(**opts))
        await self._client.connect()
        log.info("front desk connected (Claude Agent SDK)")

    async def _wake(self, items: list[dict[str, Any]]) -> None:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

        self._busy = True
        self.wakes += 1
        self.last_wake_at = now()
        started = time.perf_counter()
        prompt = self._compose(items)
        why = ", ".join(_why(i) for i in items)
        self.store.log_event("wake", why)
        calls: list[str] = []
        final = ""
        try:
            await self._client.query(prompt)
            async for message in self._client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            calls.append(block.name.replace("mcp__heard__", ""))
                        elif isinstance(block, TextBlock):
                            final = block.text.strip()
                elif isinstance(message, ResultMessage):
                    if getattr(message, "subtype", "") not in ("success", ""):
                        log.warning("front desk turn ended: %s", message.subtype)
        finally:
            self._busy = False
        ms = (time.perf_counter() - started) * 1000
        outcome = ", ".join(calls) if calls else (final[:40] or "silent")
        self.store.log_event("desk", f"{outcome} · {ms:.0f} ms")
        log.info("wake [%s] → %s · %.0f ms", why, outcome, ms)

    def _compose(self, items: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        parts.append(f"[wake at {time.strftime('%H:%M:%S')} · meeting minute {(now() - self.store.started_at) / 60:.0f}]")
        for it in items:
            if it["kind"] == "signal":
                s = it["signal"]
                parts.append("SIGNAL from the classifier:\n" + to_json(s))
                if s.get("card_id"):
                    parts.append(f"(card_id for the subject: {s['card_id']})")
            else:
                t = self.store.tasks.get(it["task_id"])
                if t is not None:
                    parts.append(
                        f"SUB-AGENT RETURNED · task_id={t.id} · card={t.card_id} · brief: {t.brief}\n"
                        f"SUMMARY: {t.summary}\n"
                        f"sources: {', '.join(t.sources[:4])}"
                    )
        parts.append("LAST 60s OF TRANSCRIPT:\n" + (self.store.render(self.store.tail(60.0)) or "(silence)"))
        cards = [
            f"- {c.id}: {c.title} [{c.status}]" + (f" — {c.summary}" if c.summary else "")
            for c in self.store.cards.values()
        ]
        parts.append("CARDS:\n" + ("\n".join(cards) if cards else "(none)"))
        running = [f"- {t.id} on {t.card_id}: {t.brief} ({t.status_line})" for t in self.store.running_tasks()]
        if running:
            parts.append("RUNNING TASKS:\n" + "\n".join(running))
        if self.store.asked and not self.store.asked.replied and now() - self.store.asked.at < self.config.asked_window_s:
            a = self.store.asked
            parts.append(f"OPEN DIRECT ADDRESS ({a.intent}): {a.text}")
        rej = [r for r in self.harness.rejections if now() - r["at"] < 300][-3:]
        if rej:
            parts.append("YOUR RECENT REJECTED LINES:\n" + "\n".join(f"- {r['text']} — {r['why']}" for r in rej))
        parts.append("NOTES (live):\n" + (self.store.notes or "(no notes yet)"))
        return "\n\n".join(parts)

    # -- the tools -------------------------------------------------------------

    def _tools(self) -> Any:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        store, harness, researcher = self.store, self.harness, self.researcher

        @tool(
            "say",
            "Speak one line to the room through text-to-speech. reason must be 'asked' (someone addressed Heard "
            "by name in the last few seconds) or 'finding' (refs names a returned task_id whose result the room "
            "needs before it decides). Two sentences max. Refused if the reason does not hold; the refusal says why.",
            {"type": "object",
             "properties": {"text": {"type": "string"},
                            "reason": {"type": "string", "enum": ["asked", "finding"]},
                            "refs": {"type": "array", "items": {"type": "string"}}},
             "required": ["text", "reason"]},
        )
        async def say(args: dict[str, Any]) -> dict[str, Any]:
            v = await harness.say(str(args.get("text") or ""), str(args.get("reason") or ""),
                                  [str(r) for r in (args.get("refs") or [])])
            if v.accepted:
                return {"content": [{"type": "text", "text": f"accepted ({v.why}); will speak at the next gap: {v.text}"}]}
            return {"content": [{"type": "text", "text": f"REFUSED: {v.why}"}], "is_error": True}

        @tool(
            "investigate",
            "Dispatch a research sub-agent (web search) on a card. card is the card id or title; brief is what to "
            "find out. Returns immediately with a task_id; you are woken again when it returns. At most three run at once.",
            {"card": str, "brief": str},
        )
        async def investigate(args: dict[str, Any]) -> dict[str, Any]:
            card = store.find_card(str(args.get("card") or ""))
            if card is None:
                return {"content": [{"type": "text", "text": f"no such card: {args.get('card')!r}. Cards: "
                                     + ", ".join(f"{c.id} ({c.title})" for c in store.cards.values())}],
                        "is_error": True}
            unprompted = not (store.asked and not store.asked.replied and now() - store.asked.at < 30)
            t = researcher.dispatch(card.id, str(args.get("brief") or "")[:400], unprompted=unprompted)
            if t is None:
                return {"content": [{"type": "text", "text": "pool full: three investigations already running"}],
                        "is_error": True}
            if store.asked and store.asked.intent == "lookup" and not store.asked.replied:
                store.asked.replied = True  # the rail is the answer
            return {"content": [{"type": "text", "text": f"dispatched task_id={t.id} on card {card.id}"}]}

        @tool(
            "recall",
            "Read the full transcript so far, or only the lines matching query (case-insensitive substring). "
            "Use query 'Heard! card' to get the seeded card describing how Heard! itself is built.",
            {"query": str},
        )
        async def recall(args: dict[str, Any]) -> dict[str, Any]:
            q = str(args.get("query") or "").strip().lower()
            if q in ("heard! card", "heard card", "heard"):
                c = store.find_card("Heard!")
                text = (c.summary + "\n\n" + "\n".join(c.notes)) if c else "no seeded card"
                return {"content": [{"type": "text", "text": text}]}
            lines = store.transcript if not q else [u for u in store.transcript if q in u.text.lower()]
            text = store.render(lines[-200:]) or "(nothing)"
            return {"content": [{"type": "text", "text": text}]}

        @tool("note", "Pin a short line on a card (a fact, a conflict you adjudicated). Not spoken.",
              {"card": str, "text": str})
        async def note(args: dict[str, Any]) -> dict[str, Any]:
            card = store.find_card(str(args.get("card") or ""))
            if card is None:
                return {"content": [{"type": "text", "text": "no such card"}], "is_error": True}
            card.notes.append(str(args.get("text") or "")[:300])
            card.updated_at = now()
            store.touch()
            return {"content": [{"type": "text", "text": "noted"}]}

        return create_sdk_mcp_server(name="heard", version="1.0.0", tools=[say, investigate, recall, note])


def _why(item: dict[str, Any]) -> str:
    if item["kind"] == "finding":
        return f"finding {item['task_id']}"
    s = item["signal"]
    if s.get("addressed"):
        return f"addressed:{s.get('intent')}"
    if s.get("new_subject"):
        return f"subject:{s['new_subject']['title']}"
    if any(q.get("worth_investigating") for q in s.get("questions") or []):
        return "question"
    return f"salience {s.get('salience')}"
