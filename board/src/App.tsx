/**
 * The shell. Start gate, top bar, canvas (cards / notes / transcript), the
 * investigations rail, the strip of what it heard and did, the spoken banner,
 * and a card's page opened in place. Renders whatever arrives; the state is
 * REPLACED on every push, never merged.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { AudioOut } from "./audio";
import { Mic } from "./mic";
import { BoardSocket } from "./socket";
import { elapsed, serverNow, stamp, useSecondTick } from "./clock";
import { EMPTY_STATE, type Card, type LinkState, type Said, type StatePayload, type Task } from "./types";

type View = "canvas" | "notes" | "transcript";

export default function App() {
  const [started, setStarted] = useState(false);
  const [state, setState] = useState<StatePayload>(EMPTY_STATE);
  const [offset, setOffset] = useState(0);
  const [link, setLink] = useState<LinkState>("idle");
  const [partial, setPartial] = useState("");
  const [micLevel, setMicLevel] = useState(0);
  const [micNote, setMicNote] = useState<string | null>(null);
  const [speaking, setSpeaking] = useState(false);
  const [banner, setBanner] = useState<Said | null>(null);
  const [view, setView] = useState<View>("canvas");
  const [open, setOpen] = useState<string | null>(null);
  const [closing, setClosing] = useState(false);
  const [settled, setSettled] = useState(false);
  const [typed, setTyped] = useState("");
  const [speaker, setSpeaker] = useState("J");
  const [thinking, setThinking] = useState<string>(() => {
    try { return localStorage.getItem("heard.thinking") || "light"; } catch { return "light"; }
  });
  const pickThinking = (level: string) => {
    setThinking(level);
    try { localStorage.setItem("heard.thinking", level); } catch { /* fine */ }
  };
  const [ripping, setRipping] = useState<Set<string>>(new Set());
  const removeCard = useCallback((id: string) => {
    if (openRef.current === id) {
      openRef.current = null;
      setOpen(null);
    }
    // Tear the note in half first; the delete goes out when the halves have fallen.
    setRipping((r) => new Set(r).add(id));
    window.setTimeout(() => {
      fetch(`/cards/${id}`, { method: "DELETE" }).catch(() => {});
      window.setTimeout(() => setRipping((r) => { const n = new Set(r); n.delete(id); return n; }), 1500);
    }, 720);
  }, []);

  const audio = useRef(new AudioOut());
  const mic = useRef<Mic | null>(null);
  const sock = useRef<BoardSocket | null>(null);
  const offsetRef = useRef(0);
  const lastSaidId = useRef<string | null>(null);
  const bannerTimer = useRef<number | null>(null);
  const canvasRef = useRef<HTMLElement | null>(null);
  const cardEls = useRef<Map<string, HTMLElement>>(new Map());
  const expandedEl = useRef<HTMLDivElement | null>(null);
  const fromRect = useRef<DOMRect | null>(null);
  const openRef = useRef<string | null>(null);

  /** Open a card in place of the grid, growing out of its own rectangle. */
  const openCard = useCallback((id: string) => {
    if (openRef.current === id) return;
    const el = cardEls.current.get(id);
    fromRect.current = el ? el.getBoundingClientRect() : null;
    openRef.current = id;
    setSettled(false);
    setClosing(false);
    setOpen(id);
  }, []);

  const closeCard = useCallback(() => {
    if (!openRef.current) return;
    fetch("/expand/none", { method: "POST" }).catch(() => {});
    const node = expandedEl.current;
    const from = fromRect.current;
    if (!node || !from) {
      openRef.current = null;
      setOpen(null);
      return;
    }
    setSettled(false);
    setClosing(true);
    const to = node.getBoundingClientRect();
    node.style.transition = "transform .36s cubic-bezier(.4,0,.2,1), opacity .3s";
    node.style.transformOrigin = "top left";
    node.style.transform = `translate(${from.left - to.left}px, ${from.top - to.top}px) scale(${from.width / to.width}, ${from.height / to.height})`;
    node.style.opacity = "0.4";
    window.setTimeout(() => {
      openRef.current = null;
      setClosing(false);
      setOpen(null);
    }, 370);
  }, []);

  // The grow: start at the card's rectangle, end filling the canvas.
  useLayoutEffect(() => {
    const node = expandedEl.current;
    if (!open || closing || !node) return;
    const from = fromRect.current;
    const to = node.getBoundingClientRect();
    if (!from) {
      setSettled(true);
      return;
    }
    node.style.transition = "none";
    node.style.transformOrigin = "top left";
    node.style.transform = `translate(${from.left - to.left}px, ${from.top - to.top}px) scale(${from.width / to.width}, ${from.height / to.height})`;
    node.style.opacity = "0.7";
    node.getBoundingClientRect();
    node.style.transition = "transform .45s cubic-bezier(.2,.8,.2,1), opacity .35s";
    node.style.transform = "none";
    node.style.opacity = "1";
    const id = window.setTimeout(() => {
      node.style.transition = "";
      setSettled(true);
    }, 470);
    return () => window.clearTimeout(id);
  }, [open, closing]);

  useSecondTick();

  useEffect(() => {
    const s = new BoardSocket({
      onLink: setLink,
      onClock: (o) => {
        offsetRef.current = o;
        setOffset(o);
      },
      onState: (next) => {
        setState(next);
        setPartial(next.partial || "");
        const last = next.said[next.said.length - 1];
        if (last && last.id !== lastSaidId.current) {
          lastSaidId.current = last.id;
          setBanner(last);
          if (bannerTimer.current) window.clearTimeout(bannerTimer.current);
          bannerTimer.current = window.setTimeout(() => setBanner(null), 14000);
        }
        if (next.expanded && next.expanded !== openRef.current) openCard(next.expanded);
      },
      onPartial: (text) => setPartial(text),
      onAudio: (msg) => {
        audio.current.push({ text: msg.text, target: msg.target, audio_b64: msg.audio_b64 });
      },
    });
    sock.current = s;
    s.start();
    const off = audio.current.onSpeaking(setSpeaking);
    return () => {
      off();
      s.stop();
    };
  }, [openCard]);

  const begin = useCallback(async () => {
    await audio.current.unlock();
    const m = new Mic({
      onChunk: (pcm_b64) => sock.current?.send({ type: "audio_chunk", pcm_b64 }),
      onTranscript: (text, final) => sock.current?.send({ type: "transcript", text, final }),
      onLevel: setMicLevel,
      onError: (what) => setMicNote(what),
    });
    mic.current = m;
    await m.start();
    setStarted(true);
  }, []);

  const inject = useCallback(() => {
    const text = typed.trim();
    if (!text) return;
    sock.current?.send({ type: "inject", text, speaker });
    setTyped("");
  }, [typed, speaker]);

  const now = serverNow(offset);
  const cards = useMemo(
    () => state.cards.filter((c) => !c.seeded).concat(state.cards.filter((c) => c.seeded)),
    [state.cards],
  );
  const shown = open ? state.cards.find((c) => c.id === open) ?? null : null;
  const minute = state.started_at ? elapsed(now - state.started_at) : "0:00";
  const status = statusLine(link, state, micNote);

  if (!started) {
    return (
      <div className="gate">
        <div className="gate-inner">
          <div className="wordmark">Heard<span>!</span></div>
          <p className="gate-lede">The meeting board that listens, goes and finds out, and speaks up.</p>
          <button className="gate-btn" onClick={begin}>Start listening</button>
          <p className="gate-note">This turns the microphone on and lets the board play sound.</p>
        </div>
      </div>
    );
  }

  return (
    <div className={`shell ${banner && banner.reason === "finding" ? "dim" : ""}`}>
      <header className="top">
        <div className="wordmark">Heard<span>!</span></div>
        <span className="clock">{minute}</span>
        <nav className="views">
          <button className={view === "canvas" ? "on" : ""} onClick={() => setView("canvas")}>Board</button>
          <button className={view === "notes" ? "on" : ""} onClick={() => setView("notes")}>Notes</button>
          <button className={view === "transcript" ? "on" : ""} onClick={() => setView("transcript")}>Transcript</button>
        </nav>
        <div className="levels" role="radiogroup" aria-label="thinking">
          <span className="levels-label">Thinking</span>
          {[["light", "Extra light"], ["normal", "Normal"], ["hard", "Hard"]].map(([v, name]) => (
            <button key={v} role="radio" aria-checked={thinking === v} className={thinking === v ? "on" : ""} onClick={() => pickThinking(v)}>{name}</button>
          ))}
        </div>
        <span className="grow" />
        <label className="typein">
          <select value={speaker} onChange={(e) => setSpeaker(e.target.value)} aria-label="speaker">
            {["J", "S", "G", "Q"].map((s) => <option key={s}>{s}</option>)}
          </select>
          <input
            placeholder="Type a line as if someone said it"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && inject()}
          />
        </label>
        <span className={`status ${status.tone}`} title={state.health.frontdesk_error ?? ""}>
          <i className="ear" style={{ opacity: 0.35 + micLevel * 0.65 }} />
          {status.text}
        </span>
      </header>

      <main className="body">
        <section className="canvas" ref={canvasRef}>
          {shown && (
            <div className={`expanded ${settled ? "settled" : ""}`} ref={expandedEl}>
              <div className="expanded-top">
                <span className="expanded-title">{shown.title}</span>
                <span className="expanded-meta">{cardState(shown)}</span>
                <span className="grow" />
                <button className="back danger" onClick={() => removeCard(shown.id)}>Delete this note</button>
                <button className="back" onClick={closeCard}>Back to the board</button>
              </div>
              {shown.page ? (
                <iframe title={shown.title} src={`/cards/${shown.id}`} sandbox="allow-same-origin allow-popups" />
              ) : (
                <div className="expanded-empty">
                  <p>{shown.one_liner || "Named, not yet understood."}</p>
                  {shown.notes.map((n, i) => <p key={i}>{n}</p>)}
                  <p className="muted">The page appears when the investigation comes back.</p>
                </div>
              )}
            </div>
          )}
          {view === "canvas" ? (
            cards.length === 0 ? (
              <p className="empty">Listening. The first idea someone pitches gets a card here.</p>
            ) : (
              <div className={`grid n${Math.min(cards.length, 10)}`}>
                {cards.map((c) => (
                  <CardView
                    key={c.id}
                    card={c}
                    tasks={state.tasks.filter((t) => t.card_id === c.id)}
                    now={now}
                    focused={state.focus === c.id && now - state.focus_at < 75}
                    ripping={ripping.has(c.id)}
                    onOpen={() => openCard(c.id)}
                    onRemove={() => removeCard(c.id)}
                    register={(el) => { if (el) cardEls.current.set(c.id, el); else cardEls.current.delete(c.id); }}
                  />
                ))}
              </div>
            )
          ) : view === "notes" ? (
            <NotesView notes={state.notes} version={state.notes_version} />
          ) : (
            <TranscriptView state={state} partial={partial} />
          )}
        </section>
        <aside className="rail">
          <h2>Currently working on</h2>
          {state.working.length === 0 ? (
            <p className="rail-empty">Nothing right now. Listening.</p>
          ) : (
            <ul className="working-list">
              {state.working.map((w) => (
                <li key={w.key + w.at} className={w.done ? "done" : ""}>
                  <i className="tick" aria-hidden="true">{w.done ? "✓" : ""}</i>
                  <span>{w.text}</span>
                </li>
              ))}
            </ul>
          )}
          <h2 className="rail-h2">Investigations</h2>
          {state.tasks.length === 0 && <p className="rail-empty">Nothing sent out yet.</p>}
          {state.tasks.map((t) => <TaskView key={t.id} task={t} card={state.cards.find((c) => c.id === t.card_id)} now={now} />)}
        </aside>
      </main>

      <footer className="strip">
        <Strip state={state} partial={partial} />
      </footer>

      {banner && (
        <div className={`banner ${banner.reason} ${banner.delivered ? "" : "dropped"}`} role="status">
          <div className="banner-top">
            <span className="banner-kind">{bannerKind(banner, speaking)}</span>
            <button className="close" onClick={() => setBanner(null)} aria-label="dismiss">×</button>
          </div>
          <p className="banner-text">{banner.text}</p>
          {banner.evidence.length > 0 && (
            <div className="sources">
              {banner.evidence.map((u) => <a key={u} href={u} target="_blank" rel="noreferrer">{host(u)}</a>)}
            </div>
          )}
        </div>
      )}

    </div>
  );
}

