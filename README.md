# Heard! — the meeting board that listens, searches, and joins your meeting

Heard! is an ambient agent that sits in a working meeting. It keeps live
meeting notes, gives every project or idea you discuss a card on a dashboard,
sends sub-agents to investigate the open questions, and answers when asked.
Otherwise it keeps quiet.

Built at **SteelHacks XIII** (24-hour hackathon, Sept 19 2026).
Tracks: **Out Loud!** (ElevenLabs) · **Beyond the Chatbot** (NVIDIA) · Seed Round.

```
J   "I've been working on a product called YesChef — it listens to a kitchen
     during service and turns call-outs into a live board."
                          →  CARD  YesChef · named            (< 5 s)
                          →  RESEARCH  has this been done? who is it for?   (unprompted)
HEARD!                       (nothing)

G   "Let's see what Heard thinks."
HEARD!  "Voice KDS exists — SoundHound and Veovox both ship one…"     [asked]

S   "Okay, agreed. Let's go with the vision assistant then."
HEARD!  "Heads up — Devpost has at least six hackathon projects
         doing exactly this…"                                        [finding]
```

## Architecture

Five components. Two have no model in them.

| Component | Model | Runs | Writes |
|---|---|---|---|
| **Scribe** | none | every STT commit | `data/transcript.md` — verbatim, anchored, append-only |
| **Classifier** | Nemotron via NVIDIA NIM | every 2–5 s of new speech | placeholder cards (< 5 s) + a signal |
| **Notes agent** | Nemotron via NVIDIA NIM | every ~20 s | `data/notes.md` — the shared context window |
| **Front desk** | Claude Agent SDK, one resident session | woken by a signal or a returning sub-agent | tool calls only: `recall · investigate · say · note` |
| **Sub-agents** | Claude Agent SDK + WebSearch | on `investigate`, max 3 | an HTML page per card + a summary |

Between `say` and the speakers sits the **harness**, plain code: `say` must
carry a reason the runtime can check (`asked` — someone addressed Heard in the
last 20 s; `finding` — a named task returned and has not been spoken), one
unsolicited line per 90 s, two sentences max, and the floor waits for a gap
in the room. Every layer fails towards silence.

```
STT (ElevenLabs Scribe v2)
  → SCRIBE → transcript.md
      → CLASSIFIER → card + signal ─┐
      → NOTES AGENT → notes.md      │
                                    ▼
                               FRONT DESK ── investigate ──► SUB-AGENT ──► /cards/{id}.html
                                    │ say
                                 HARNESS → floor → TTS (ElevenLabs) → board
```

## Run it

```bash
cp .env.example .env        # NVIDIA_API_KEY, ELEVENLABS_API_KEY (+ voice id)
uv venv && uv pip install -e ".[dev]"
cd board && npm install && npm run build && cd ..
.venv/bin/python main.py    # http://127.0.0.1:8000
```

The front desk and the researchers run on the Claude Agent SDK, which drives
the bundled `claude` CLI. Either run `claude login` once on the machine, or set
`ANTHROPIC_API_KEY`.

Click **Start listening** on the board: that one gesture grants the microphone
and unlocks audio. With no ElevenLabs key the board falls back to the browser's
own speech recognition and nothing is spoken.

### Without a microphone

Everything can be driven by text, which is how it is tested:

```bash
.venv/bin/python scripts/replay.py fixtures/demo.txt        # the demo meeting, timed
curl -X POST localhost:8000/inject -H 'content-type: application/json' \
     -d '{"text":"Heard, has anyone built this before?","speaker":"J"}'
```

There is also a type-a-line box in the board's top bar.

### Endpoints

| Route | What |
|---|---|
| `WS /ws` | full state on connect and after every change; `partial`, `audio` |
| `GET /cards/{id}` | the sub-agent's HTML page for a card |
| `GET /transcript.md` · `GET /notes.md` | the two files, raw |
| `POST /inject` | a line as if spoken |
| `POST /reset` | back to an empty meeting |
| `GET /healthz` | which providers are live, wake and pass counts |

### Configuration

Everything is in `.env.example`. The ones that matter on stage:

- `NEMOTRON_MODEL_ID` — the classifier is on the 5-second path; use a small one (`nvidia/nemotron-3-super-120b-a12b`).
- `HEARD_STT_KEYTERMS` — names Scribe is biased towards (`Heard`, the product names). Add the ideas you plan to pitch.
- `HEARD_UNSOLICITED_GAP_S` — how often it may speak without being asked.
- `HEARD_RESEARCH_EFFORT` / `HEARD_RESEARCH_MODEL` — research speed vs depth.
- `--no-audio` — log spoken lines instead of synthesising them.

## Layout

```
heard/
  store.py        the store and the Scribe (commit = one anchored line)
  classifier.py   Nemotron, JSON, creates placeholder cards directly
  notes.py        Nemotron, rewrites notes.md
  frontdesk.py    Claude Agent SDK session + the four tools
  research.py     one SDK run per investigation → HTML page
  harness.py      say → reason check → budget → floor → TTS
  nim.py          NIM client + loose JSON parsing
  speech/         Scribe v2 realtime, TTS streaming, echo guard (ported from the kitchen build)
  server/         FastAPI app, WebSocket hub
board/            React + Vite: canvas, task rail, transcript strip, notes view, banner
fixtures/demo.txt the scripted demo meeting
scripts/replay.py posts a fixture to /inject with timing
```

## What carried over from the kitchen build

The ElevenLabs Scribe socket reader, TTS streaming with the echo guard,
the FastAPI/asyncio skeleton, the WebSocket hub with full-state resync, the
browser mic capture and audio unlock, and the loose-JSON NIM parsing.
Everything above the transcript is new.
