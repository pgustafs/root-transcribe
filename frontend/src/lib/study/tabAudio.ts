/**
 * The shared tab's audio, kept for study notes.
 *
 * Only the tab's own audio track feeds this buffer, never the microphone. It
 * holds the last 15 seconds, counted in samples. A note takes that look-back
 * and, while it stays open, every block that follows: both come from the same
 * stream of blocks at one synchronous point, so nothing is dropped or doubled
 * at the join.
 *
 * Pausing, resuming or changing the source starts a new interval (`reset`):
 * audio from before can never appear in a later note.
 */

export const NOTE_SAMPLE_RATE = 16000;
export const LOOKBACK_SECONDS = 15;
const CHUNK_SECONDS = 5;

export type NoteChunkSink = (index: number, samples: Float32Array) => void;

type OpenNote = {
  sink: NoteChunkSink;
  nextIndex: number;
  pending: Float32Array[];
  pendingSamples: number;
  totalSamples: number;
};

function concat(parts: Float32Array[], total: number): Float32Array {
  const out = new Float32Array(total);
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}

export class TabAudioBuffer {
  private ring: Float32Array[] = [];
  private ringSamples = 0;
  private open: OpenNote | null = null;
  private gateOpen = true;
  private readonly maxSamples: number;
  private readonly chunkSamples: number;

  constructor(
    readonly sampleRate = NOTE_SAMPLE_RATE,
    lookbackSeconds = LOOKBACK_SECONDS,
    chunkSeconds = CHUNK_SECONDS,
  ) {
    this.maxSamples = Math.round(sampleRate * lookbackSeconds);
    this.chunkSamples = Math.round(sampleRate * chunkSeconds);
  }

  /** One block from the audio thread (mono). Ignored while the gate is shut. */
  append(block: Float32Array): void {
    if (!this.gateOpen || block.length === 0) return;
    // The audio thread reuses nothing here: each message is its own array.
    this.ring.push(block);
    this.ringSamples += block.length;
    while (
      this.ring.length > 1 &&
      this.ringSamples - this.ring[0].length >= this.maxSamples
    ) {
      this.ringSamples -= this.ring[0].length;
      this.ring.shift();
    }
    if (this.open) this.feed(this.open, block);
  }

  /** Shut while the recording is paused: nothing is kept. */
  setGate(open: boolean): void {
    this.gateOpen = open;
  }

  /** Start a new interval: forget everything heard so far. */
  reset(): void {
    this.ring = [];
    this.ringSamples = 0;
  }

  /** Samples available for a look-back right now (at most 15 seconds). */
  available(): number {
    return Math.min(this.ringSamples, this.maxSamples);
  }

  get noteOpen(): boolean {
    return this.open !== null;
  }

  /** The newest samples, at most 15 seconds. */
  lookback(): Float32Array {
    const all = concat(this.ring, this.ringSamples);
    return all.length > this.maxSamples
      ? all.slice(all.length - this.maxSamples)
      : all;
  }

  /**
   * Open a note: the look-back goes to `sink` first, then every later block
   * in chunks of five seconds. Returns the look-back length in samples.
   */
  beginNote(sink: NoteChunkSink): number {
    if (this.open) throw new Error("A note is already open");
    const note: OpenNote = {
      sink,
      nextIndex: 0,
      pending: [],
      pendingSamples: 0,
      totalSamples: 0,
    };
    const lookback = this.lookback();
    if (lookback.length > 0) {
      note.totalSamples = lookback.length;
      sink(note.nextIndex++, lookback);
    }
    this.open = note;
    return lookback.length;
  }

  /** Close the open note, flushing what is left. Returns its total samples. */
  endNote(): number {
    const note = this.open;
    if (!note) return 0;
    this.flush(note);
    this.open = null;
    return note.totalSamples;
  }

  private feed(note: OpenNote, block: Float32Array): void {
    note.pending.push(block);
    note.pendingSamples += block.length;
    note.totalSamples += block.length;
    if (note.pendingSamples >= this.chunkSamples) this.flush(note);
  }

  private flush(note: OpenNote): void {
    if (note.pendingSamples === 0) return;
    note.sink(note.nextIndex++, concat(note.pending, note.pendingSamples));
    note.pending = [];
    note.pendingSamples = 0;
  }
}

/** 16-bit mono PCM WAV: decodable on its own, unlike a slice of WebM. */
export function encodeWav(parts: Float32Array[], sampleRate: number): Blob {
  const samples = parts.reduce((sum, part) => sum + part.length, 0);
  const buffer = new ArrayBuffer(44 + samples * 2);
  const view = new DataView(buffer);
  const text = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i += 1)
      view.setUint8(offset + i, value.charCodeAt(i));
  };
  text(0, "RIFF");
  view.setUint32(4, 36 + samples * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, samples * 2, true);
  let offset = 44;
  for (const part of parts) {
    for (let i = 0; i < part.length; i += 1) {
      const clamped = Math.max(-1, Math.min(1, part[i]));
      view.setInt16(
        offset,
        clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff,
        true,
      );
      offset += 2;
    }
  }
  return new Blob([buffer], { type: "audio/wav" });
}

export function samplesToMs(samples: number, sampleRate: number): number {
  return Math.round((samples / sampleRate) * 1000);
}
