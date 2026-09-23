// Synchronized playback of one episode's camera videos (07 §9; F6.2, the requester's item 14).
//
// Nothing plays until the user asks (「同时播放」). Then every video is buffered, set to the same
// moment of the episode - times are relative to each video's from_ts, because a LeRobot v3 file
// holds several episodes (`#t=from,to`) - and started together. While they play, a video that runs
// short (waiting, stalled, or too little buffered ahead) pauses all of them (「缓冲中…」) until
// every one has enough data again; drift beyond 0.2 s is corrected. Pausing, playing or seeking
// one video with its own controls does the same to the others. Leaving synced mode pauses them.
//
// The controller only knows media elements (an interface, so tests drive fake ones); the React
// side (SyncedVideos) attaches each camera's <video> as it mounts.

/** The part of HTMLMediaElement the controller uses. */
export interface MediaLike extends EventTarget {
  readonly readyState: number;
  readonly buffered: { readonly length: number; start(index: number): number; end(index: number): number };
  currentTime: number;
  readonly duration: number;
  readonly paused: boolean;
  readonly ended: boolean;
  preload: string;
  play(): Promise<void> | void;
  pause(): void;
}

/**
 * idle: not synced; loading / buffering: waiting until every video has enough data (first start /
 * after one ran short); playing; paused (by the user, all of them); ended (one reached its end).
 */
export type SyncStatus = 'idle' | 'loading' | 'buffering' | 'playing' | 'paused' | 'ended';

export const SYNC_CONFIG = {
  /** Seconds buffered ahead of the playhead a video needs before (re)starting. */
  readyAheadS: 2,
  /** Below this many seconds ahead while playing, everything pauses until all are ready again. */
  lowWaterS: 0.5,
  /** Videos further apart than this (seconds) are aligned again. */
  driftS: 0.2,
  /** How often buffering and drift are checked while synced. */
  tickMs: 250,
  /** Within this many seconds of its end a video counts as finished. */
  endSlackS: 0.15,
};

export type SyncConfig = typeof SYNC_CONFIG;

const HAVE_METADATA = 1;
const HAVE_FUTURE_DATA = 3;
/** Seeks closer than this are not worth doing (and would only restart buffering). */
const SEEK_EPS = 0.05;

type Kind = 'pause' | 'play' | 'seek';

interface Member {
  id: string;
  el: MediaLike | null;
  from: number;
  to: number | null;
  failed: boolean;
  /** Events the controller caused and must not take for the user's. */
  expect: Record<Kind, number>;
  /** False from a new source until it has data: its initial (fragment) seek is not the user's. */
  settled: boolean;
  off: (() => void) | null;
}

export interface MemberWindow {
  from?: number | null;
  to?: number | null;
}

export type Scheduler = (tick: () => void, ms: number) => () => void;

const intervalScheduler: Scheduler = (tick, ms) => {
  const h = setInterval(tick, ms);
  return () => clearInterval(h);
};

export class SyncController {
  private readonly members = new Map<string, Member>();
  private expected: string[] = [];
  private active = false;
  private intent: 'play' | 'pause' = 'pause';
  private status: SyncStatus = 'idle';
  /** The episode-relative time everything is aligned to when (re)starting. */
  private target = 0;
  private cancelTick: (() => void) | null = null;
  private readonly listeners = new Set<() => void>();

  constructor(
    private readonly cfg: SyncConfig = SYNC_CONFIG,
    private readonly schedule: Scheduler = intervalScheduler,
  ) {}

  // -- the React side ----------------------------------------------------------------------

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  readonly getStatus = (): SyncStatus => this.status;

  isActive(): boolean {
    return this.active;
  }

