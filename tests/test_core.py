"""The deterministic parts: the Scribe/store, the harness, classifier normalisation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from heard.classifier import _normalise
from heard.config import Config
from heard.harness import Harness, _tidy
from heard.store import Store, Utterance


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path)


# -- scribe / store -----------------------------------------------------------

def test_commit_is_verbatim_and_anchored(store: Store) -> None:
    u1 = store.commit("Does anyone have ideas?", "J")
    u2 = store.commit("  I've got one.  ", "S")
    assert (u1.id, u2.id) == ("u0001", "u0002")
    assert u2.text == "I've got one."
    md = store.transcript_path.read_text()
    assert "**J**  `u0001`  Does anyone have ideas?" in md
    assert "**S**  `u0002`  I've got one." in md


def test_since_and_tail(store: Store) -> None:
    a = store.commit("one", "J")
    store.commit("two", "J")
    store.commit("three", "S")
    assert [u.text for u in store.since(a.id)] == ["two", "three"]
    assert [u.text for u in store.since(None)] == ["one", "two", "three"]
    assert len(store.tail(60)) == 3


def test_cards_dedupe_and_find(store: Store) -> None:
    c = store.create_card("YesChef!", anchor="u0001")
    assert c.id == "yeschef" and c.status == "PLACEHOLDER"
    c2 = store.create_card("YesChef!")
    assert c2.id == "yeschef-2"
    assert store.find_card("yeschef").id == "yeschef"
    assert store.find_card("YESCHEF!").id == "yeschef"
    assert store.find_card("nope") is None


def test_task_lifecycle(store: Store) -> None:
    c = store.create_card("Vision Assistant", named_by="heard")
    t = store.create_task(c.id, "has it been done", unprompted=True)
    assert store.cards[c.id].status == "INVESTIGATING"
    store.finish_task(t.id, summary="six on devpost", sources=["https://devpost.com/x"])
    assert store.tasks[t.id].status == "DONE"
    assert store.cards[c.id].status == "READY"
    snap = store.snapshot()
    assert snap["cards"][0]["id"] == c.id and snap["tasks"][0]["status"] == "DONE"


# -- harness -------------------------------------------------------------------

class _Voice:
    def __init__(self) -> None:
        self.lines: list[str] = []

    async def speak(self, text: str, target: str = "line") -> None:
        self.lines.append(text)


def _cfg(**kw) -> Config:
    c = Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_say_without_reason_is_refused(store: Store) -> None:
    h = Harness(store, None, config=_cfg())
    v = h.check("Hello room.", "asked", [])
    assert not v.accepted and "nobody asked" in v.why
    v = h.check("Hello room.", "finding", [])
    assert not v.accepted and "task id" in v.why
    v = h.check("Hello room.", "vibes", [])
    assert not v.accepted


def test_asked_passes_once(store: Store) -> None:
    h = Harness(store, None, config=_cfg())
    u = store.commit("Heard, what do you think?", "G")
    store.mark_asked(u.id, u.text, "opinion")
    assert h.check("I think X.", "asked", []).accepted
    asyncio.run(h.say("I think X.", "asked", []))
    assert not h.check("And also Y.", "asked", []).accepted  # replied


def test_finding_needs_done_unspoken_task_and_budget(store: Store) -> None:
    h = Harness(store, None, config=_cfg(unsolicited_gap_s=90))
    c = store.create_card("Thing")
    t = store.create_task(c.id, "done before?", unprompted=True)
    assert "not DONE" in h.check("Six exist.", "finding", [t.id]).why
    store.finish_task(t.id, summary="six", sources=[])
    assert h.check("Six exist.", "finding", [t.id]).accepted
    asyncio.run(h.say("Six exist.", "finding", [t.id]))
    assert "already spoken" in h.check("Six exist.", "finding", [t.id]).why
    t2 = store.create_task(c.id, "again", unprompted=True)
    store.finish_task(t2.id, summary="x", sources=[])
    assert "budget" in h.check("More.", "finding", [t2.id]).why


def test_floor_waits_for_a_gap_then_speaks(store: Store) -> None:
    voice = _Voice()
    h = Harness(store, voice, config=_cfg(floor_gap_s=0.2, floor_timeout_s=5))

    async def run() -> None:
        u = store.commit("Heard?", "J")
        store.mark_asked(u.id, u.text, "other")
        await h.say("Yes.", "asked", [])
        store.heard_partial("still talking")
        await asyncio.sleep(0.1)
        assert voice.lines == []
        await asyncio.sleep(0.5)
        assert voice.lines == ["Yes."]
        assert store.said[-1].delivered

    asyncio.run(run())


def test_floor_drops_to_card_on_timeout(store: Store) -> None:
    voice = _Voice()
    h = Harness(store, voice, config=_cfg(floor_gap_s=1.0, floor_timeout_s=0.3))

    async def run() -> None:
        c = store.create_card("Thing")
        t = store.create_task(c.id, "q", unprompted=True)
        store.finish_task(t.id, summary="s", sources=["https://a"])
        store.heard_partial("talk")
        await h.say("Fact.", "finding", [t.id])
        for _ in range(6):
            store.heard_partial("talk talk")
            await asyncio.sleep(0.1)
        assert voice.lines == []
        assert store.cards[c.id].highlight == "Fact."
        assert store.said[-1].delivered is False

    asyncio.run(run())


def test_tidy_caps_sentences_and_words() -> None:
    assert _tidy("One. Two. Three.", 2) == "One. Two."
    long = " ".join(["word"] * 80) + "."
    assert len(_tidy(long, 2).split()) <= 46


# -- classifier normalisation ------------------------------------------------

def test_normalise_tolerates_junk() -> None:
    new = [Utterance("u0001", 0.0, "J", "hi")]
    s = _normalise({"new_subject": {"title": "  YesChef ", "named_by": "HEARD"},
                    "questions": [{"text": "done?", "worth_investigating": "yes"}, "junk"],
                    "addressed": "true", "salience": "2"}, new)
    assert s["new_subject"] == {"title": "YesChef", "named_by": "heard", "one_liner": "", "anchor": "u0001"}
    assert s["questions"] == [{"text": "done?", "subject": None, "worth_investigating": True}]
    assert s["addressed"] is True and s["salience"] == 2
    s = _normalise({"new_subject": None, "questions": None}, new)
    assert s["new_subject"] is None and s["questions"] == [] and s["addressed"] is False


def test_two_questions_answered_in_order_by_id(store: Store) -> None:
    voice = _Voice()
    h = Harness(store, voice, config=_cfg(floor_gap_s=0.05, floor_timeout_s=2))

    async def run() -> None:
        q1 = store.commit("Hey Heard, has this been done?", "J")
        a1 = store.mark_asked(q1.id, q1.text, "other")
        q2 = store.commit("Heard, who would pay for it?", "S")
        a2 = store.mark_asked(q2.id, q2.text, "other")
        assert [a.utterance_id for a in store.open_asks(60)] == [q1.id, q2.id]
        # naming the second question ticks only the second
        v = await h.say("Restaurants would.", "asked", [], answering=q2.id)
        assert v.accepted and a2.replied and not a1.replied
        # a wrong id is refused and lists what is open
        v = await h.say("Nope.", "asked", [], answering="u9999")
        assert not v.accepted and q1.id in v.why
        # no id falls back to the oldest open one
        v = await h.say("Six times on Devpost.", "asked", [])
        assert v.accepted and a1.replied
        await asyncio.sleep(0.6)
        # spoken in the order accepted: FIFO
        assert voice.lines == ["Restaurants would.", "Six times on Devpost."]

    asyncio.run(run())
