// A JPEG / PNG frame pack read by Range (design doc 18 §4.2: an mcap camera's own frames, back to
// back, with an index of episode times, offsets and sizes). Frames are fetched in runs - the
// neighbours of the one asked for, in one request while they sit next to each other - decoded to
// bitmaps and kept in a small LRU; playing asks for the frames ahead so they are ready in time.
// Further ahead (about 5 s, requester 2026-10-05) only the bytes are fetched - a decoded frame is
// megabytes - in batches, and decoded when they come near the frame shown; `ahead` tells the clock
// how far the fetched frames reach.
import type { VizFrameIndex } from '../../api/types';
import { bisectRight } from '../../lib/vizTime';

export type Drawable = ImageBitmap | HTMLImageElement;

export interface FramePackOptions {
  /** frames kept decoded */
  cache: number;
  /** bytes of fetched frames kept undecoded (the ones ahead) */
  rawBytes: number;
  /** frames fetched together at most, and bytes per request at most */
  runFrames: number;
  runBytes: number;
  fetch: typeof fetch;
  decode: (blob: Blob) => Promise<Drawable>;
}

async function defaultDecode(blob: Blob): Promise<Drawable> {
  if (typeof createImageBitmap === 'function') return createImageBitmap(blob);
  const url = URL.createObjectURL(blob);
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    return img;
  } finally {
    URL.revokeObjectURL(url);
  }
}

const DEFAULTS: FramePackOptions = {
  cache: 90,
  rawBytes: 64 << 20,
  runFrames: 24,
  runBytes: 4 << 20,
  fetch: (input, init) => globalThis.fetch(input, init),
  decode: defaultDecode,
};

export class FramePack {
  private readonly cache = new Map<number, Drawable>();
  private readonly inflight = new Map<number, Promise<void>>();
  /** fetched, not decoded yet, and the ones being decoded from it */
  private readonly raw = new Map<number, Blob>();
  private readonly decoding = new Set<number>();
  private rawSize = 0;
  /** the frame shown and how far after it frames are decoded (what the last `want` asked) */
  private focus = 0;
  private decodeTo = 0;
  private readonly opts: FramePackOptions;
  private disposed = false;
  /** called when a frame arrives (the cell redraws) */
  onFrame: ((k: number) => void) | null = null;

  constructor(
    private readonly url: string,
    readonly index: VizFrameIndex,
    opts: Partial<FramePackOptions> = {},
  ) {
    this.opts = { ...DEFAULTS, ...opts };
  }

  get count(): number {
    return this.index.count;
  }

  /** A decoded frame, if it is here (and marks it recently used). */
  get(k: number): Drawable | undefined {
    const d = this.cache.get(k);
    if (d) {
      this.cache.delete(k);
      this.cache.set(k, d);
    }
    return d;
  }

  /** The nearest decoded frame at or before k (what to show while k loads). */
  nearest(k: number): Drawable | undefined {
    for (let i = k; i >= 0 && i > k - 30; i -= 1) {
      const d = this.cache.get(i);
      if (d) return d;
    }
    return undefined;
  }

  /**
   * Makes sure frame k and `ahead` frames after it are decoded or on their way, and the bytes of
   * `prefetch` frames after those (fetched in batches: nothing new while more than half of them are here).
   */
  want(k: number, ahead = 0, prefetch = 0): Promise<void> {
    if (this.disposed || k < 0 || k >= this.count) return Promise.resolve();
    const waits: Promise<void>[] = [];
    let i = k;
    const last = Math.min(this.count - 1, k + ahead);
    this.focus = k;
    this.decodeTo = last;
    while (i <= last) {
      if (this.cache.has(i) || this.decoding.has(i)) {
        i += 1;
        continue;
      }
      const blob = this.raw.get(i);
      if (blob) {
        waits.push(this.decodeRaw(i, blob));
        i += 1;
        continue;
      }
      const busy = this.inflight.get(i);
      if (busy) {
        waits.push(busy);
        i += 1;
        continue;
      }
      const run = this.runFrom(i, last);
      waits.push(this.fetchRun(run));
      i = run[run.length - 1] + 1;
    }
    if (prefetch > 0) this.prefetchFrom(last + 1, prefetch);
    return Promise.all(waits).then(() => undefined);
  }

  /** Seconds from t on whose frames are here (decoded or fetched); Infinity once that reaches the last frame. */
  ahead(t: number): number {
    const times = this.index.t;
    if (!this.count) return Infinity;
    const k = Math.max(0, bisectRight(times, t + 1e-6));
    let j = k;
    while (j < this.count && this.has(j)) j += 1;
    if (j >= this.count) return Infinity;
    if (j === k) return 0;
    return Math.max(0, times[j] - Math.max(t, times[k]));
  }