  /** A camera's element, now or again (a re-signed URL keeps the element, a remount does not). */
  attach(id: string, el: MediaLike, window: MemberWindow = {}): void {
    const m = this.member(id);
    if (m.el !== el) {
      m.off?.();
      m.el = el;
      m.settled = el.readyState >= HAVE_FUTURE_DATA;
      m.expect = { pause: 0, play: 0, seek: 0 };
      m.off = this.listen(m, el);
    }
    m.from = window.from ?? 0;
    m.to = window.to ?? null;
    m.failed = false;
    if (this.active) {
      el.preload = 'auto';
      this.pauseEl(m);
      this.seekEl(m, this.target);
      if (this.intent === 'play' && this.status === 'playing') this.hold();
      this.evaluate();
    }
  }

  detach(id: string, el?: MediaLike | null): void {
    const m = this.members.get(id);
    if (!m || (el && m.el !== el)) return;
    m.off?.();
    m.off = null;
    m.el = null;
    if (this.active && this.intent === 'play' && this.status === 'playing' && this.expected.includes(id)) this.hold();
  }

  /** A camera that cannot be played (its URL keeps failing): the others go on without it. */
  fail(id: string): void {
    this.member(id).failed = true;
    this.evaluate();
  }

  /** 「同时播放」: buffer every video in `ids`, align them, then play them together. */
  start(ids: readonly string[]): void {
    this.expected = [...ids];
    for (const id of ids) this.member(id);
    this.active = true;
    this.intent = 'play';
    const lead = this.participants().find((m) => m.el && m.el.readyState >= HAVE_METADATA);
    const at = lead ? this.rel(lead) : 0;
    this.target = lead && at > 0.1 && at < this.length(lead) - 0.5 ? at : 0;
    for (const m of this.participants()) {
      if (!m.el) continue;
      m.el.preload = 'auto';
      this.pauseEl(m);
      this.seekEl(m, this.target);
    }
    this.setStatus('loading');
    this.cancelTick?.();
    this.cancelTick = this.schedule(() => this.evaluate(), this.cfg.tickMs);
    this.evaluate();
  }

  /** 「停止同步」: every video pauses and is on its own again. */
  stop(): void {
    this.cancelTick?.();
    this.cancelTick = null;
    for (const m of this.members.values()) this.pauseEl(m);
    this.active = false;
    this.intent = 'pause';
    this.expected = [];
    this.setStatus('idle');
  }

  dispose(): void {
    this.stop();
    for (const m of this.members.values()) m.off?.();
    this.members.clear();
    this.listeners.clear();
  }

  /** The periodic check (also run on every media event while synced). */
  tick(): void {
    this.evaluate();
  }

  // -- state ----------------------------------------------------------------------------------

  private member(id: string): Member {
    let m = this.members.get(id);
    if (!m) {
      m = { id, el: null, from: 0, to: null, failed: false, expect: { pause: 0, play: 0, seek: 0 }, settled: false, off: null };
      this.members.set(id, m);
    }
    return m;
  }

  private participants(): Member[] {
    return this.expected.map((id) => this.members.get(id)!).filter((m) => m && !m.failed);
  }

  private setStatus(status: SyncStatus): void {
    if (this.status === status) return;
    this.status = status;
    for (const l of [...this.listeners]) l();
  }

  // -- time -----------------------------------------------------------------------------------

  private rel(m: Member): number {
    return m.el ? m.el.currentTime - m.from : 0;
  }

  /** The episode's length in this video: to_ts - from_ts, else what the file says. */
  private length(m: Member): number {
    if (m.to !== null) return Math.max(0, m.to - m.from);
    const d = m.el?.duration ?? NaN;
    return Number.isFinite(d) ? Math.max(0, d - m.from) : Infinity;
  }

  private atEnd(m: Member): boolean {
    return Boolean(m.el) && (m.el!.ended || this.rel(m) >= this.length(m) - this.cfg.endSlackS);
  }

  /** Seconds buffered ahead of the playhead (0 when the playhead is outside every range). */
  private ahead(m: Member): number {
    const el = m.el;
    if (!el) return 0;
    const t = el.currentTime;
    for (let i = 0; i < el.buffered.length; i += 1) {
      if (el.buffered.start(i) <= t + SEEK_EPS && el.buffered.end(i) >= t) return el.buffered.end(i) - t;
    }
    return 0;
  }

