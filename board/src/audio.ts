/**
 * NEW-405 · Audio out: the gesture gate and the playback queue.
 *
 * Two facts about browsers shape this file.
 *
 *   1. AUDIO NEEDS A GESTURE FIRST. Nothing here works until a real click has
 *      run `unlock()`. That click is the "Start service" screen, which is also
 *      where the demo begins, so the gate costs nothing as long as nobody
 *      forgets it exists.
 *
 *   2. TWO LINES MUST NEVER OVERLAP. Everything queues. A line that fails to
 *      play does not stall the queue behind it.
 *
 * On an `audio` message (CT-007): if `audio_b64` is present, play it; if it is
 * null, nothing is spoken: §9.1's only fallback is a pre-rendered file.
 */

import type { Target } from "./types";

export interface SpokenLine {
  text: string;
  target: Target;
  audio_b64: string | null;
}

type Listener = (speaking: boolean) => void;

export class AudioOut {
  private queue: SpokenLine[] = [];
  private running = false;
  private unlocked = false;
  private ctx: AudioContext | null = null;
  private listeners = new Set<Listener>();
  private current: SpokenLine | null = null;

  /** Must be called from inside a real user gesture handler. */
  async unlock(): Promise<void> {
    if (this.unlocked) return;
    try {
      const Ctor =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext: typeof AudioContext })
          .webkitAudioContext;
      this.ctx = new Ctor();
      if (this.ctx.state === "suspended") await this.ctx.resume();
      // A zero-length blip: some browsers only consider the context unlocked
      // once something has actually been scheduled on it.
      const buf = this.ctx.createBuffer(1, 1, 22050);
      const src = this.ctx.createBufferSource();
      src.buffer = buf;
      src.connect(this.ctx.destination);
      src.start(0);
    } catch {
      /* no AudioContext: nothing to prime */
    }
    try {
    } catch {
      /* not available */
    }
    this.unlocked = true;
    this.pump();
  }

  get isUnlocked(): boolean {
    return this.unlocked;
  }

  get speakingNow(): SpokenLine | null {
    return this.current;
  }

  onSpeaking(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private emit(speaking: boolean) {
    for (const fn of this.listeners) fn(speaking);
  }

  push(line: SpokenLine) {
    this.queue.push(line);
    this.pump();
  }

  clear() {
    this.queue = [];
    try {
    } catch {
      /* not available */
    }
  }

  private pump() {
    if (this.running || !this.unlocked) return;
    const next = this.queue.shift();
    if (!next) return;
    this.running = true;
    this.current = next;
    this.emit(true);
    this.play(next)
      .catch(() => {
        /* a line that will not play must not stall the queue behind it */
      })
      .finally(() => {
        this.running = false;
        this.current = null;
        this.emit(false);
        this.pump();
      });
  }

  private play(line: SpokenLine): Promise<void> {
    if (line.audio_b64) return this.playBytes(line.audio_b64);
    return this.speak(line.text);
  }

  private playBytes(b64: string): Promise<void> {
    return new Promise((resolve, reject) => {
      let url: string;
      try {
        url = URL.createObjectURL(base64ToBlob(b64));
      } catch (err) {
        reject(err);
        return;
      }
      const el = new Audio(url);
      const done = () => {
        URL.revokeObjectURL(url);
        resolve();
      };
      el.onended = done;
      el.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("audio decode failed"));
      };
      el.play().catch(reject);
    });
  }

  private speak(text: string): Promise<void> {
    // Nothing. §9.1 gives TTS exactly one fallback — "every line in the beat
    // sheet pre-rendered to audio files; the runtime falls back to the file
    // for a known line" — and when that misses, the designed outcome is
    // silence. Synthesising it here with the browser voice would put a
    // different voice in Heard!'s mouth at the worst possible moment, which
    // is worse on stage than saying nothing. The line still reaches the
    // transcript strip, so the room can see what it would have said.
    console.warn("[heard] no audio for a spoken line; staying silent:", text);
    return Promise.resolve();
  }
}

/** The contract does not name a container, so sniff the common ones. */
function base64ToBlob(b64: string): Blob {
  const clean = b64.includes(",") ? b64.slice(b64.indexOf(",") + 1) : b64;
  const bin = atob(clean);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: sniff(bytes) });
}

function sniff(b: Uint8Array): string {
  if (b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46)
    return "audio/wav";
  if (b[0] === 0x4f && b[1] === 0x67 && b[2] === 0x67) return "audio/ogg";
  if (b[0] === 0xff && (b[1] & 0xe0) === 0xe0) return "audio/mpeg";
  if (b[0] === 0x49 && b[1] === 0x44 && b[2] === 0x33) return "audio/mpeg";
  return "audio/mpeg";
}