function CardView({ card, tasks, now, onOpen, onRemove, register, focused, ripping }: { card: Card; tasks: Task[]; now: number; onOpen: () => void; onRemove: () => void; register: (el: HTMLElement | null) => void; focused: boolean; ripping: boolean }) {
  const running = tasks.some((t) => t.status === "RUNNING");
  const age = now - card.created_at;
  const tagline = card.seeded ? card.one_liner : shorten(card.summary) || card.one_liner || "";
  return (
    <article
      ref={register}
      className={`card tone-${tone(card)} tilt-${tilt(card)} ${card.status.toLowerCase()} ${card.seeded ? "seeded" : ""} ${card.highlight ? "spoken" : ""} ${focused ? "focus" : ""} ${age < 1.2 ? "landed" : ""} ${ripping ? "ripping" : ""}`}
      onClick={onOpen}
      title={card.named_by === "heard" ? "Heard named this one" : undefined}
    >
      {!ripping && <button className="remove" aria-label={`Delete ${card.title}`} title="Delete this note" onClick={(e) => { e.stopPropagation(); onRemove(); }}>×</button>}
      <div className="face">
        <h3>{card.title}</h3>
        {tagline ? <p>{tagline}</p> : <p className="ghost">Named, not yet understood.</p>}
      </div>
      {ripping && (
        <>
          <div className="half left"><div className="face"><h3>{card.title}</h3><p>{tagline}</p></div></div>
          <div className="half right"><div className="face"><h3>{card.title}</h3><p>{tagline}</p></div></div>
        </>
      )}
      {running && !ripping && <span className="working" aria-label="investigating" />}
    </article>
  );
}

