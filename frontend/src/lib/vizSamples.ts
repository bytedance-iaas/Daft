// The decoding plan of a sample pack (design doc 19 §3): an mcap H.264 / H.265 camera's access units,
// indexed from the first keyframe, decoded by the browser itself. Pure functions over the C4
// VizFrameIndex; the stateful part (decoder, bytes, bitmaps) is features/visualizer/sampleStream.ts.

/** The keyframe a decode of frame i starts at: the last keyframe at or before it, -1 for none. */
export function keyBefore(key: readonly boolean[], i: number): number {
  for (let k = Math.min(i, key.length - 1); k >= 0; k -= 1) if (key[k]) return k;
  return -1;
}

/** The first keyframe after frame i (where i's GOP ends), or the frame count. */
export function nextKey(key: readonly boolean[], i: number): number {
  for (let k = i + 1; k < key.length; k += 1) if (key[k]) return k;
  return key.length;
}

/**
 * Whether the decode run that started at keyframe `runStart` and has been fed up to frame `fedTo` should
 * go on to reach frame `i`, rather than start over at i's own keyframe: i is not behind the run, not
 * already fed (a frame fed and gone from the cache needs a new run), and not so far ahead that decoding
 * from its keyframe is the shorter way.
 */
export function continues(runStart: number, fedTo: number, i: number, key: readonly boolean[]): boolean {
  if (runStart < 0 || i < runStart || i <= fedTo) return false;
  const k = keyBefore(key, i);
  return k <= fedTo + 1;
}

/** The bytes of a base64 string (the index's parameter sets). */
export function fromBase64(b64: string | null | undefined): Uint8Array {
  if (!b64) return new Uint8Array(0);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}

/** a then b, in one buffer. */
export function concat(a: Uint8Array, b: Uint8Array): Uint8Array {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}
