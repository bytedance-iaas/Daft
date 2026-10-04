import { describe, expect, it, vi } from 'vitest';
import type { VizFrameIndex } from '../../api/types';
import { SampleStream, type DecoderLike, type SampleStreamDeps } from './sampleStream';

// 30 frames of 10 bytes each (every byte the frame number), a keyframe every 10
const N = 30;
const SIZE = 10;
const PACK = new Uint8Array(N * SIZE).map((_, b) => Math.floor(b / SIZE));
const CONFIG = [0, 0, 0, 1, 0x67, 0x64];
const INDEX: VizFrameIndex = {
  camera: 'wrist', codec: 'h264', width: 64, height: 48, count: N,
  t: Array.from({ length: N }, (_, i) => i / 10),
  offset: Array.from({ length: N }, (_, i) => i * SIZE),
  size: Array.from({ length: N }, () => SIZE),
  bytes: N * SIZE,
  key: Array.from({ length: N }, (_, i) => i % 10 === 0),
  codec_string: 'avc1.64000a',
  config: btoa(String.fromCharCode(...CONFIG)),
};

interface Fed { type: string; timestamp: number; data: Uint8Array }

function world() {
  const fed: Fed[] = [];
  const calls: string[] = [];
  const ranges: string[] = [];
  let output: ((f: VideoFrame) => void) | null = null;
  let error: ((e: DOMException) => void) | null = null;
  const decoder: DecoderLike = {
    decodeQueueSize: 0,
    configure: (c) => calls.push(`configure ${c.codec}`),
    decode: (chunk) => {
      const c = chunk as unknown as Fed;
      fed.push(c);
      // decodes in order, a little later
      queueMicrotask(() => output?.({ timestamp: c.timestamp, close: () => undefined } as unknown as VideoFrame));
    },
    reset: () => calls.push('reset'),
    close: () => calls.push('close'),
  };
  const deps: SampleStreamDeps = {
    fetch: vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const range = (init?.headers as Record<string, string>).Range;
      ranges.push(range);
      const [a, b] = range.replace('bytes=', '').split('-').map(Number);
      return new Response(PACK.slice(a, b + 1), { status: 206 });
    }) as unknown as typeof fetch,
    createDecoder: (init) => {
      output = init.output;
      error = init.error;
      return decoder;
    },
    chunk: (init) => ({ type: init.type, timestamp: init.timestamp, data: new Uint8Array(init.data as ArrayBuffer) }) as unknown as EncodedVideoChunk,
    toBitmap: async (frame) => ({ width: 64, height: 48, frame: frame.timestamp }) as unknown as ImageBitmap,
  };
  return { fed, calls, ranges, deps, fail: (e: DOMException) => error?.(e) };
}

async function settle(stream: SampleStream, i: number) {
  await vi.waitFor(() => expect(stream.get(i)).toBeDefined());
}

describe('SampleStream (design doc 19 §3)', () => {
  it('decodes a frame from the keyframe before it, the parameter sets in front, a GOP fetched at a time', async () => {
    const w = world();
    const s = new SampleStream('/x.frames', INDEX, w.deps);
    s.want(5, 0);
    await settle(s, 5);
    expect(w.calls).toEqual(['configure avc1.64000a']);
    expect(w.fed.map((f) => f.timestamp)).toEqual([0, 1, 2, 3, 4, 5]);
    expect(w.fed.map((f) => f.type)).toEqual(['key', 'delta', 'delta', 'delta', 'delta', 'delta']);
    expect([...w.fed[0].data]).toEqual([...CONFIG, ...Array(SIZE).fill(0)]);
    expect([...w.fed[3].data]).toEqual(Array(SIZE).fill(3));
    expect(w.ranges).toEqual(['bytes=0-99']);                    // frames 0-9 in one request
    expect(s.get(4)).toBeDefined();
  });

  it('plays on from where it is; a jump past a GOP or back starts over at that keyframe', async () => {
    const w = world();
    const s = new SampleStream('/x.frames', INDEX, w.deps);
    s.want(2, 4);                                                   // shown 2, fed up to 6
    await settle(s, 6);
    s.want(7, 2);                                                   // the same run goes on
    await settle(s, 9);
    expect(w.calls).toEqual(['configure avc1.64000a']);
    expect(w.fed.map((f) => f.timestamp)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
    s.want(25, 0);                                                  // a GOP away: start at 20
    await settle(s, 25);
    expect(w.calls).toEqual(['configure avc1.64000a', 'reset', 'configure avc1.64000a']);
    expect(w.fed.slice(10).map((f) => f.timestamp)).toEqual([20, 21, 22, 23, 24, 25]);
    expect([...w.fed[10].data.slice(0, CONFIG.length)]).toEqual(CONFIG);
    s.want(3, 0);                                                   // decoded before: no new run
    expect(w.calls).toHaveLength(3);
    s.want(14, 0);                                                  // behind the run, not decoded: from 10
    await settle(s, 14);
    expect(w.fed.slice(16).map((f) => f.timestamp)).toEqual([10, 11, 12, 13, 14]);
    expect(s.nearest(19)).toBe(s.get(14));
  });

  it('tells the cell when the decoder fails, and stops', async () => {
    const w = world();
    const s = new SampleStream('/x.frames', INDEX, w.deps);
    const failed = vi.fn();
    s.onError = failed;
    s.want(0, 0);
    await settle(s, 0);
    w.fail(new DOMException('bad stream', 'EncodingError'));
    expect(failed).toHaveBeenCalledTimes(1);
    s.want(5, 0);
    await Promise.resolve();
    expect(w.fed).toHaveLength(1);
    s.dispose();
    expect(w.calls.at(-1)).toBe('close');
  });
});