  private has(k: number): boolean {
    return this.cache.has(k) || this.raw.has(k) || this.decoding.has(k);
  }

  /** The bytes of `n` frames from i on: when fewer than half of them are here, fetch up to all n. */
  private prefetchFrom(i: number, n: number): void {
    const end = Math.min(this.count - 1, i + n - 1);
    let have = i;
    while (have <= end && (this.has(have) || this.inflight.has(have))) have += 1;
    if (have > end || have - i >= n / 2) return;
    let j = have;
    while (j <= end) {
      if (this.has(j) || this.inflight.has(j)) {
        j += 1;
        continue;
      }
      const run = this.runFrom(j, end);
      void this.fetchRun(run);
      j = run[run.length - 1] + 1;
    }
  }

  private decodeRaw(k: number, blob: Blob): Promise<void> {
    this.raw.delete(k);
    this.rawSize -= blob.size;
    this.decoding.add(k);
    return this.opts
      .decode(blob)
      .then((d) => {
        if (this.disposed) {
          if ('close' in d) d.close();
          return;
        }
        this.put(k, d);
        this.onFrame?.(k);
      })
      .catch(() => undefined)
      .finally(() => this.decoding.delete(k));
  }

  /** Bytes kept for later; over the budget, the frames farthest from the one shown go (those behind first). */
  private keepRaw(k: number, blob: Blob): void {
    if (this.cache.has(k) || this.raw.has(k)) return;
    this.raw.set(k, blob);
    this.rawSize += blob.size;
    if (this.rawSize <= this.opts.rawBytes) return;
    const order = [...this.raw.keys()].sort((a, b) => {
      const da = a < this.focus ? (this.focus - a) * 4 : a - this.focus;
      const db = b < this.focus ? (this.focus - b) * 4 : b - this.focus;
      return db - da;
    });
    for (const x of order) {
      if (this.rawSize <= this.opts.rawBytes) break;
      this.rawSize -= (this.raw.get(x) as Blob).size;
      this.raw.delete(x);
    }
  }

  /** Frames from i that one request can bring: contiguous bytes, not cached, within the limits. */
  private runFrom(i: number, last: number): number[] {
    const { offset, size } = this.index;
    const run = [i];
    let bytes = size[i];
    let j = i + 1;
    const stop = Math.max(last, Math.min(this.count - 1, i + this.opts.runFrames - 1));
    while (j <= stop && run.length < this.opts.runFrames && !this.cache.has(j) && !this.inflight.has(j)) {
      if (offset[j] !== offset[j - 1] + size[j - 1] || bytes + size[j] > this.opts.runBytes) break;
      bytes += size[j];
      run.push(j);
      j += 1;
    }
    return run;
  }

  private fetchRun(run: number[]): Promise<void> {
    const { offset, size, codec } = this.index;
    const start = offset[run[0]];
    const end = offset[run[run.length - 1]] + size[run[run.length - 1]] - 1;
    const type = codec === 'png' ? 'image/png' : 'image/jpeg';
    const job = (async () => {
      const res = await this.opts.fetch(this.url, { headers: { Range: `bytes=${start}-${end}` } });
      if (!res.ok) throw new Error(`frame pack ${res.status}`);
      const buf = new Uint8Array(await res.arrayBuffer());
      // a server without Range answers the whole pack: cut from the absolute offsets then
      const base = res.status === 206 ? start : 0;
      await Promise.all(
        run.map(async (k) => {
          const a = offset[k] - base;
          const blob = new Blob([buf.subarray(a, a + size[k])], { type });
          // beyond the frames to show soon: the bytes only, decoded when they come near
          if (k < this.focus || k > this.decodeTo) {
            if (!this.disposed) this.keepRaw(k, blob);
            return;
          }
          const drawable = await this.opts.decode(blob);
          if (this.disposed) {
            if ('close' in drawable) drawable.close();
            return;
          }
          this.put(k, drawable);
          this.onFrame?.(k);
        }),
      );
    })();
    const settled = job.catch(() => undefined).finally(() => run.forEach((k) => this.inflight.delete(k)));
    run.forEach((k) => this.inflight.set(k, settled));
    return settled;
  }

  private put(k: number, d: Drawable): void {
    this.cache.set(k, d);
    while (this.cache.size > this.opts.cache) {
      const oldest = this.cache.keys().next().value as number;
      const gone = this.cache.get(oldest);
      this.cache.delete(oldest);
      if (gone && 'close' in gone) gone.close();
    }
  }

  /** Drops the decoded frames and the fetched bytes (the pack can still be used: they are fetched again when asked). */
  clear(): void {
    for (const d of this.cache.values()) if ('close' in d) d.close();
    this.cache.clear();
    this.raw.clear();
    this.rawSize = 0;
  }

  dispose(): void {
    this.disposed = true;
    this.clear();
    this.onFrame = null;
  }
}
