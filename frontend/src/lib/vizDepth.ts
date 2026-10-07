// Depth pictures (design doc 21 §5.2, §5.5, D68): a depth pack's frames are 16-bit greyscale PNGs, which
// the browser cannot decode at full depth itself (createImageBitmap brings them down to 8 bits), so they
// are decoded here - the chunks, the IDAT inflated by the browser's own DecompressionStream, PNG's five
// row filters undone - into millimetres, then coloured (turbo or grey) between a low and a high value,
// holes (0) left transparent.

export interface DepthPicture {
  width: number;
  height: number;
  /** millimetres (times the index's scale), row by row; 0 is a hole */
  data: Uint16Array;
}

export type DepthColormap = 'turbo' | 'gray';

/** How a depth cell draws (kept per cell; saved with a layout). */
export interface DepthView {
  cmap: DepthColormap;
  /** the colour scale's ends in millimetres; null: the episode's 2 % / 98 % from the index */
  lo: number | null;
  hi: number | null;
  /** drawn over its camera (when they pair and their aspect ratios agree) */
  overlay: boolean;
  /** the depth's opacity over the camera, 0-1 */
  opacity: number;
}

export const DEFAULT_DEPTH_VIEW: DepthView = { cmap: 'turbo', lo: null, hi: null, overlay: false, opacity: 0.5 };

const SIG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

function u32(b: Uint8Array, at: number): number {
  return ((b[at] << 24) | (b[at + 1] << 16) | (b[at + 2] << 8) | b[at + 3]) >>> 0;
}

async function pipe(bytes: Uint8Array, stream: { readable: ReadableStream<Uint8Array>; writable: WritableStream<BufferSource> }): Promise<Uint8Array> {
  const input = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(bytes);
      c.close();
    },
  });
  const reader = input.pipeThrough(stream as unknown as ReadableWritablePair<Uint8Array, Uint8Array>).getReader();
  const parts: Uint8Array[] = [];
  let n = 0;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    parts.push(value);
    n += value.length;
  }
  const out = new Uint8Array(n);
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

/** The pixels of a 16-bit greyscale, not interlaced PNG; throws on anything else. */
export async function decodePng16(png: Uint8Array): Promise<DepthPicture> {
  if (png.length < 33 || SIG.some((v, i) => png[i] !== v)) throw new Error('not a PNG');
  let at = 8;
  let width = 0;
  let height = 0;
  const idat: Uint8Array[] = [];
  let total = 0;
  while (at + 8 <= png.length) {
    const len = u32(png, at);
    const type = String.fromCharCode(png[at + 4], png[at + 5], png[at + 6], png[at + 7]);
    const data = png.subarray(at + 8, at + 8 + len);
    if (type === 'IHDR') {
      width = u32(data, 0);
      height = u32(data, 4);
      const [depth, color, , , interlace] = [data[8], data[9], data[10], data[11], data[12]];
      if (depth !== 16 || color !== 0 || interlace !== 0) throw new Error(`not a 16-bit greyscale PNG (${depth}-bit, colour ${color}, interlace ${interlace})`);
    } else if (type === 'IDAT') {
      idat.push(data);
      total += data.length;
    } else if (type === 'IEND') break;
    at += 12 + len;
  }
  const z = new Uint8Array(total);
  let pos = 0;
  for (const part of idat) {
    z.set(part, pos);
    pos += part.length;
  }
  const raw = await pipe(z, new DecompressionStream('deflate'));
  const stride = width * 2;
  if (raw.length < height * (stride + 1)) throw new Error('PNG data too short');
  const out = new Uint16Array(width * height);
  let prev = new Uint8Array(stride);
  let cur = new Uint8Array(stride);
  for (let y = 0; y < height; y += 1) {
    const base = y * (stride + 1);
    const filter = raw[base];
    for (let i = 0; i < stride; i += 1) {
      const x = raw[base + 1 + i];
      const left = i >= 2 ? cur[i - 2] : 0;
      const up = prev[i];
      let v: number;
      switch (filter) {
        case 0:
          v = x;
          break;
        case 1:
          v = x + left;
          break;
        case 2:
          v = x + up;
          break;
        case 3:
          v = x + ((left + up) >> 1);
          break;
        case 4: {
          const ul = i >= 2 ? prev[i - 2] : 0;
          const p = left + up - ul;
          const pa = Math.abs(p - left);
          const pb = Math.abs(p - up);
          const pc = Math.abs(p - ul);
          v = x + (pa <= pb && pa <= pc ? left : pb <= pc ? up : ul);
          break;
        }
        default:
          throw new Error(`PNG filter ${filter}`);
      }
      cur[i] = v & 0xff;
    }
    const row = y * width;
    for (let i = 0; i < width; i += 1) out[row + i] = (cur[2 * i] << 8) | cur[2 * i + 1];
    const t = prev;
    prev = cur;
    cur = t;
  }
  return { width, height, data: out };
}

let crcTable: Uint32Array | null = null;

function crc32(bytes: Uint8Array): number {
  if (!crcTable) {
    crcTable = new Uint32Array(256);
    for (let n = 0; n < 256; n += 1) {
      let c = n;
      for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      crcTable[n] = c >>> 0;
    }
  }
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) c = crcTable[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type: string, data: Uint8Array): Uint8Array {
  const out = new Uint8Array(12 + data.length);
  const view = new DataView(out.buffer);
  view.setUint32(0, data.length);
  for (let i = 0; i < 4; i += 1) out[4 + i] = type.charCodeAt(i);
  out.set(data, 8);
  view.setUint32(8 + data.length, crc32(out.subarray(4, 8 + data.length)));
  return out;
}

