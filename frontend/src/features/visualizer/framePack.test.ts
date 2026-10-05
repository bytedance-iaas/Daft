import { describe, expect, it, vi } from 'vitest';
import type { VizFrameIndex } from '../../api/types';
import { FramePack, type Drawable } from './framePack';

// 60 frames at 10 fps, 10 bytes each, back to back
const N = 60;
const INDEX: VizFrameIndex = {
  camera: 'cam', codec: 'jpeg', width: 4, height: 3, count: N,
  t: Array.from({ length: N }, (_, i) => i / 10),
  offset: Array.from({ length: N }, (_, i) => i * 10),
  size: Array.from({ length: N }, () => 10),
  bytes: N * 10,
};

/** A Blob's bytes (jsdom's Blob has no arrayBuffer(); FileReader reads it). */
function bytesOf(blob: Blob): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(new Uint8Array(r.result as ArrayBuffer));
    r.onerror = () => reject(r.error);
    r.readAsArrayBuffer(blob);
  });
}

function world(rawBytes = 1 << 20) {
  const ranges: string[] = [];
  const decoded: number[] = [];
  const fetchFn = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    const range = (init?.headers as Record<string, string>).Range;
    ranges.push(range);
    const [a, b] = range.replace('bytes=', '').split('-').map(Number);
    // every byte of a frame is its frame number
    return new Response(new Uint8Array(b - a + 1).map((_, p) => Math.floor((a + p) / 10)), { status: 206 });
  }) as unknown as typeof fetch;
  const decode = async (blob: Blob) => {
    const first = (await bytesOf(blob))[0];
    decoded.push(first);
    return { width: 4, height: 3, frame: first } as unknown as Drawable;
  };
  const pack = new FramePack('/x.frames', INDEX, { fetch: fetchFn, decode, rawBytes, runFrames: 24 });
  return { pack, ranges, decoded };
}

describe('FramePack: about 5 s ahead, bytes only (requester 2026-10-05)', () => {
  it('decodes near the frame shown, keeps the bytes further on, and tells how far they reach', async () => {
    const w = world();
    await w.pack.want(0, 2, 30);
    await vi.waitFor(() => expect(w.pack.ahead(0)).toBeCloseTo(2.4));
    // the run brought 0-23: 0-2 decoded, the rest kept as bytes
    expect(w.decoded.sort((a, b) => a - b)).toEqual([0, 1, 2]);
    expect(w.ranges).toEqual(['bytes=0-239']);
    // more than half of the 30 still here: nothing new is asked yet
    await w.pack.want(1, 2, 30);
    expect(w.ranges).toHaveLength(1);
    // further on, fewer than half are left: the next batch is fetched, and only what is near gets decoded
    await w.pack.want(10, 2, 30);
    expect(w.decoded).toEqual(expect.arrayContaining([10, 11, 12]));
    expect(w.decoded).not.toContain(13);
    // (a run takes up to 24 neighbours in one request: 24-47)
    await vi.waitFor(() => expect(w.ranges).toEqual(['bytes=0-239', 'bytes=240-479']));
    await vi.waitFor(() => expect(w.pack.ahead(1.0)).toBeCloseTo(3.8));
    // fetched to the last frame: as far as it goes
    await w.pack.want(40, 2, 30);
    await vi.waitFor(() => expect(w.pack.ahead(4.0)).toBe(Infinity));
    expect(w.pack.ahead(-1)).toBe(Infinity);
  });

  it('holds nothing where nothing came, and over its byte budget lets the frames behind go first', async () => {
    const w = world(100);                                                // ten frames' bytes
    expect(w.pack.ahead(0)).toBe(0);
    await w.pack.want(0, 0, 20);
    await vi.waitFor(() => expect(w.ranges).toHaveLength(1));
    await w.pack.want(15, 0, 0);                                        // frames 1-23 were bytes; now at 15
    await w.pack.want(15, 0, 20);
    await vi.waitFor(() => expect(w.pack.ahead(1.5)).toBeGreaterThan(0.8));
    // the budget kept the frames from 15 on, not the ones behind
    expect(w.pack.ahead(0.1)).toBe(0);
  });
});
