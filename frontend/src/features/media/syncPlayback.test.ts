import { describe, expect, it } from 'vitest';
import { SYNC_CONFIG, SyncController, type MediaLike } from './syncPlayback';

/** jsdom has no media: an element with just what the controller reads, events dispatched at once. */
class FakeMedia extends EventTarget implements MediaLike {
  readyState = 0;
  duration = Number.NaN;
  paused = true;
  ended = false;
  preload = 'metadata';
  ranges: [number, number][] = [];
  plays = 0;
  refusePlay = false;
  private t = 0;

  get buffered() {
    const r = this.ranges;
    return { length: r.length, start: (i: number) => r[i][0], end: (i: number) => r[i][1] };
  }

  get currentTime() {
    return this.t;
  }

  set currentTime(v: number) {
    this.t = v;
    if (this.readyState >= 1) {
      this.fire('seeking');
      this.fire('seeked');
    }
  }

  play() {
    this.plays += 1;
    if (this.refusePlay) return Promise.reject(Object.assign(new Error('not allowed'), { name: 'NotAllowedError' }));
    if (this.paused) {
      this.paused = false;
      this.fire('play');
      this.fire('playing');
    }
    return Promise.resolve();
  }

  pause() {
    if (!this.paused) {
      this.paused = true;
      this.fire('pause');
    }
  }

  fire(type: string) {
    this.dispatchEvent(new Event(type));
  }

  /** Metadata arrives; a v3 fragment starts at `from`. */
  load(duration: number, from = 0) {
    this.duration = duration;
    this.readyState = 1;
    this.t = from;
    this.fire('loadedmetadata');
  }

  /** Data from `from` to `to` (file time) is buffered and playable. */
  buffer(from: number, to: number) {
    this.ranges = [[from, to]];
    this.readyState = 4;
    this.fire('progress');
    this.fire('canplay');
  }

  advance(dt: number) {
    if (!this.paused) this.t += dt;
  }

  userPause() {
    this.paused = true;
    this.fire('pause');
  }

  userPlay() {
    this.paused = false;
    this.fire('play');
  }

  userSeek(t: number) {
    this.t = t;
    this.fire('seeking');
    this.fire('seeked');
  }
}

const noTimer = () => () => undefined;

function setup(windows: { from?: number; to?: number }[] = [{}, {}, {}]) {
  const c = new SyncController(SYNC_CONFIG, noTimer);
  const els = windows.map(() => new FakeMedia());
  const ids = windows.map((_, i) => `cam${i}`);
  const statuses: string[] = [];
  c.subscribe(() => statuses.push(c.getStatus()));
  const attach = (i: number) => c.attach(ids[i], els[i], windows[i]);
  /** Every camera loaded (metadata) and buffered generously around its window. */
  const ready = () =>
    els.forEach((el, i) => {
      const from = windows[i].from ?? 0;
      if (el.readyState < 1) el.load(60, from);
      el.buffer(from, from + 30);
    });
  const rel = (i: number) => els[i].currentTime - (windows[i].from ?? 0);
  return { c, els, ids, statuses, attach, ready, rel };
}