function TaskView({ task, card, now }: { task: Task; card: Card | undefined; now: number }) {
  const secs = (task.finished_at ?? now) - task.started_at;
  return (
    <div className={`task ${task.status.toLowerCase()}`}>
      <div className="task-top">
        <span className="task-card">{card?.title ?? task.card_id}</span>
        <span className="task-time">{elapsed(secs)}</span>
      </div>
      <p className="task-brief">{task.brief}</p>
      {task.status === "RUNNING" && <div className="bar"><i /></div>}
    </div>
  );
}

function Strip({ state, partial }: { state: StatePayload; partial: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const lines = useMemo(() => {
    const out: { key: string; at: number; kind: string; who: string; text: string }[] = [];
    for (const u of state.transcript) out.push({ key: u.id, at: u.at, kind: "heard", who: who(u.speaker), text: u.text });
    for (const e of state.log) {
      if (["card", "task", "said", "dropped", "rejected", "wake", "desk"].includes(e.kind))
        out.push({ key: `${e.kind}-${e.at}`, at: e.at, kind: e.kind, who: "", text: e.text });
    }
    out.sort((a, b) => a.at - b.at);
    return out.slice(-80);
  }, [state.transcript, state.log]);
  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight });
  }, [lines.length, partial]);
  return (
    <div className="feed" ref={ref}>
      {lines.map((l) => (
        <div key={l.key} className={`line k-${l.kind}`}>
          <span className="t">{stamp(l.at)}</span>
          <span className="k">{label(l.kind)}</span>
          <span className="who">{l.who}</span>
          <span className="txt">{l.text}</span>
        </div>
      ))}
      {partial && (
        <div className="line k-heard interim">
          <span className="t" />
          <span className="k">hearing</span>
          <span className="who" />
          <span className="txt">{partial}…</span>
        </div>
      )}
    </div>
  );
}

