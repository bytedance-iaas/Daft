// The player's clock (design doc 18 §4.4, D64): episode time is the only clock. It advances with
// the wall clock while playing; every <video> cell is attached with where its own time sits on it
// (`media = t - offset + from`) and is kept there - small drift by nudging its playbackRate, large
// drift by a seek. Starting, seeking while playing, and any video running short of data hold the
// clock (`waiting`): every video is put on the clock's time, and once each has data they start
// together; the clock goes on from where they actually are. Paused, every video sits exactly on the
// clock's frame. Frame-pack and curve cells just read `t`. A video that failed to load is left out.
//
// It knows media elements only through MediaLike, so tests drive fake ones and a fake frame loop.

/** The part of HTMLMediaElement the clock uses. */
export interface MediaLike extends EventTarget {
  readonly readyState: number;
  currentTime: number;
  readonly duration: number;
  readonly paused: boolean;
  readonly seeking: boolean;
  readonly error?: unknown;
  playbackRate: number;
  play(): Promise<void> | void;
  pause(): void;
}

export interface ClockSnapshot {
  /** episode seconds */
  t: number;
  playing: boolean;
  /** playing, but held until every attached video plays */
  waiting: boolean;
  speed: number;
  loop: boolean;
  duration: number;
}

/** Where a video's time sits on the clock: media = t - offset + from, until `end` (media seconds). */
export interface MediaBinding {
  offset: number;
  from: number;
  end: number | null;
}

export interface ClockEnv {
  raf(cb: (now: number) => void): number;
  caf(id: number): void;
  /** the frame loop's time base (ms) */
  now(): number;
}

const browserEnv: ClockEnv = {
  raf: (cb) => requestAnimationFrame(cb),
  caf: (id) => cancelAnimationFrame(id),
  now: () => performance.now(),
};

export const CLOCK_CONFIG = {
  /** Drift (s) above which a playing video is seeked (and the clock held) instead of nudged. */
  hardDriftS: 0.3,
  /** Drift (s) under which the rate is left alone. */
  softDriftS: 0.008,
  /** Rate nudge per second of drift, and its bound (a fraction of the speed). */
  nudgeGain: 3,
  maxNudge: 0.15,
  /** A frame-loop gap longer than this (s; a background tab) does not move the clock further. */
  maxStepS: 0.25,
  /** Paused videos land this far (s) inside the frame the clock shows. */
  frameEpsS: 0.001,
};

const HAVE_METADATA = 1;
const HAVE_FUTURE_DATA = 3;

interface Attached {
  el: MediaLike;
  b: MediaBinding;
  off: () => void;
}

function start(el: MediaLike): void {
  const p = el.play();
  if (p && typeof (p as Promise<void>).catch === 'function') (p as Promise<void>).catch(() => undefined);
}

export class PlayerClock {
  private snap: ClockSnapshot;
  private readonly listeners = new Set<() => void>();
  private readonly media = new Map<string, Attached>();
  private raf: number | null = null;
  private last: number | null = null;
  private disposed = false;

  constructor(
    duration: number,
    private readonly env: ClockEnv = browserEnv,
    private readonly cfg = CLOCK_CONFIG,
  ) {
    this.snap = { t: 0, playing: false, waiting: false, speed: 1, loop: false, duration: Math.max(0, duration) };
  }