describe('synced playback of the cameras (07 §9, F6.2)', () => {
  it('never plays on its own: attaching, loading and buffering start nothing', () => {
    const { c, els, attach, ready } = setup();
    els.forEach((_, i) => attach(i));
    ready();
    c.tick();
    expect(c.getStatus()).toBe('idle');
    expect(els.every((el) => el.paused && el.plays === 0)).toBe(true);
  });

  it('同时播放 waits until every camera is attached and buffered, then starts them together at the same moment', () => {
    const { c, els, ids, attach, ready, rel, statuses } = setup([{ from: 28, to: 42 }, { from: 0, to: 14 }, {}]);
    attach(0);
    c.start(ids);
    expect(c.getStatus()).toBe('loading');
    expect(els[0].preload).toBe('auto');
    attach(1);
    attach(2);
    // metadata only: still waiting
    els.forEach((el, i) => el.load(60, [28, 0, 0][i]));
    c.tick();
    expect(els.some((el) => el.plays > 0)).toBe(false);
    // two cameras buffered, the third only 1 s ahead: still waiting
    els[0].buffer(28, 42);
    els[1].buffer(0, 14);
    els[2].buffer(0, 1);
    c.tick();
    expect(c.getStatus()).toBe('loading');
    expect(els.some((el) => el.plays > 0)).toBe(false);
    ready();
    expect(c.getStatus()).toBe('playing');
    expect(els.every((el) => !el.paused && el.plays === 1)).toBe(true);
    expect([rel(0), rel(1), rel(2)]).toEqual([0, 0, 0]);
    expect(els[0].currentTime).toBe(28);          // v3: episode time is relative to from_ts
    expect(statuses).toEqual(['loading', 'playing']);
  });

  it('one camera running short pauses all of them (缓冲中), and they go on together once all have data', () => {
    const { c, els, ids, attach, ready, rel } = setup();
    els.forEach((_, i) => attach(i));
    c.start(ids);
    ready();
    els.forEach((el) => el.advance(3));
    els[1].readyState = 2;
    els[1].fire('waiting');
    expect(c.getStatus()).toBe('buffering');
    expect(els.every((el) => el.paused)).toBe(true);
    els[1].buffer(0, 30);
    expect(c.getStatus()).toBe('playing');
    expect(els.every((el) => !el.paused)).toBe(true);
    expect(new Set([rel(0), rel(1), rel(2)]).size).toBe(1);
  });

  it('too little buffered ahead counts as running short; a spurious stalled with data does not', () => {
    const { c, els, ids, attach, ready } = setup();
    els.forEach((_, i) => attach(i));
    c.start(ids);
    ready();
    els[2].fire('stalled');
    expect(c.getStatus()).toBe('playing');
    els[2].ranges = [[0, 0.3]];
    c.tick();
    expect(c.getStatus()).toBe('buffering');
    expect(els.every((el) => el.paused)).toBe(true);
  });

  it('drift beyond 0.2 s is aligned to the first camera', () => {
    const { c, els, ids, attach, ready, rel } = setup();
    els.forEach((_, i) => attach(i));
    c.start(ids);
    ready();
    els[0].advance(5);
    els[1].advance(5.1);
    els[2].advance(5.6);
    c.tick();
    expect(rel(1)).toBeCloseTo(5.1);             // within 0.2 s: left alone
    expect(rel(2)).toBeCloseTo(5);                // 0.6 s off: back in line
    expect(c.getStatus()).toBe('playing');
  });

  it("pausing, playing or seeking one camera with its own controls does the same to the others", () => {
    const { c, els, ids, attach, ready, rel } = setup([{ from: 10 }, {}, {}]);
    els.forEach((_, i) => attach(i));
    c.start(ids);
    ready();
    els.forEach((el) => el.advance(4));
    els[1].userPause();
    expect(c.getStatus()).toBe('paused');
    expect(els.every((el) => el.paused)).toBe(true);
    // seeking while paused: the others follow and stay paused
    els[2].userSeek(7.5);
    expect([rel(0), rel(1), rel(2)]).toEqual([7.5, 7.5, 7.5]);
    expect(els[0].currentTime).toBe(17.5);
    expect(els.every((el) => el.paused)).toBe(true);
    // playing one: all of them go, from its moment
    els[0].userPlay();
    expect(c.getStatus()).toBe('playing');
    expect(els.every((el) => !el.paused)).toBe(true);
    // seeking while playing: everyone waits until they have data there, then goes on
    els[1].ranges = [[0, 12]];
    els[1].userSeek(20);
    expect(c.getStatus()).toBe('buffering');
    expect(els.every((el) => el.paused)).toBe(true);
    expect(rel(0)).toBe(20);
    els[1].buffer(0, 40);
    expect(c.getStatus()).toBe('playing');
  });

  it('the end of the episode stops all of them; playing again starts from the beginning', () => {
    const { c, els, ids, attach, ready, rel } = setup([{ from: 28, to: 42 }, { from: 0, to: 14 }]);
    els.forEach((_, i) => attach(i));
    c.start(ids);
    ready();
    els.forEach((el) => el.advance(13.95));
    c.tick();
    expect(c.getStatus()).toBe('ended');
    expect(els.every((el) => el.paused)).toBe(true);
    els[1].userPlay();
    expect(c.getStatus()).toBe('playing');
    expect([rel(0), rel(1)]).toEqual([0, 0]);
  });

  it('停止同步 pauses everything and leaves the cameras on their own', () => {
    const { c, els, ids, attach, ready } = setup();
    els.forEach((_, i) => attach(i));
    c.start(ids);
    ready();
    c.stop();
    expect(c.getStatus()).toBe('idle');
    expect(els.every((el) => el.paused)).toBe(true);
    els[0].userPlay();
    expect(els[1].paused && els[2].paused).toBe(true);
    expect(c.getStatus()).toBe('idle');
  });

  it('a camera whose video cannot be loaded is left out; the others play', () => {
    const { c, els, ids, attach, ready } = setup();
    attach(0);
    attach(1);
    c.start(ids);
    ready();
    expect(c.getStatus()).toBe('loading');         // cam2 never came
    c.fail(ids[2]);
    expect(c.getStatus()).toBe('playing');
    expect(els[0].paused || els[1].paused).toBe(false);
  });

  it('a re-signed camera (new source) holds the others until it is back at the same moment', () => {
    const { c, els, ids, attach, ready, rel } = setup();
    els.forEach((_, i) => attach(i));
    c.start(ids);
    ready();
    els.forEach((el) => el.advance(6));
    els[0].fire('error');
    expect(c.getStatus()).toBe('buffering');
    els[0].fire('emptied');
    els[0].ranges = [];
    els[0].readyState = 0;
    els[0].load(60);                               // the new URL starts at 0 ...
    expect(rel(0)).toBe(6);                        // ... and is put back where the others are
    els[0].buffer(0, 30);
    expect(c.getStatus()).toBe('playing');
  });

  it('a refused play() (autoplay policy) leaves them paused instead of waiting forever', async () => {
    const { c, els, ids, attach, ready } = setup([{}, {}]);
    els.forEach((_, i) => attach(i));
    els[1].refusePlay = true;
    c.start(ids);
    ready();
    await Promise.resolve();
    await Promise.resolve();
    expect(c.getStatus()).toBe('paused');
    expect(els.every((el) => el.paused)).toBe(true);
  });

  it('dispose drops the listeners', () => {
    const { c, els, ids, attach, ready } = setup([{}, {}]);
    els.forEach((_, i) => attach(i));
    c.start(ids);
    ready();
    c.dispose();
    els[0].userPause();
    expect(els[1].paused).toBe(true);
    expect(c.getStatus()).toBe('idle');
  });
});
