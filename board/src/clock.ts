/**
 * The server clock. Timers are drawn in the browser, once per second, but
 * against the SERVER's idea of now — never against raw Date.now().
 *
 * `socket.ts` takes the offset once, from the first `state` message. Everything
 * that draws elapsed time calls `serverNow(offset)`.
 */

import { useEffect, useState } from "react";

/** Server-clock seconds, from the offset taken on the first state message. */
export function serverNow(offsetSeconds: number): number {
  return Date.now() / 1000 + offsetSeconds;
}

/** Re-renders once per second, on the second, so every timer ticks together. */
export function useSecondTick(): number {
  const [, setN] = useState(0);
  useEffect(() => {
    let id = 0;
    const align = 1000 - (Date.now() % 1000);
    const start = window.setTimeout(() => {
      setN((n) => n + 1);
      id = window.setInterval(() => setN((n) => n + 1), 1000);
    }, align);
    return () => {
      window.clearTimeout(start);
      if (id) window.clearInterval(id);
    };
  }, []);
  return Date.now();
}

/** m:ss, or h:mm:ss past an hour. Kitchen timers never show milliseconds. */
export function elapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

/** Wall-clock HH:MM:SS of a server timestamp, for the transcript gutter. */
export function stamp(at: number): string {
  if (!at) return "--:--:--";
  const d = new Date(at * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
