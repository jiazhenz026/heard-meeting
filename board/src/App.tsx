/**
 * The shell. Start gate, topbar, canvas of cards, task rail, transcript strip,
 * notes view, spoken banner, expanded card. Renders whatever arrives; the
 * state is REPLACED on every push, never merged.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AudioOut } from "./audio";
import { Mic } from "./mic";
import { BoardSocket } from "./socket";
import { elapsed, serverNow, stamp, useSecondTick } from "./clock";
import { EMPTY_STATE, type Card, type LinkState, type Said, type StatePayload, type Task } from "./types";

type View = "canvas" | "notes";

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
  const [typed, setTyped] = useState("");
  const [speaker, setSpeaker] = useState("J");

  const audio = useRef(new AudioOut());
  const mic = useRef<Mic | null>(null);
  const sock = useRef<BoardSocket | null>(null);
  const offsetRef = useRef(0);
  const lastSaidId = useRef<string | null>(null);
  const bannerTimer = useRef<number | null>(null);

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
        // The newest said line rises as a banner for a while.
        const last = next.said[next.said.length - 1];
        if (last && last.id !== lastSaidId.current) {
          lastSaidId.current = last.id;
          setBanner(last);
          if (bannerTimer.current) window.clearTimeout(bannerTimer.current);
          bannerTimer.current = window.setTimeout(() => setBanner(null), 14000);
        }
        if (next.expanded) setOpen(next.expanded);
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
  }, []);

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
  const cards = useMemo(() => state.cards.filter((c) => !c.seeded).concat(state.cards.filter((c) => c.seeded)), [state.cards]);
  const openCard = open ? state.cards.find((c) => c.id === open) ?? null : null;
  const minute = state.started_at ? elapsed(now - state.started_at) : "0:00";

  if (!started) {
    return (
      <div className="gate">
        <div className="gate-card">
          <div className="brand">HEARD<b>!</b></div>
          <p className="gate-tag">The meeting board that listens, goes and finds out, and speaks up.</p>
          <button className="gate-btn" onClick={begin}>Start listening</button>
          <p className="gate-note">Grants the microphone and unlocks audio. Link: {link}.</p>
        </div>
      </div>
    );
  }

  return (
    <div className={`shell ${banner && banner.reason === "finding" ? "dim" : ""}`}>
      <header className="topbar">
        <div className="brand">HEARD<b>!</b></div>
        <span className="pill">{minute}</span>
        <span className={`pill link-${link}`}>{link}</span>
        <span className="pill level" title="mic level">
          <i style={{ width: `${Math.round(micLevel * 100)}%` }} />
        </span>
        <span className="pill" title={state.health.frontdesk_error ?? ""}>
          desk {state.health.frontdesk}{state.health.frontdesk_busy ? " · thinking" : ""}
        </span>
        <span className="pill">{state.health.research_running} researching</span>
        {micNote && <span className="pill warn">{micNote}</span>}
        <span className="spacer" />
        <select className="pill" value={speaker} onChange={(e) => setSpeaker(e.target.value)}>
          {["J", "S", "G", "Q"].map((s) => <option key={s}>{s}</option>)}
        </select>
        <input
          className="inject"
          placeholder="type a line as if spoken…"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && inject()}
        />
        <button className={`pill tab ${view === "canvas" ? "on" : ""}`} onClick={() => setView("canvas")}>Board</button>
        <button className={`pill tab ${view === "notes" ? "on" : ""}`} onClick={() => setView("notes")}>Notes</button>
        <a className="pill" href="/transcript.md" target="_blank" rel="noreferrer">transcript</a>
      </header>

      <main className="body">
        <section className="canvas">
          {view === "canvas" ? (
            cards.length === 0 ? (
              <div className="empty">Listening. The first idea gets a card.</div>
            ) : (
              <div className="grid">
                {cards.map((c) => (
                  <CardView key={c.id} card={c} tasks={state.tasks.filter((t) => t.card_id === c.id)} now={now} onOpen={() => setOpen(c.id)} />
                ))}
              </div>
            )
          ) : (
            <NotesView notes={state.notes} version={state.notes_version} />
          )}
        </section>
        <aside className="rail">
          <div className="rail-h">Investigations</div>
          {state.tasks.length === 0 && <div className="rail-empty">Nothing dispatched yet.</div>}
          {state.tasks.map((t) => <TaskView key={t.id} task={t} card={state.cards.find((c) => c.id === t.card_id)} now={now} />)}
        </aside>
      </main>

      <footer className="strip">
        <Strip state={state} partial={partial} />
      </footer>

      {banner && (
        <div className={`banner ${banner.reason} ${banner.delivered ? "" : "dropped"}`}>
          <div className="banner-h">
            <span className="badge">{banner.reason}</span>
            <span className="badge dim">{banner.delivered ? (speaking ? "speaking" : "said") : "no gap · sent to card"}</span>
            <button className="x" onClick={() => setBanner(null)}>×</button>
          </div>
          <div className="banner-line">{banner.text}</div>
          {banner.evidence.length > 0 && (
            <div className="chips">{banner.evidence.map((u) => <a key={u} href={u} target="_blank" rel="noreferrer" className="chip">{host(u)}</a>)}</div>
          )}
        </div>
      )}

      {openCard && (
        <div className="modal" onClick={() => { setOpen(null); fetch("/expand/none", { method: "POST" }).catch(() => {}); }}>
          <div className="modal-body" onClick={(e) => e.stopPropagation()}>
            <div className="modal-h">
              <span className="modal-title">{openCard.title}</span>
              <span className={`status ${openCard.status}`}>{openCard.status}</span>
              <span className="spacer" />
              <button className="x" onClick={() => setOpen(null)}>×</button>
            </div>
            {openCard.page ? (
              <iframe title={openCard.title} src={`/cards/${openCard.id}`} sandbox="allow-same-origin allow-popups" />
            ) : (
              <div className="modal-empty">
                <p>{openCard.one_liner || "Named, not yet understood."}</p>
                {openCard.notes.map((n, i) => <p key={i}>· {n}</p>)}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function CardView({ card, tasks, now, onOpen }: { card: Card; tasks: Task[]; now: number; onOpen: () => void }) {
  const running = tasks.filter((t) => t.status === "RUNNING");
  const age = now - card.created_at;
  return (
    <article className={`card ${card.status} ${card.seeded ? "seeded" : ""} ${card.highlight ? "hl" : ""} ${age < 1.2 ? "landed" : ""}`} onClick={onOpen}>
      <div className="card-h">
        <span className="card-title">{card.title}</span>
        {card.named_by === "heard" && <span className="tag">named by Heard</span>}
        <span className={`status ${card.status}`}>{card.status === "PLACEHOLDER" ? "named" : card.status.toLowerCase()}</span>
      </div>
      {card.one_liner && <div className="card-sub">{card.one_liner}</div>}
      {card.status === "PLACEHOLDER" && !card.summary && <div className="card-ghost">named, not yet understood</div>}
      {card.summary && <div className="card-body">{card.summary}</div>}
      {card.notes.length > 0 && !card.seeded && (
        <ul className="card-notes">{card.notes.slice(-3).map((n, i) => <li key={i}>{n}</li>)}</ul>
      )}
      {card.highlight && <div className="card-hl">{card.highlight}</div>}
      <div className="card-f">
        {running.map((t) => <span key={t.id} className="mini run">● {t.status_line} · {elapsed(now - t.started_at)}</span>)}
        {tasks.filter((t) => t.status === "DONE").length > 0 && <span className="mini">{tasks.filter((t) => t.status === "DONE").length} finding{tasks.filter((t) => t.status === "DONE").length > 1 ? "s" : ""}</span>}
        {card.page && <span className="mini link">open page →</span>}
      </div>
    </article>
  );
}

function TaskView({ task, card, now }: { task: Task; card: Card | undefined; now: number }) {
  const secs = (task.finished_at ?? now) - task.started_at;
  return (
    <div className={`task ${task.status}`}>
      <div className="task-h">
        <span className="task-card">{card?.title ?? task.card_id}</span>
        {task.unprompted && <span className="tag">unprompted</span>}
        <span className={`num task-t ${task.status === "RUNNING" ? "run" : ""}`}>{elapsed(secs)}</span>
      </div>
      <div className="task-brief">{task.brief}</div>
      <div className="task-s">
        {task.status === "RUNNING" && <i className="dot" />}
        {task.status === "RUNNING" ? task.status_line : task.status === "DONE" ? task.summary : "failed"}
      </div>
      {task.status === "RUNNING" && <div className="track"><i /></div>}
      {task.sources.length > 0 && <div className="chips">{task.sources.slice(0, 4).map((u) => <a key={u} href={u} target="_blank" rel="noreferrer" className="chip">{host(u)}</a>)}</div>}
    </div>
  );
}

function Strip({ state, partial }: { state: StatePayload; partial: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const lines = useMemo(() => {
    const out: { key: string; at: number; kind: string; who: string; text: string }[] = [];
    for (const u of state.transcript) out.push({ key: u.id, at: u.at, kind: "heard", who: u.speaker, text: u.text });
    for (const e of state.log) {
      if (e.kind === "card" || e.kind === "task" || e.kind === "said" || e.kind === "dropped" || e.kind === "rejected" || e.kind === "wake" || e.kind === "desk")
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
        <div key={l.key} className={`line ${l.kind}`}>
          <span className="num t">{stamp(l.at)}</span>
          <span className="k">{label(l.kind)}</span>
          <span className="who">{l.who}</span>
          <span className="txt">{l.text}</span>
        </div>
      ))}
      {partial && (
        <div className="line heard interim">
          <span className="num t">--:--:--</span>
          <span className="k">heard</span>
          <span className="who" />
          <span className="txt">{partial}…</span>
        </div>
      )}
    </div>
  );
}

function NotesView({ notes, version }: { notes: string; version: number }) {
  return (
    <div className="notes">
      <div className="notes-h">Live meeting notes · v{version} · written by the notes agent every ~20 s</div>
      {notes ? <pre className="notes-md">{notes}</pre> : <div className="empty">No notes yet. They arrive after the first stretch of talk.</div>}
    </div>
  );
}

function label(kind: string): string {
  switch (kind) {
    case "heard": return "heard";
    case "card": return "card";
    case "task": return "research";
    case "said": return "said";
    case "dropped": return "dropped";
    case "rejected": return "refused";
    case "wake": return "wake";
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
