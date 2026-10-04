// One camera's sample pack decoded by the browser (design doc 19 §3): the Annex-B access units the Daemon
// kept from the mcap, fetched a GOP at a time by Range and fed to a WebCodecs VideoDecoder (no
// description: Annex-B). A frame is decoded from the keyframe before it - the index's parameter sets go in
// front of that keyframe - and while playing the frames ahead are fed too. Decoded frames become bitmaps
// at once (a held VideoFrame keeps the hardware decoder's picture) in a small cache by frame number; the
// cell draws the frame at the clock, or the newest one before it.
import type { VizFrameIndex } from '../../api/types';
import { concat, continues, fromBase64, keyBefore, nextKey } from '../../lib/vizSamples';
import type { Drawable } from './framePack';

/** What the stream needs of a VideoDecoder. */
export interface DecoderLike {
  readonly decodeQueueSize: number;
  configure(config: VideoDecoderConfig): void;
  decode(chunk: EncodedVideoChunk): void;
  reset(): void;
  close(): void;
}

export interface SampleStreamDeps {
  fetch: typeof fetch;
  createDecoder(init: VideoDecoderInit): DecoderLike;
  chunk(init: EncodedVideoChunkInit): EncodedVideoChunk;
  /** a decoded frame as something a canvas draws; the stream closes the frame afterwards */
  toBitmap(frame: VideoFrame): Promise<Drawable>;
}

const DEFAULT_DEPS: SampleStreamDeps = {
  fetch: (input, init) => globalThis.fetch(input, init),
  createDecoder: (init) => new VideoDecoder(init),
  chunk: (init) => new EncodedVideoChunk(init),
  toBitmap: (frame) => createImageBitmap(frame),
};

/** The decoder config of a sample pack (Annex-B: no description). */
export function decoderConfig(index: VizFrameIndex): VideoDecoderConfig {
  return {
    codec: index.codec_string ?? (index.codec === 'h265' ? 'hvc1.1.6.L120.90' : 'avc1.640028'),
    codedWidth: index.width ?? undefined,
    codedHeight: index.height ?? undefined,
    optimizeForLatency: true,
  };
}

/** Whether this browser decodes the pack (WebCodecs there, and the codec supported). */
export async function canDecodeSamples(index: VizFrameIndex): Promise<boolean> {
  if (typeof VideoDecoder !== 'function' || typeof EncodedVideoChunk !== 'function') return false;
  try {
    const r = await VideoDecoder.isConfigSupported(decoderConfig(index));
    return Boolean(r.supported);
  } catch {
    return false;
  }
}

/** Decoded frames kept, and how many may wait in the decoder's queue. */
const CACHE = 48;
const QUEUE = 8;

export class SampleStream {
  private readonly deps: SampleStreamDeps;
  private readonly config: Uint8Array;
  private readonly frames = new Map<number, Drawable>();
  private readonly gops = new Map<number, Promise<Uint8Array>>();
  private decoder: DecoderLike | null = null;
  private gen = 0;
  private runStart = -1;
  private fedTo = -1;
  private goal = -1;
  private pumping = false;
  private disposed = false;
  private failed = false;
  /** the frame the cell shows: what the cache keeps around */
  private focus = 0;
  /** a frame was decoded (the cell redraws) */
  onFrame: ((i: number) => void) | null = null;
  /** the decoder failed: the cell falls back to <video> */
  onError: ((e: unknown) => void) | null = null;

  constructor(
    private readonly url: string,
    readonly index: VizFrameIndex,
    deps: Partial<SampleStreamDeps> = {},
  ) {
    this.deps = { ...DEFAULT_DEPS, ...deps };
    this.config = fromBase64(index.config);
  }

  get count(): number {
    return this.index.count;
  }

  private get key(): boolean[] {
    return this.index.key ?? [];
  }

  /** The decoded frame i, if it is here. */
  get(i: number): Drawable | undefined {
    return this.frames.get(i);
  }

  /** The newest decoded frame at or before i (what to show while i is on its way). */
  nearest(i: number): Drawable | undefined {
    let best = -1;
    for (const k of this.frames.keys()) if (k <= i && k > best) best = k;
    return best >= 0 ? this.frames.get(best) : undefined;
  }

  /** Frame i is wanted now, and `ahead` frames after it soon. */
  want(i: number, ahead: number): void {
    if (this.disposed || this.failed || i < 0 || i >= this.count) return;
    this.focus = i;
    const goal = Math.min(this.count - 1, i + Math.max(0, ahead));
    // go on with the run that reaches i, wait for i when it is in the decoder, else start over at i's keyframe
    const waiting = this.runStart >= 0 && this.runStart <= i && i <= this.fedTo && this.pending(i);
    if (!this.frames.has(i) && !waiting && !continues(this.runStart, this.fedTo, i, this.key)) this.restart(keyBefore(this.key, i));
    this.goal = goal;
    void this.pump();
  }

  private readonly inQueue = new Set<number>();