  // -- the store (useSyncExternalStore)
  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };

  getSnapshot = (): ClockSnapshot => this.snap;

  private set(patch: Partial<ClockSnapshot>): void {
    const next = { ...this.snap, ...patch };
    const keys = Object.keys(next) as (keyof ClockSnapshot)[];
    if (keys.every((k) => next[k] === this.snap[k])) return;
    this.snap = next;
    for (const fn of [...this.listeners]) fn();
  }

  // -- controls
  play(): void {
    if (this.disposed || this.snap.duration <= 0 || this.snap.playing) return;
    const atEnd = this.snap.t >= this.snap.duration - 1e-6;
    this.set({ playing: true, waiting: this.liveMedia().length > 0, t: atEnd ? 0 : this.snap.t });
    this.last = null;
    this.syncMedia();
    this.loop();
  }

  pause(): void {
    this.set({ playing: false, waiting: false });
    this.stopLoop();
    this.syncMedia();
  }

  toggle(): void {
    if (this.snap.playing) this.pause();
    else this.play();
  }

  seek(t: number): void {
    const clamped = Math.max(0, Math.min(this.snap.duration, Number.isFinite(t) ? t : 0));
    this.set({ t: clamped });
    if (this.snap.playing) this.hold();
    this.syncMedia();
  }

  setSpeed(speed: number): void {
    this.set({ speed: speed > 0 ? speed : 1 });
    this.syncMedia();
  }

  setLoop(loop: boolean): void {
    this.set({ loop });
  }

  setDuration(duration: number): void {
    const d = Math.max(0, duration);
    this.set({ duration: d, t: Math.min(this.snap.t, d) });
  }

  /** A video cell's element; returns the detach function. */
  attach(id: string, el: MediaLike, binding: MediaBinding): () => void {
    this.detach(id);
    const nudge = () => {
      if (!this.disposed) this.syncMedia();
    };
    const events = ['loadedmetadata', 'canplay', 'canplaythrough', 'playing', 'waiting', 'seeked', 'stalled', 'error'];
    for (const ev of events) el.addEventListener(ev, nudge);
    const entry: Attached = { el, b: binding, off: () => events.forEach((ev) => el.removeEventListener(ev, nudge)) };
    this.media.set(id, entry);
    if (this.snap.playing) this.hold();
    this.syncMedia();
    return () => this.detach(id, entry);
  }

  private detach(id: string, only?: Attached): void {
    const m = this.media.get(id);
    if (!m || (only && m !== only)) return;
    m.off();
    this.media.delete(id);
    this.syncMedia();
  }

  dispose(): void {
    this.disposed = true;
    this.stopLoop();
    for (const m of this.media.values()) {
      m.off();
      m.el.pause();
    }
    this.media.clear();
    this.listeners.clear();
  }

