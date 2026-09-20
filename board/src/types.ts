/**
 * The board's shape. Mirrors `heard/store.py:Store.snapshot()` and the
 * envelopes in `heard/server/ws.py`. If this disagrees with the server, the
 * server is right.
 */

export type Target = "line" | "floor";
export type CardStatus = "PLACEHOLDER" | "INVESTIGATING" | "READY";
export type TaskStatus = "RUNNING" | "DONE" | "FAILED";

export interface Utterance {
  id: string;
  at: number;
  speaker: string;
  text: string;
}

export interface Card {
  id: string;
  title: string;
  named_by: "speaker" | "heard";
  status: CardStatus;
  created_at: number;
  updated_at: number;
  anchor: string | null;
  one_liner: string;
  summary: string;
  page: boolean;
  highlight: string | null;
  notes: string[];
  seeded: boolean;
}

export interface Task {
  id: string;
  card_id: string;
  brief: string;
  status: TaskStatus;
  started_at: number;
  finished_at: number | null;
  summary: string;
  sources: string[];
  status_line: string;
  spoken: boolean;
  unprompted: boolean;
}

export interface Said {
  id: string;
  at: number;
  text: string;
  reason: string;
  refs: string[];
  evidence: string[];
  delivered: boolean;
}

export interface Ask {
  at: number;
  utterance_id: string;
  text: string;
  intent: string;
  replied: boolean;
}

export interface WorkItem {
  key: string;
  text: string;
  done: boolean;
  at: number;
  done_at: number | null;
}

export interface LogEntry {
  at: number;
  kind: string;
  text: string;
}

export interface Health {
  stt: string | null;
  tts: string | null;
  frontdesk: string;
  frontdesk_busy: boolean;
  classifier_fires: number;
  classifier_error: string | null;
  notes_passes: number;
  notes_error: string | null;
  frontdesk_error: string | null;
  wakes: number;
  research_running: number;
  audio_age: number | null;
}

export interface StatePayload {
  now: number;
  started_at: number;
  speaking: boolean;
  partial: string;
  transcript: Utterance[];
  notes: string;
  notes_version: number;
  cards: Card[];
  tasks: Task[];
  said: Said[];
  log: LogEntry[];
  asked: Ask | null;
  asks: Ask[];
  expanded: string | null;
  focus: string | null;
  focus_at: number;
  working: WorkItem[];
  health: Health;
}

export const EMPTY_STATE: StatePayload = {
  now: 0,
  started_at: 0,
  speaking: false,
  partial: "",
  transcript: [],
  notes: "",
  notes_version: 0,
  cards: [],
  tasks: [],
  said: [],
  log: [],
  asked: null,
  asks: [],
  expanded: null,
  focus: null,
  focus_at: 0,
  working: [],
  health: {
    stt: null,
    tts: null,
    frontdesk: "starting",
    frontdesk_busy: false,
    classifier_fires: 0,
    classifier_error: null,
    notes_passes: 0,
    notes_error: null,
    frontdesk_error: null,
    wakes: 0,
    research_running: 0,
    audio_age: null,
  },
};

export type DownStateMsg = { type: "state" } & StatePayload;
export interface DownPartialMsg {
  type: "partial";
  text: string;
}
export interface DownAudioMsg {
  type: "audio";
  text: string;
  target: Target;
  audio_b64: string | null;
}
export type DownMsg = DownStateMsg | DownPartialMsg | DownAudioMsg;

export type UpMsg =
  | { type: "hello" }
  | { type: "audio_chunk"; pcm_b64: string }
  | { type: "transcript"; text: string; final: boolean }
  | { type: "inject"; text: string; speaker: string }
  | { type: "reset" };

export type LinkState = "idle" | "connecting" | "open" | "reconnecting";