function TranscriptView({ state, partial }: { state: StatePayload; partial: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight });
  }, [state.transcript.length, partial]);
  return (
    <div className="doc transcript" ref={ref}>
      <h2>Transcript</h2>
      <p className="doc-lede">Every line as it was said, written down by the scribe. {state.transcript.length} lines so far.</p>
      {state.transcript.length === 0 && <p className="empty">Nothing heard yet.</p>}
      {state.transcript.map((u) => (
        <div key={u.id} className="tline">
          <span className="t">{stamp(u.at)}</span>
          <span className="who">{who(u.speaker)}</span>
          <span className="txt">{u.text}</span>
        </div>
      ))}
      {partial && (
        <div className="tline interim">
          <span className="t" />
          <span className="who" />
          <span className="txt">{partial}…</span>
        </div>
      )}
    </div>
  );
}

function NotesView({ notes, version }: { notes: string; version: number }) {
  return (
    <div className="doc">
      <h2>Notes</h2>
      <p className="doc-lede">Live meeting notes, rewritten by the notes agent every twenty seconds or so. Version {version}.</p>
      {notes ? <pre className="notes-md">{notes}</pre> : <p className="empty">No notes yet. They arrive after the first stretch of talk.</p>}
    </div>
  );
}

function statusLine(link: LinkState, state: StatePayload, micNote: string | null): { text: string; tone: string } {
  if (link !== "open") return { text: "Reconnecting", tone: "warn" };
  if (micNote && micNote.includes("denied")) return { text: "Microphone off, typing only", tone: "warn" };
  const h = state.health;
  if (h.frontdesk !== "up") return { text: `Front desk ${h.frontdesk}`, tone: "warn" };
  const parts = ["Listening"];
  if (h.frontdesk_busy) parts.push("thinking");
  if (h.research_running > 0) parts.push(`${h.research_running} out looking`);
  return { text: parts.join(", "), tone: "" };
}