/**
   * The clock's time now: `t` moves once per frame of the loop, so between frames it is extrapolated
   * from the last one (what a video's continuously moving currentTime should be compared with).
   */
  timeNow(): number {
    const s = this.snap;
    if (!s.playing || s.waiting || this.last === null) return s.t;
    return Math.min(s.duration, s.t + (Math.max(0, this.env.now() - this.last) / 1000) * s.speed);
  }

  /** How far (s) each attached video is from the clock now (positive: ahead); for the drift check. */
  drift(): Record<string, number> {
    const out: Record<string, number> = {};
    const t = this.timeNow();
    for (const [id, m] of this.media) {
      const target = this.target(m);
      if (target !== null && m.el.readyState >= HAVE_METADATA) out[id] = m.el.currentTime - (t - m.b.offset + m.b.from);
    }
    return out;
  }

  // -- the frame loop
  private loop(): void {
    if (this.raf !== null || this.disposed || !this.snap.playing) return;
    this.raf = this.env.raf(this.tick);
  }

  private stopLoop(): void {
    if (this.raf !== null) this.env.caf(this.raf);
    this.raf = null;
    this.last = null;
  }

  private tick = (now: number): void => {
    this.raf = null;
    if (this.disposed || !this.snap.playing) {
      this.last = null;
      return;
    }
    const dt = this.last === null ? 0 : Math.min(this.cfg.maxStepS, Math.max(0, (now - this.last) / 1000));
    this.last = now;
    if (!this.snap.waiting && dt > 0) {
      const t = this.snap.t + dt * this.snap.speed;
      if (t >= this.snap.duration) {
        if (this.snap.loop) {
          this.set({ t: 0 });
          this.hold();
        } else {
          this.set({ t: this.snap.duration });
          this.pause();
          return;
        }
      } else {
        this.set({ t });
      }
    }
    this.syncMedia();
    this.loop();
  };

  // -- keeping the videos on the clock
  /** A video's time for the clock's t, or null when the clock is outside the video. */
  private target(m: Attached): number | null {
    const target = this.snap.t - m.b.offset + m.b.from;
    const end = m.b.end ?? (Number.isFinite(m.el.duration) ? m.el.duration : null);
    if (target < m.b.from - 1e-3 || (end !== null && target > end + 1e-3)) return null;
    return target;
  }

  /** Attached videos the clock is inside of and that did not fail. */
  private liveMedia(): Attached[] {
    return [...this.media.values()].filter((m) => !m.el.error && this.target(m) !== null);
  }

  /** Holds the clock until every video is back on it and plays (after a seek, a loop, a new cell). */
  private hold(): void {
    if (!this.snap.playing) return;
    const live = this.liveMedia();
    for (const m of live) if (!m.el.paused) m.el.pause();
    this.set({ waiting: live.length > 0 });
  }

  /** Puts a video exactly where the clock is (outside its range: parked at the nearer end). */
  private align(m: Attached, inside: boolean): void {
    const el = m.el;
    if (el.readyState < HAVE_METADATA || el.seeking) return; // 'loadedmetadata' / 'seeked' come back here
    const raw = this.snap.t - m.b.offset + m.b.from;
    const end = m.b.end ?? (Number.isFinite(el.duration) ? el.duration : raw);
    const target = Math.max(m.b.from, Math.min(end, raw));
    const want = inside ? target + this.cfg.frameEpsS : target;
    if (Math.abs(el.currentTime - want) > this.cfg.frameEpsS / 2) el.currentTime = want;
  }

  private setRate(m: Attached, diff: number): void {
    const off = Math.abs(diff) < this.cfg.softDriftS ? 0 : Math.max(-this.cfg.maxNudge, Math.min(this.cfg.maxNudge, diff * this.cfg.nudgeGain));
    const rate = this.snap.speed * (1 - off);
    if (Math.abs(m.el.playbackRate - rate) > 0.004) m.el.playbackRate = rate;
  }

  private syncMedia(): void {
    if (this.disposed) return;
    const live = this.liveMedia();
    for (const m of this.media.values()) {
      if (live.includes(m)) continue;
      if (!m.el.paused) m.el.pause();
      if (!m.el.error) this.align(m, false);
    }
    if (!this.snap.playing) {
      for (const m of live) {
        if (!m.el.paused) m.el.pause();
        this.align(m, true);
      }
      return;
    }
    const unready = (m: Attached) => m.el.readyState < HAVE_FUTURE_DATA || m.el.seeking;
    if (!this.snap.waiting) {
      // running: each video follows the clock
      let held = false;
      for (const m of live) {
        if (unready(m)) {
          held = true;
          continue;
        }
        const diff = m.el.currentTime - (this.target(m) as number);
        if (Math.abs(diff) > this.cfg.hardDriftS) {
          held = true;
          continue;
        }
        this.setRate(m, diff);
        if (m.el.paused) start(m.el); // a camera whose range the clock just entered
      }
      if (held) {
        this.hold();
        this.syncMedia();
      }
      return;
    }
    // waiting: every video on the clock's time with data, then all start, then the clock goes on
    if (live.some(unready)) {
      for (const m of live) {
        if (!m.el.paused) m.el.pause();
        this.align(m, false);
      }
      return;
    }
    const off = live.filter((m) => Math.abs(m.el.currentTime - (this.target(m) as number)) > this.cfg.hardDriftS / 2);
    if (off.length) {
      for (const m of off) this.align(m, false);
      return;
    }
    const stopped = live.filter((m) => m.el.paused);
    if (stopped.length) {
      for (const m of live) {
        this.setRate(m, 0);
        if (m.el.paused) start(m.el);
      }
      return; // their 'playing' events bring us back
    }
    // every one plays: go on from where they are (the median, so one early starter does not lead)
    const times = live.map((m) => m.el.currentTime + m.b.offset - m.b.from).sort((a, b) => a - b);
    const t = times.length ? times[(times.length - 1) >> 1] : this.snap.t;
    this.last = null;
    this.set({ waiting: false, t: Math.max(0, Math.min(this.snap.duration, t)) });
  }
}
