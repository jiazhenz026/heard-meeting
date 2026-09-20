/**
 * Microphone in. Two producers from one gesture, and a level so the user can
 * see the board is actually hearing the room.
 *
 *   1. RAW AUDIO. getUserMedia → downsample to 16 kHz mono PCM16 → base64 →
 *      `{"type":"audio_chunk","pcm_b64":...}` (CT-007).
 *
 *   2. BROWSER STT. webkitSpeechRecognition, continuous with interim results →
 *      `{"type":"transcript","text":...,"final":...}` (CT-007).
 *
 * Both go up. The SERVER chooses which source it uses — the board does not
 * decide that and does not try to be clever about it.
 */

export interface MicHandlers {
  onChunk: (pcmB64: string) => void;
  onTranscript: (text: string, final: boolean) => void;
  /** 0..1, for the level meter. */
  onLevel: (level: number) => void;
  onError: (what: string) => void;
}

const TARGET_RATE = 16000;

/** Downsamples and encodes inside the audio thread; posts PCM16 + RMS out. */
const WORKLET_SOURCE = `
class PcmDownsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / ${TARGET_RATE};
    this.carry = 0;
    this.out = [];
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    let sum = 0;
    for (let i = 0; i < ch.length; i++) sum += ch[i] * ch[i];
    const rms = Math.sqrt(sum / ch.length);
    // Linear interpolation down to 16 kHz.
    let pos = this.carry;
    while (pos < ch.length - 1) {
      const i = Math.floor(pos);
      const frac = pos - i;
      const v = ch[i] * (1 - frac) + ch[i + 1] * frac;
      const c = Math.max(-1, Math.min(1, v));
      this.out.push(c < 0 ? c * 0x8000 : c * 0x7fff);
      pos += this.ratio;
    }
    this.carry = pos - ch.length;
    if (this.out.length >= ${TARGET_RATE / 5}) {
      const pcm = new Int16Array(this.out);
      this.out = [];
      this.port.postMessage({ pcm, rms }, [pcm.buffer]);
    } else {
      this.port.postMessage({ rms });
    }
    return true;
  }
}
registerProcessor('pcm-downsampler', PcmDownsampler);
`;

export class Mic {
  private stream: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private node: AudioNode | null = null;
  private recog: SpeechRecognitionLike | null = null;
  private stopped = false;
  private wantRecog = false;

  constructor(private readonly h: MicHandlers) {}