/** A 16-bit greyscale PNG of a picture (the mock world's packs and the tests; the Daemon makes the real ones). */
export async function encodePng16(pic: DepthPicture): Promise<Uint8Array> {
  const { width, height, data } = pic;
  const raw = new Uint8Array(height * (width * 2 + 1));
  for (let y = 0; y < height; y += 1) {
    const base = y * (width * 2 + 1);
    raw[base] = 0;
    for (let x = 0; x < width; x += 1) {
      const v = data[y * width + x];
      raw[base + 1 + 2 * x] = v >> 8;
      raw[base + 2 + 2 * x] = v & 0xff;
    }
  }
  const z = await pipe(raw, new CompressionStream('deflate'));
  const ihdr = new Uint8Array(13);
  const view = new DataView(ihdr.buffer);
  view.setUint32(0, width);
  view.setUint32(4, height);
  ihdr.set([16, 0, 0, 0, 0], 8);
  const parts = [new Uint8Array(SIG), chunk('IHDR', ihdr), chunk('IDAT', z), chunk('IEND', new Uint8Array(0))];
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let pos = 0;
  for (const p of parts) {
    out.set(p, pos);
    pos += p.length;
  }
  return out;
}

// ---------------------------------------------------------------- colours

function turboRgb(x: number): [number, number, number] {
  // Google's turbo, polynomial approximation (Mikhailov, 2019)
  const r = 0.13572138 + x * (4.6153926 + x * (-42.66032258 + x * (132.13108234 + x * (-152.94239396 + x * 59.28637943))));
  const g = 0.09140261 + x * (2.19418839 + x * (4.84296658 + x * (-14.18503333 + x * (4.27729857 + x * 2.82956604))));
  const b = 0.1066733 + x * (12.64194608 + x * (-60.58204836 + x * (110.36276771 + x * (-89.90310912 + x * 27.34824973))));
  const c = (v: number) => Math.max(0, Math.min(255, Math.round(v * 255)));
  return [c(r), c(g), c(b)];
}

const LUTS = new Map<DepthColormap, Uint8Array>();

/** 256 RGB entries of a colormap, near to far. */
export function lut(cmap: DepthColormap): Uint8Array {
  let t = LUTS.get(cmap);
  if (!t) {
    t = new Uint8Array(256 * 3);
    for (let i = 0; i < 256; i += 1) {
      const [r, g, b] = cmap === 'turbo' ? turboRgb(i / 255) : [i, i, i];
      t.set([r, g, b], i * 3);
    }
    LUTS.set(cmap, t);
  }
  return t;
}

/** The CSS gradient of a colormap (the legend bar). */
export function gradient(cmap: DepthColormap): string {
  const t = lut(cmap);
  const stops = [0, 32, 64, 96, 128, 160, 192, 224, 255].map((i) => `rgb(${t[i * 3]},${t[i * 3 + 1]},${t[i * 3 + 2]}) ${Math.round((i / 255) * 100)}%`);
  return `linear-gradient(to right, ${stops.join(', ')})`;
}

/** RGBA pixels of a depth picture between lo and hi (beyond them: the ends), holes transparent. */
export function colorize(pic: DepthPicture, lo: number, hi: number, cmap: DepthColormap): Uint8ClampedArray<ArrayBuffer> {
  const table = lut(cmap);
  const out = new Uint8ClampedArray(pic.width * pic.height * 4);
  const span = Math.max(1, hi - lo);
  const { data } = pic;
  for (let i = 0; i < data.length; i += 1) {
    const v = data[i];
    if (v === 0) continue; // a hole: alpha stays 0
    let k = Math.round(((v - lo) / span) * 255);
    k = k < 0 ? 0 : k > 255 ? 255 : k;
    const o = i * 4;
    out[o] = table[k * 3];
    out[o + 1] = table[k * 3 + 1];
    out[o + 2] = table[k * 3 + 2];
    out[o + 3] = 255;
  }
  return out;
}

/** Where a picture sits in a box at its aspect ratio (letterboxed): its offset and scale. */
export function fit(boxW: number, boxH: number, picW: number, picH: number): { x: number; y: number; k: number } {
  const k = Math.min(boxW / picW, boxH / picH);
  return { x: (boxW - picW * k) / 2, y: (boxH - picH * k) / 2, k };
}

/** The pixel under a point of the box (null outside the picture). */
export function pixelAt(boxW: number, boxH: number, pic: Pick<DepthPicture, 'width' | 'height'>, px: number, py: number): { x: number; y: number } | null {
  const f = fit(boxW, boxH, pic.width, pic.height);
  const x = Math.floor((px - f.x) / f.k);
  const y = Math.floor((py - f.y) / f.k);
  if (x < 0 || y < 0 || x >= pic.width || y >= pic.height) return null;
  return { x, y };
}

/** Whether a depth picture can be drawn over its camera: the same aspect ratio within 2 %. */
export function overlayFits(depth: { width: number | null; height: number | null }, camera: { width: number | null; height: number | null }): boolean {
  if (!depth.width || !depth.height || !camera.width || !camera.height) return false;
  return Math.abs(depth.width / depth.height - camera.width / camera.height) <= 0.02 * (camera.width / camera.height);
}