function bannerKind(b: Said, speaking: boolean): string {
  if (!b.delivered) return "No gap to speak, so it went on the card";
  if (b.reason === "asked") return speaking ? "Answering" : "Answered";
  return speaking ? "Speaking up" : "Spoke up";
}

function cardState(c: Card): string {
  if (c.seeded) return "How it is built";
  if (c.status === "PLACEHOLDER") return "Named, not yet understood";
  if (c.status === "INVESTIGATING") return "Being looked into";
  return "Looked into";
}

/** A pastel paper tone per card, fixed by its id so it never changes. */
function tone(card: Card): number {
  if (card.seeded) return 0;
  let h = 0;
  for (const ch of card.id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return 1 + (h % 11);
}

function tilt(card: Card): number {
  let h = 7;
  for (const ch of card.id) h = (h * 17 + ch.charCodeAt(0)) >>> 0;
  return h % 3;
}

/** Unknown speakers show as nothing, never as a question mark. */
function who(speaker: string): string {
  return speaker && speaker !== "?" ? speaker : "";
}

/** First sentence or two, capped so it fits three lines on a card. */
function shorten(text: string): string {
  const t = (text || "").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/(\*\*|__|`|\*)/g, "").trim();
  if (!t) return "";
  const m = t.match(/^(.{20,320}?[.!?])(\s|$)/);
  const s = m ? m[1] : t;
  return s.length > 320 ? s.slice(0, 317).replace(/\s+\S*$/, "") + "…" : s;
}

function label(kind: string): string {
  switch (kind) {
    case "heard": return "heard";
    case "card": return "card";
    case "task": return "sent out";
    case "said": return "said";
    case "dropped": return "kept";
    case "rejected": return "held";
    case "wake": return "woke";
    case "desk": return "desk";
    default: return kind;
  }
}

function host(u: string): string {
  try {
    return new URL(u).host.replace(/^www\./, "");
  } catch {
    return u.slice(0, 30);
  }
}