  /** Call from inside the start gesture: permission needs one. */
  async start(): Promise<boolean> {
    this.stopped = false;
    let ok = false;
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      await this.startCapture(this.stream);
      ok = true;
    } catch (err) {
      this.h.onError(
        err instanceof Error ? err.message : "microphone unavailable",
      );
    }
    this.startRecognition();
    return ok;
  }

  stop() {
    this.stopped = true;
    this.wantRecog = false;
    try {
      this.recog?.stop();
    } catch {
      /* already stopped */
    }
    this.recog = null;
    this.node?.disconnect();
    this.node = null;
    this.ctx?.close().catch(() => {});
    this.ctx = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
  }

  // -- raw PCM up ---------------------------------------------------------

  private async startCapture(stream: MediaStream) {
    const Ctor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext: typeof AudioContext })
        .webkitAudioContext;
    const ctx = new Ctor();
    this.ctx = ctx;
    if (ctx.state === "suspended") await ctx.resume();
    const src = ctx.createMediaStreamSource(stream);

    if (ctx.audioWorklet) {
      const url = URL.createObjectURL(
        new Blob([WORKLET_SOURCE], { type: "application/javascript" }),
      );
      try {
        await ctx.audioWorklet.addModule(url);
        const node = new AudioWorkletNode(ctx, "pcm-downsampler");
        node.port.onmessage = (ev: MessageEvent) => {
          const d = ev.data as { pcm?: Int16Array; rms?: number };
          if (typeof d.rms === "number") this.h.onLevel(meter(d.rms));
          if (d.pcm && d.pcm.length) this.h.onChunk(encodePcm(d.pcm));
        };
        src.connect(node);
        // Keep the graph pulling without putting the mic on the speakers.
        const sink = ctx.createGain();
        sink.gain.value = 0;
        node.connect(sink).connect(ctx.destination);
        this.node = node;
        return;
      } finally {
        URL.revokeObjectURL(url);
      }
    }
    this.startScriptProcessor(ctx, src);
  }

  /** Fallback for browsers with no AudioWorklet. Deprecated, but it works. */
  private startScriptProcessor(ctx: AudioContext, src: MediaStreamAudioSourceNode) {
    const node = ctx.createScriptProcessor(4096, 1, 1);
    const ratio = ctx.sampleRate / TARGET_RATE;
    let carry = 0;
    let out: number[] = [];
    node.onaudioprocess = (ev) => {
      const ch = ev.inputBuffer.getChannelData(0);
      let sum = 0;
      for (let i = 0; i < ch.length; i++) sum += ch[i] * ch[i];
      this.h.onLevel(meter(Math.sqrt(sum / ch.length)));
      let pos = carry;
      while (pos < ch.length - 1) {
        const i = Math.floor(pos);
        const frac = pos - i;
        const v = Math.max(-1, Math.min(1, ch[i] * (1 - frac) + ch[i + 1] * frac));
        out.push(v < 0 ? v * 0x8000 : v * 0x7fff);
        pos += ratio;
      }
      carry = pos - ch.length;
      if (out.length >= TARGET_RATE / 5) {
        this.h.onChunk(encodePcm(Int16Array.from(out)));
        out = [];
      }
    };
    src.connect(node);
    const sink = ctx.createGain();
    sink.gain.value = 0;
    node.connect(sink).connect(ctx.destination);
    this.node = node;
  }

  // -- browser STT up -----------------------------------------------------

  private startRecognition() {
    const Ctor =
      (window as unknown as { webkitSpeechRecognition?: SpeechRecognitionCtor })
        .webkitSpeechRecognition ??
      (window as unknown as { SpeechRecognition?: SpeechRecognitionCtor })
        .SpeechRecognition;
    if (!Ctor) {
      this.h.onError("no browser speech recognition; sending raw audio only");
      return;
    }
    this.wantRecog = true;
    const recog = new Ctor();
    recog.continuous = true;
    recog.interimResults = true;
    recog.lang = "en-US";
    recog.onresult = (ev: SpeechRecognitionEventLike) => {
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const r = ev.results[i];
        const text = r[0]?.transcript?.trim() ?? "";
        if (text) this.h.onTranscript(text, Boolean(r.isFinal));
      }
    };
    recog.onerror = (ev: { error?: string }) => {
      if (ev.error === "not-allowed" || ev.error === "service-not-allowed") {
        this.wantRecog = false;
        this.h.onError("speech recognition denied");
      }
    };
    // continuous recognition still ends itself; restart while we want it.
    recog.onend = () => {
      if (this.stopped || !this.wantRecog) return;
      window.setTimeout(() => {
        try {
          recog.start();
        } catch {
          /* already running */
        }
      }, 300);
    };
    try {
      recog.start();
      this.recog = recog;
    } catch {
      this.h.onError("speech recognition would not start");
    }
  }
}

/** RMS is tiny for speech; this is a display curve, not a measurement. */
function meter(rms: number): number {
  return Math.max(0, Math.min(1, Math.sqrt(rms) * 2.6));
}

function encodePcm(pcm: Int16Array): string {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

// The Web Speech API is not in lib.dom for every TS version; name only what
// this file touches rather than pulling a dependency in for it.
interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start(): void;
  stop(): void;
  onresult: ((ev: SpeechRecognitionEventLike) => void) | null;
  onerror: ((ev: { error?: string }) => void) | null;
  onend: (() => void) | null;
}
type SpeechRecognitionCtor = new () => SpeechRecognitionLike;
interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: ArrayLike<
    ArrayLike<{ transcript: string }> & { isFinal: boolean }
  >;
}