  /** Buffered through the end of the episode: nothing more to wait for. */
  private toEnd(m: Member): boolean {
    const len = this.length(m);
    return Number.isFinite(len) && this.rel(m) + this.ahead(m) >= len - this.cfg.endSlackS;
  }

  private ready(m: Member): boolean {
    const el = m.el;
    if (!el || el.readyState < HAVE_FUTURE_DATA) return this.atEnd(m) && Boolean(el);
    return this.ahead(m) >= this.cfg.readyAheadS || this.toEnd(m) || this.atEnd(m);
  }

  private short(m: Member): boolean {
    const el = m.el;
    if (!el || el.readyState < HAVE_FUTURE_DATA) return true;
    return this.ahead(m) < this.cfg.lowWaterS && !this.toEnd(m);
  }

  // -- acting on the elements (each marked, so its event is not taken for the user's) ----------

  private pauseEl(m: Member): void {
    if (!m.el || m.el.paused) return;
    m.expect.pause += 1;
    m.el.pause();
  }

  private playEl(m: Member): void {
    const el = m.el;
    if (!el || !el.paused) return;
    m.expect.play += 1;
    let p: Promise<void> | void;
    try {
      p = el.play();
    } catch {
      p = Promise.reject(new Error('play failed'));
    }
    void Promise.resolve(p).catch((e: unknown) => {
      // Refused (not an abort caused by our own pause): no 'play' event came, stop waiting for it.
      if ((e as { name?: string } | null)?.name === 'AbortError') return;
      m.expect.play = 0;
      if (this.active && this.intent === 'play') {
        this.intent = 'pause';
        for (const o of this.participants()) this.pauseEl(o);
        this.setStatus('paused');
      }
    });
  }

  /** Put the playhead at episode time `rel` (a no-op before the element has metadata). */
  private seekEl(m: Member, rel: number): void {
    const el = m.el;
    if (!el || el.readyState < HAVE_METADATA) return;
    const t = m.from + Math.max(0, Math.min(rel, this.length(m)));
    if (Math.abs(el.currentTime - t) <= SEEK_EPS) return;
    m.expect.seek += 1;
    el.currentTime = t;
  }

  private consume(m: Member, kind: Kind): boolean {
    if (m.expect[kind] <= 0) return false;
    m.expect[kind] -= 1;
    return true;
  }

  // -- the rules ------------------------------------------------------------------------------

  /** One video ran short: pause all of them at the leader's moment and wait. */
  private hold(): void {
    const lead = this.participants().find((m) => m.el);
    if (lead) this.target = this.rel(lead);
    for (const m of this.participants()) this.pauseEl(m);
    this.setStatus('buffering');
  }

  private finish(): void {
    this.intent = 'pause';
    for (const m of this.participants()) this.pauseEl(m);
    this.setStatus('ended');
  }

  /** Media events arrive while the controller acts (fakes dispatch them synchronously): one
   * evaluation at a time, repeated once if something asked for another meanwhile. */
  private evaluating = false;
  private again = false;

  private evaluate(): void {
    if (this.evaluating) {
      this.again = true;
      return;
    }
    this.evaluating = true;
    try {
      for (let round = 0; round < 5; round += 1) {
        this.again = false;
        this.step();
        if (!this.again) break;
      }
    } finally {
      this.evaluating = false;
    }
  }

  private step(): void {
    if (!this.active || this.intent !== 'play') return;
    const ps = this.participants();
    if (!ps.length) return;
    if (this.status === 'playing') {
      if (ps.some((m) => this.atEnd(m))) {
        this.finish();
        return;
      }
      if (ps.some((m) => this.short(m) || m.el!.paused)) {
        this.hold();
        return;
      }
      const lead = ps[0];
      const at = this.rel(lead);
      for (const m of ps.slice(1)) if (Math.abs(this.rel(m) - at) > this.cfg.driftS) this.seekEl(m, at);
      return;
    }
    // loading / buffering: everyone attached, at the target, with enough data
    if (!ps.every((m) => m.el)) return;
    for (const m of ps) this.seekEl(m, this.target);
    if (!ps.every((m) => this.ready(m))) return;
    if (ps.some((m) => this.atEnd(m))) {
      this.finish();
      return;
    }
    this.setStatus('playing');
    for (const m of ps) this.playEl(m);
  }

