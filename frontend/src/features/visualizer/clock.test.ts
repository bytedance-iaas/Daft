import { describe, expect, it } from 'vitest';
import { CLOCK_CONFIG, PlayerClock, type ClockEnv, type MediaLike } from './clock';

/** A <video> stand-in: plays at its rate when the test advances wall time. */
class FakeMedia extends EventTarget implements MediaLike {
  readyState = 4;
  duration = 10;
  paused = true;
  seeking = false;
  error: unknown = null;
  playbackRate = 1;
  /** the most data it gets to (2: still loading) */
  maxReady = 4;
  seeks: number[] = [];
  private time = 0;

  get currentTime(): number {
    return this.time;
  }

  set currentTime(v: number) {
    this.time = v;
    this.seeks.push(v);
    this.seeking = true;
    this.readyState = 1;
  }

  /** The seek finishes and data is there again. */
  settle(): void {
    this.seeking = false;
    this.readyState = this.maxReady;
    this.dispatchEvent(new Event('seeked'));
  }

  play(): void {
    if (!this.paused) return;
    this.paused = false;
    this.dispatchEvent(new Event('playing'));
  }

  pause(): void {
    this.paused = true;
  }

  /** One frame of wall time: a pending seek finishes (as a browser does, a moment later), or it plays on. */
  advance(dt: number): void {
    if (this.seeking) {
      this.settle();
      return;
    }
    if (!this.paused) this.time = Math.min(this.duration, this.time + dt * this.playbackRate);
  }
}

/** A video with bytes up to `bufferedTo` (media seconds), as a slow network brings them. */
class BufferedMedia extends FakeMedia {
  bufferedTo = 0;
  get buffered() {
    const end = this.bufferedTo;
    return { length: end > 0 ? 1 : 0, start: () => 0, end: () => end };
  }
}

class FakeLoop implements ClockEnv {
  time = 0;
  now(): number {
    return this.time;
  }
  private cbs = new Map<number, (now: number) => void>();
  private next = 1;
  raf(cb: (now: number) => void): number {
    const id = this.next++;
    this.cbs.set(id, cb);
    return id;
  }
  caf(id: number): void {
    this.cbs.delete(id);
  }
  /** One frame `ms` later; the media advance with the wall clock first. */
  frame(ms: number, media: FakeMedia[] = []): void {
    this.time += ms;
    for (const m of media) m.advance(ms / 1000);
    const cbs = [...this.cbs.values()];
    this.cbs.clear();
    for (const cb of cbs) cb(this.time);
  }
  run(ms: number, frames: number, media: FakeMedia[] = []): void {
    for (let i = 0; i < frames; i += 1) this.frame(ms, media);
  }
}

