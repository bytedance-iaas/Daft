import { describe, expect, it } from 'vitest';
import { colorize, decodePng16, DEFAULT_DEPTH_VIEW, encodePng16, fit, gradient, lut, overlayFits, pixelAt } from './vizDepth';

// a 16 × 12 depth picture (millimetres, a hole top left) as the Daemon writes it (Sub filter) and as PIL
// writes it with every row filter (Sub, Up, Average, Paeth): design doc 21 §5.2
const W = 16;
const H = 12;
const OURS =
  'iVBORw0KGgoAAAANSUhEUgAAABAAAAAMEAAAAAAeHL4eAAABY0lEQVR4XkXKO0gVABgG0P97XLtqhHGxItCh1+AD1BAsiETEBpUGC1ECI0FRyKDQKASzKCiKykEJskmCSgJpCB2sJVsqEEKRKBQsLTC8UDSI0lJ45oP4x9Wo5xAXWMhuvVKmGjzsJZfif0jUopkj/MlZXtU77dBpP/Uvf94MabZzjGtY1F3Naa+2+qXhH/DZqMNg9KGAKUwyybQe8rpLdFmt3uY/SFRGU1RhBTPsRydzuaEnHPARHfC0JmxkpKItjuMBqlHMi9zDbJ5U6LsO+ovfejsyvuFCnMJjrOIwv/IDd/GMRtWoSt/yR+djyziuRAdeYB3HeJ+fuJ/nNC6p3kNecCGSt6MIPfEaWTyBR1xWGXs1pRw1e0R3EhXIbIlDuBbvsRPBZ/zNo7rJ88pzu8e8pkVklUUN7sUl7GMXGknWsVzzKnBKk046jexENMQwbqAUu/mGz9nEKq1oxv3qdK43/gJ/1FcvncL9OAAAAABJRU5ErkJggg==';
const PIL =
  'iVBORw0KGgoAAAANSUhEUgAAABAAAAAMEAAAAAAeHL4eAAABLUlEQVR42kWOsUsCYRiH3+f9PhNNzkJpcbIIgmioltbANRyqNZqqpbEa+weCBodoCFySFodwiI7A226y5Q4ig1AqzgSH4iZbWhR/88PveZDRbIktvdKuLuuJaZqU2bY3NrKrdgyQJE+RLC9ExKRxKJAlZAwkfvRQ7/WPT3NpXs2CydgHi+2rPbaPiXLimSQN8aRFm0gqVHHEFV9CJaAjFwypkcejRZuIGKhrAZ+QqZwcSJlrSqzomc7rtO4aMd9m3b5b386qBHQYMKRGg+LooQI4uPiEloCODBiSJM8tbywSEVNlgEuXX0sg55yKJ2l2xJMea0TEzOBQkDs2lLEiKQ08WtImkopUqePqkoRmypCjSYov3SSjPc2xp32dM/v6pEfmQycNMomMqY4j/wHaVWaBEpG/lAAAAABJRU5ErkJggg==';

function expected(): number[] {
  const out: number[] = [];
  for (let y = 0; y < H; y += 1) {
    for (let x = 0; x < W; x += 1) out.push(y < 3 && x < 4 ? 0 : 500 + 37 * x * x + 211 * y + ((x * y * 13) % 97));
  }
  return out;
}

function bytes(b64: string): Uint8Array {
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

describe('vizDepth (design doc 21 §5)', () => {
  it('decodes 16-bit PNGs whatever their row filters', async () => {
    for (const b64 of [OURS, PIL]) {
      const pic = await decodePng16(bytes(b64));
      expect([pic.width, pic.height]).toEqual([W, H]);
      expect(Array.from(pic.data)).toEqual(expected());
    }
  });

  it('round-trips its own encoding, and refuses an 8-bit PNG', async () => {
    const data = Uint16Array.from(expected());
    const png = await encodePng16({ width: W, height: H, data });
    expect(Array.from((await decodePng16(png)).data)).toEqual(expected());
    const eight = bytes(OURS);
    eight[24] = 8; // IHDR bit depth
    await expect(decodePng16(eight)).rejects.toThrow(/16-bit/);
  });

  it('colours between lo and hi, holes transparent, the ends clamped', () => {
    const pic = { width: 3, height: 1, data: Uint16Array.from([0, 500, 2000]) };
    const rgba = colorize(pic, 500, 1500, 'gray');
    expect(Array.from(rgba)).toEqual([0, 0, 0, 0, 0, 0, 0, 255, 255, 255, 255, 255]);
    const turbo = lut('turbo');
    expect(turbo.length).toBe(768);
    // turbo runs blue (near) through green to dark red (far)
    const at = (i: number) => [turbo[i * 3], turbo[i * 3 + 1], turbo[i * 3 + 2]];
    expect(at(38)[2]).toBeGreaterThan(at(38)[0]);
    expect(at(128)[1]).toBeGreaterThan(Math.max(at(128)[0], at(128)[2]));
    expect(at(230)[0]).toBeGreaterThan(at(230)[2]);
    expect(gradient('turbo')).toMatch(/^linear-gradient\(to right, rgb/);
  });

  it('maps a point of the cell to the pixel under it (letterboxed)', () => {
    expect(fit(200, 100, 64, 48)).toEqual({ x: (200 - 64 * (100 / 48)) / 2, y: 0, k: 100 / 48 });
    const pic = { width: 64, height: 48 };
    expect(pixelAt(200, 100, pic, 100, 50)).toEqual({ x: 32, y: 24 });
    expect(pixelAt(200, 100, pic, 2, 50)).toBeNull();
  });

  it('draws over a camera only at the same aspect ratio', () => {
    expect(overlayFits({ width: 640, height: 480 }, { width: 1280, height: 960 })).toBe(true);
    expect(overlayFits({ width: 424, height: 240 }, { width: 640, height: 480 })).toBe(false);
    expect(overlayFits({ width: null, height: null }, { width: 640, height: 480 })).toBe(false);
    expect(DEFAULT_DEPTH_VIEW.cmap).toBe('turbo');
  });
});