  // -- what the elements tell us -----------------------------------------------------------------

  private listen(m: Member, el: MediaLike): () => void {
    const on: [string, () => void][] = [
      ['pause', () => this.onPause(m)],
      ['play', () => this.onPlay(m)],
      ['seeking', () => this.onSeeking(m)],
      ['waiting', () => this.onShort(m, true)],
      ['stalled', () => this.onShort(m, false)],
      ['ended', () => this.active && this.finish()],
      ['error', () => this.onShort(m, true)],
      ['emptied', () => this.onNewSource(m)],
      ['loadstart', () => this.onNewSource(m)],
      ['loadedmetadata', () => this.onMetadata(m)],
      ['loadeddata', () => this.onData(m)],
      ['canplay', () => this.onData(m)],
      ['canplaythrough', () => this.onData(m)],
      ['progress', () => this.evaluate()],
      ['seeked', () => this.evaluate()],
      ['playing', () => this.evaluate()],
    ];
    for (const [type, fn] of on) el.addEventListener(type, fn);
    return () => {
      for (const [type, fn] of on) el.removeEventListener(type, fn);
    };
  }

  private synced(m: Member): boolean {
    return this.active && this.expected.includes(m.id) && !m.failed;
  }

  private onPause(m: Member): void {
    if (this.consume(m, 'pause') || !this.synced(m) || this.intent !== 'play') return;
    // the user paused one of them (or a fragment ended): all of them stop at its moment
    if (this.atEnd(m)) {
      this.finish();
      return;
    }
    this.intent = 'pause';
    this.target = this.rel(m);
    for (const o of this.participants()) {
      this.pauseEl(o);
      if (o !== m) this.seekEl(o, this.target);
    }
    this.setStatus('paused');
  }

  private onPlay(m: Member): void {
    if (this.consume(m, 'play') || !this.synced(m)) return;
    if (this.intent === 'play' && this.status === 'playing') return;
    // the user played one of them: all of them go, from its moment (from the start after the end)
    this.target = this.status === 'ended' ? 0 : this.rel(m);
    this.intent = 'play';
    for (const o of this.participants()) {
      this.pauseEl(o);
      this.seekEl(o, this.target);
    }
    this.setStatus('buffering');
    this.evaluate();
  }

  private onSeeking(m: Member): void {
    if (this.consume(m, 'seek') || !this.synced(m) || !m.settled) return;
    // the user moved one playhead: the others follow; playing waits until they have data there
    this.target = this.rel(m);
    for (const o of this.participants()) if (o !== m) this.seekEl(o, this.target);
    if (this.intent === 'play') {
      for (const o of this.participants()) this.pauseEl(o);
      this.setStatus('buffering');
    } else if (this.status === 'ended') {
      this.setStatus('paused');
    }
    this.evaluate();
  }

  private onShort(m: Member, always: boolean): void {
    if (!this.synced(m) || this.intent !== 'play' || this.status !== 'playing') return;
    if (always || this.short(m)) this.hold();
  }

  private onNewSource(m: Member): void {
    m.settled = false;
    m.expect = { pause: 0, play: 0, seek: 0 };
    if (this.synced(m) && this.intent === 'play' && this.status === 'playing') this.hold();
  }

  private onMetadata(m: Member): void {
    if (!this.synced(m) || !m.el) return;
    m.el.preload = 'auto';
    this.seekEl(m, this.target);
    this.evaluate();
  }

  private onData(m: Member): void {
    m.settled = true;
    this.evaluate();
  }
}