describe('PlayerClock', () => {
  it('waits until a video holds about 5 s before it starts, and holds again before it runs dry (2026-10-05)', () => {
    const loop = new FakeLoop();
    const clock = new PlayerClock(20, loop);
    const v = new BufferedMedia();
    v.duration = 20;
    v.bufferedTo = 1; // a slow network: 1 s so far
    clock.attach('v', v, { offset: 0, from: 0, end: null });
    clock.play();
    loop.run(16, 30, [v]);
    expect(clock.getSnapshot()).toMatchObject({ waiting: true, t: 0 });
    v.bufferedTo = CLOCK_CONFIG.readyAheadS + 0.5;
    loop.run(16, 3, [v]);
    expect(clock.getSnapshot().waiting).toBe(false);
    loop.run(16, 60, [v]);
    expect(clock.getSnapshot().t).toBeGreaterThan(0.8);
    // no more bytes come, and the browser says it may stall: the clock holds a little before they run out
    v.maxReady = 3;
    v.readyState = 3;
    loop.run(16, 400, [v]);
    expect(clock.getSnapshot().waiting).toBe(true);
    const at = clock.getSnapshot().t;
    expect(at).toBeGreaterThan(v.bufferedTo - CLOCK_CONFIG.lowWaterS - 0.2);
    expect(at).toBeLessThanOrEqual(v.bufferedTo);
  });

  it('a video buffered to its end does not wait for more; waited maxWaitS, the clock starts on what there is', () => {
    const loop = new FakeLoop();
    const short = new PlayerClock(3, loop);
    const end = new BufferedMedia();
    end.duration = 3;
    end.bufferedTo = 3;
    short.attach('v', end, { offset: 0, from: 0, end: null });
    short.play();
    loop.run(16, 3, [end]);
    expect(short.getSnapshot().waiting).toBe(false);

    const clock = new PlayerClock(20, loop);
    const v = new BufferedMedia();
    v.duration = 20;
    v.bufferedTo = 1;
    clock.attach('v', v, { offset: 0, from: 0, end: null });
    clock.play();
    loop.run(100, CLOCK_CONFIG.maxWaitS * 10 - 5, [v]);
    expect(clock.getSnapshot().waiting).toBe(true);
    loop.run(100, 10, [v]);
    expect(clock.getSnapshot().waiting).toBe(false);
  });

  it('waits for the frame-pack and sample-pack cells too, and holds before one runs dry', () => {
    const loop = new FakeLoop();
    const clock = new PlayerClock(20, loop);
    let ahead = 1;
    const detach = clock.attachSource('frames', { ahead: () => ahead });
    clock.play();
    loop.run(16, 10);
    expect(clock.getSnapshot()).toMatchObject({ waiting: true, t: 0 });
    ahead = CLOCK_CONFIG.readyAheadS + 1;
    loop.run(16, 2);
    expect(clock.getSnapshot().waiting).toBe(false);
    loop.run(16, 30);
    expect(clock.getSnapshot().t).toBeGreaterThan(0.3);
    ahead = 0.2; // the fetches fell behind
    loop.run(16, 2);
    expect(clock.getSnapshot().waiting).toBe(true);
    // gone, it is not waited for
    detach();
    loop.run(16, 2);
    expect(clock.getSnapshot().waiting).toBe(false);
  });

  it('runs on the wall clock without videos, pauses at the end, loops when asked', () => {
    const loop = new FakeLoop();
    const clock = new PlayerClock(1, loop);
    const seen: number[] = [];
    clock.subscribe(() => seen.push(clock.getSnapshot().t));
    clock.play();
    expect(clock.getSnapshot()).toMatchObject({ playing: true, waiting: false });
    loop.run(100, 6); // the first frame only starts the count
    expect(clock.getSnapshot().t).toBeCloseTo(0.5);
    clock.setSpeed(2);
    loop.run(100, 4);
    expect(clock.getSnapshot()).toMatchObject({ t: 1, playing: false });
    expect(seen.length).toBeGreaterThan(5);
    clock.setLoop(true);
    clock.play(); // from the end: starts over
    expect(clock.getSnapshot().t).toBe(0);
    loop.run(100, 7);
    expect(clock.getSnapshot().playing).toBe(true);
    expect(clock.getSnapshot().t).toBeLessThan(0.5); // wrapped around
  });

  it('caps a long gap between frames (a background tab)', () => {
    const loop = new FakeLoop();
    const clock = new PlayerClock(100, loop);
    clock.play();
    loop.frame(16);
    loop.frame(5000);
    expect(clock.getSnapshot().t).toBeCloseTo(CLOCK_CONFIG.maxStepS);
  });

  it('starts the videos together and goes on from where they are', () => {
    const loop = new FakeLoop();
    const clock = new PlayerClock(10, loop);
    const a = new FakeMedia();
    const b = new FakeMedia();
    b.readyState = 2; // still loading
    b.maxReady = 2;
    clock.attach('a', a, { offset: 0, from: 0, end: null });
    clock.attach('b', b, { offset: 0, from: 0, end: null });
    clock.play();
    expect(clock.getSnapshot().waiting).toBe(true);
    loop.run(16, 3, [a, b]);
    expect(clock.getSnapshot()).toMatchObject({ waiting: true, t: 0 });
    expect(a.paused && b.paused).toBe(true); // nobody runs ahead while one is loading
    b.maxReady = 4;
    b.readyState = 4;
    b.dispatchEvent(new Event('canplay'));
    expect(a.paused || b.paused).toBe(false);
    loop.run(16, 1, [a, b]);
    expect(clock.getSnapshot().waiting).toBe(false);
    loop.run(16, 60, [a, b]);
    const t = clock.getSnapshot().t;
    expect(t).toBeGreaterThan(0.9);
    for (const d of Object.values(clock.drift())) expect(Math.abs(d)).toBeLessThan(1 / 30);
  });

  it('nudges a drifting video and seeks one that is far off', () => {
    const loop = new FakeLoop();
    const clock = new PlayerClock(10, loop);
    const v = new FakeMedia();
    clock.attach('v', v, { offset: 0, from: 0, end: null });
    clock.play();
    loop.run(16, 3, [v]);
    expect(clock.getSnapshot().waiting).toBe(false);
    v.playbackRate = 1;
    // the video runs 5 % fast: the clock slows it down and keeps it within a frame
    const fast = v.advance.bind(v);
    v.advance = (dt) => fast(dt * 1.05);
    loop.run(16, 240, [v]);
    expect(v.playbackRate).toBeLessThan(1);
    expect(Math.abs(clock.drift().v)).toBeLessThan(1 / 30);
    // something moved it half a second ahead: seek back and hold until it is there again
    v.advance = fast;
    (v as unknown as { time: number }).time += 0.5;
    loop.frame(16, [v]);
    expect(clock.getSnapshot().waiting).toBe(true);
    expect(v.seeking).toBe(true);
    v.settle();
    loop.run(16, 3, [v]);
    expect(clock.getSnapshot().waiting).toBe(false);
    expect(Math.abs(clock.drift().v)).toBeLessThan(1 / 30);
  });

  it('holds everything while one video runs short of data', () => {
    const loop = new FakeLoop();
    const clock = new PlayerClock(10, loop);
    const a = new FakeMedia();
    const b = new FakeMedia();
    clock.attach('a', a, { offset: 0, from: 0, end: null });
    clock.attach('b', b, { offset: 0, from: 0, end: null });
    clock.play();
    loop.run(16, 30, [a, b]);
    const before = clock.getSnapshot().t;
    b.readyState = 2;
    b.dispatchEvent(new Event('waiting'));
    expect(clock.getSnapshot().waiting).toBe(true);
    expect(a.paused).toBe(true);
    loop.run(16, 30, [a, b]);
    expect(clock.getSnapshot().t).toBeCloseTo(before, 2);
    b.readyState = 4;
    b.dispatchEvent(new Event('canplay'));
    loop.run(16, 30, [a, b]);
    expect(clock.getSnapshot().t).toBeGreaterThan(before + 0.3);
  });

  it('puts paused videos exactly on the frame, with their offset and window', () => {
    const loop = new FakeLoop();
    const clock = new PlayerClock(10, loop);
    const v3 = new FakeMedia(); // a LeRobot v3 file: this episode is 12–22 s of it
    v3.duration = 60;
    const late = new FakeMedia(); // an mcap camera whose first frame comes 1 s in
    clock.attach('v3', v3, { offset: 0, from: 12, end: 22 });
    clock.attach('late', late, { offset: 1, from: 0, end: null });
    v3.settle();
    late.settle();
    clock.seek(2.5);
    expect(v3.currentTime).toBeCloseTo(14.5 + CLOCK_CONFIG.frameEpsS, 6);
    expect(late.currentTime).toBeCloseTo(1.5 + CLOCK_CONFIG.frameEpsS, 6);
    v3.settle();
    late.settle();
    clock.seek(0.4); // before the late camera starts: parked at its first frame
    late.settle();
    expect(late.currentTime).toBe(0);
    clock.play();
    v3.settle();
    loop.run(16, 3, [v3, late]);
    expect(late.paused).toBe(true);
    expect(v3.paused).toBe(false);
  });

  it('leaves a video that failed to load out of the sync', () => {
    const loop = new FakeLoop();
    const clock = new PlayerClock(10, loop);
    const ok = new FakeMedia();
    const bad = new FakeMedia();
    bad.readyState = 0;
    bad.error = { code: 4 };
    clock.attach('ok', ok, { offset: 0, from: 0, end: null });
    clock.attach('bad', bad, { offset: 0, from: 0, end: null });
    clock.play();
    loop.run(16, 30, [ok, bad]);
    expect(clock.getSnapshot().t).toBeGreaterThan(0.3);
  });

  it('detaches and disposes cleanly', () => {
    const loop = new FakeLoop();
    const clock = new PlayerClock(10, loop);
    const v = new FakeMedia();
    const detach = clock.attach('v', v, { offset: 0, from: 0, end: null });
    clock.play();
    loop.run(16, 3, [v]);
    detach();
    expect(clock.drift()).toEqual({});
    clock.dispose();
    loop.run(16, 3, [v]);
    expect(v.paused).toBe(false); // detached before the dispose: no longer the clock's
  });
});