  /** Frame i was fed and has not come out yet. */
  private pending(i: number): boolean {
    return this.inQueue.has(i);
  }

  private restart(k: number): void {
    if (k < 0) return;
    this.gen += 1;
    this.inQueue.clear();
    if (this.decoder) {
      try {
        this.decoder.reset();
      } catch {
        this.decoder = null;
      }
    }
    if (!this.decoder) this.decoder = this.makeDecoder();
    if (!this.decoder) return;
    try {
      this.decoder.configure(decoderConfig(this.index));
    } catch (e) {
      this.fail(e);
      return;
    }
    this.runStart = k;
    this.fedTo = k - 1;
  }

  private makeDecoder(): DecoderLike | null {
    try {
      return this.deps.createDecoder({
        output: (frame) => this.output(frame),
        error: (e) => this.fail(e),
      });
    } catch (e) {
      this.fail(e);
      return null;
    }
  }

  private output(frame: VideoFrame): void {
    const i = frame.timestamp;
    this.inQueue.delete(i);
    if (this.disposed) {
      frame.close();
      return;
    }
    void this.deps.toBitmap(frame).then(
      (img) => {
        frame.close();
        if (this.disposed) {
          (img as ImageBitmap).close?.();
          return;
        }
        this.keep(i, img);
        this.onFrame?.(i);
      },
      () => frame.close(),
    );
  }

  private keep(i: number, img: Drawable): void {
    const old = this.frames.get(i);
    if (old && old !== img) (old as ImageBitmap).close?.();
    this.frames.set(i, img);
    if (this.frames.size <= CACHE) return;
    // drop the frames farthest from the one shown, behind it first
    const order = [...this.frames.keys()].sort((a, b) => {
      const da = a < this.focus ? (this.focus - a) * 2 : a - this.focus;
      const db = b < this.focus ? (this.focus - b) * 2 : b - this.focus;
      return db - da;
    });
    for (const k of order.slice(0, this.frames.size - CACHE)) {
      (this.frames.get(k) as ImageBitmap | undefined)?.close?.();
      this.frames.delete(k);
    }
  }

  private fail(e: unknown): void {
    if (this.failed || this.disposed) return;
    this.failed = true;
    this.onError?.(e);
  }

  /** Bytes of the GOP that starts at keyframe k (one Range request, shared). */
  private gop(k: number): Promise<Uint8Array> {
    let p = this.gops.get(k);
    if (!p) {
      const end = nextKey(this.key, k) - 1;
      const start = this.index.offset[k];
      const stop = this.index.offset[end] + this.index.size[end] - 1;
      p = this.deps.fetch(this.url, { headers: { Range: `bytes=${start}-${stop}` } }).then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const buf = new Uint8Array(await r.arrayBuffer());
        return r.status === 206 ? buf : buf.slice(start, stop + 1);
      });
      p.catch(() => this.gops.delete(k));
      this.gops.set(k, p);
      if (this.gops.size > 6) {
        const far = [...this.gops.keys()].sort((a, b) => Math.abs(b - this.focus) - Math.abs(a - this.focus))[0];
        if (far !== k) this.gops.delete(far);
      }
    }
    return p;
  }

  private async sample(i: number): Promise<Uint8Array> {
    const k = keyBefore(this.key, i);
    const bytes = await this.gop(k);
    const from = this.index.offset[i] - this.index.offset[k];
    return bytes.subarray(from, from + this.index.size[i]);
  }

  /** Feeds the run on to the goal, a GOP's bytes at a time, never more than QUEUE frames waiting. */
  private async pump(): Promise<void> {
    if (this.pumping) return;
    this.pumping = true;
    try {
      while (!this.disposed && !this.failed && this.decoder && this.fedTo < this.goal) {
        const gen = this.gen;
        const j = this.fedTo + 1;
        let data: Uint8Array;
        try {
          data = await this.sample(j);
        } catch (e) {
          this.fail(e);
          return;
        }
        if (gen !== this.gen || this.disposed) continue;            // a restart in the meantime
        while (this.decoder && this.decoder.decodeQueueSize >= QUEUE && gen === this.gen && !this.disposed) {
          await new Promise((r) => setTimeout(r, 4));
        }
        if (gen !== this.gen || this.disposed || !this.decoder) continue;
        const first = j === this.runStart;
        try {
          this.decoder.decode(this.deps.chunk({
            type: first || this.key[j] ? 'key' : 'delta',
            timestamp: j,
            data: first && this.config.length ? concat(this.config, data) : data,
          }));
        } catch (e) {
          this.fail(e);
          return;
        }
        this.inQueue.add(j);
        this.fedTo = j;
      }
    } finally {
      this.pumping = false;
    }
  }

  /** Lets go of the decoder and the frames. */
  dispose(): void {
    this.disposed = true;
    this.gen += 1;
    try {
      this.decoder?.close();
    } catch {
      // already closed
    }
    this.decoder = null;
    for (const img of this.frames.values()) (img as ImageBitmap).close?.();
    this.frames.clear();
    this.gops.clear();
  }
}
