import { describe, expect, it } from 'vitest';
import { concat, continues, fromBase64, keyBefore, nextKey } from './vizSamples';

// a keyframe every ten frames, 30 frames
const KEY = Array.from({ length: 30 }, (_, i) => i % 10 === 0);

describe('vizSamples (design doc 19 §3)', () => {
  it('finds the keyframe a frame decodes from and where its GOP ends', () => {
    expect([keyBefore(KEY, 0), keyBefore(KEY, 9), keyBefore(KEY, 10), keyBefore(KEY, 29), keyBefore(KEY, 99)]).toEqual([0, 0, 10, 20, 20]);
    expect(keyBefore([false, false, true], 1)).toBe(-1);
    expect([nextKey(KEY, 0), nextKey(KEY, 10), nextKey(KEY, 25)]).toEqual([10, 20, 30]);
  });

  it('goes on with a run that reaches the frame, starts over when it is behind, fed or a GOP away', () => {
    expect(continues(0, 5, 6, KEY)).toBe(true);           // the next frame
    expect(continues(0, 5, 9, KEY)).toBe(true);           // same GOP
    expect(continues(0, 9, 12, KEY)).toBe(true);          // the next GOP starts right after what was fed
    expect(continues(0, 5, 15, KEY)).toBe(false);         // a whole GOP to skip: start at 10
    expect(continues(10, 15, 5, KEY)).toBe(false);        // behind the run
    expect(continues(0, 5, 3, KEY)).toBe(false);          // fed already (and not in the cache)
    expect(continues(-1, -1, 0, KEY)).toBe(false);        // no run yet
  });

  it('reads the parameter sets from base64 and puts them in front', () => {
    const config = fromBase64(btoa(String.fromCharCode(0, 0, 0, 1, 0x67)));
    expect([...config]).toEqual([0, 0, 0, 1, 0x67]);
    expect(fromBase64(null)).toHaveLength(0);
    expect([...concat(config, new Uint8Array([9]))]).toEqual([0, 0, 0, 1, 0x67, 9]);
  });
});
