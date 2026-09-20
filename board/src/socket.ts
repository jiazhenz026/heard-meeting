/**
 * Socket client. Connect, full resync, clock offset, messages up.
 *
 * Every `state` message REPLACES the store. Nothing is merged. The clock
 * offset is taken once, from the first state, so timers never jitter.
 */

import type { DownMsg, StatePayload, UpMsg } from "./types";

export interface SocketHandlers {
  onState: (state: StatePayload) => void;
  onPartial: (text: string) => void;
  onAudio: (msg: Extract<DownMsg, { type: "audio" }>) => void;
  onLink: (state: "connecting" | "open" | "reconnecting") => void;
  onClock: (offsetSeconds: number) => void;
}

export function socketUrl(): string {
  const env = (import.meta as { env?: Record<string, string> }).env;
  const override = env?.VITE_WS_URL;
  if (override) return override;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws`;
}

const BACKOFF_MS = [250, 500, 1000, 2000, 4000, 8000];

export class BoardSocket {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private closed = false;
  private timer: number | null = null;
  private clockOffset: number | null = null;

  constructor(private readonly h: SocketHandlers) {}

  start() {
    this.closed = false;
    this.open();
  }

  stop() {
    this.closed = true;
    if (this.timer !== null) window.clearTimeout(this.timer);
    this.timer = null;
    this.ws?.close();
    this.ws = null;
  }

  get ready(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  send(msg: UpMsg): boolean {
    if (!this.ready) return false;
    this.ws!.send(JSON.stringify(msg));
    return true;
  }

  private open() {
    if (this.closed) return;
    this.h.onLink(this.attempt === 0 ? "connecting" : "reconnecting");
    let ws: WebSocket;
    try {
      ws = new WebSocket(socketUrl());
    } catch {
      this.scheduleRetry();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.h.onLink("open");
      ws.send(JSON.stringify({ type: "hello" } as UpMsg));
    };
    ws.onmessage = (ev) => {
      let msg: DownMsg;
      try {
        msg = JSON.parse(String(ev.data)) as DownMsg;
      } catch {
        return;
      }
      this.dispatch(msg);
    };
    ws.onerror = () => {};
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null;
      this.scheduleRetry();
    };
  }

  private dispatch(msg: DownMsg) {
    switch (msg.type) {
      case "state": {
        const { type: _t, ...state } = msg;
        void _t;
        if (this.clockOffset === null && typeof state.now === "number") {
          this.clockOffset = state.now - Date.now() / 1000;
          this.h.onClock(this.clockOffset);
        }
        this.h.onState(state as StatePayload);
        return;
      }
      case "partial":
        this.h.onPartial(msg.text ?? "");
        return;
      case "audio":
        this.h.onAudio(msg);
        return;
      default:
        return;
    }
  }

  private scheduleRetry() {
    if (this.closed) return;
    this.h.onLink("reconnecting");
    const wait = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)];
    this.attempt += 1;
    if (this.timer !== null) window.clearTimeout(this.timer);
    this.timer = window.setTimeout(() => this.open(), wait);
  }
}
