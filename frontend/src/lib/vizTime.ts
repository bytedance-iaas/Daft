// The data visualizer's clock arithmetic (design doc 18 §4.4, D64): episode time in seconds is the
// only clock; frame numbers, camera media times, segments and findings are all converted to and from
// it here. Pure functions, no DOM.
import type { VizCheckClock, VizEpisode, VizEpisodeCamera, VizSegment, VizTimeline } from '../api/types';

/** Index of the last element <= x in an ascending array (-1 when x is before the first). */
export function bisectRight(arr: readonly number[], x: number): number {
  let lo = 0;
  let hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (arr[mid] <= x) lo = mid + 1;
    else hi = mid;
  }
  return lo - 1;
}

export function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

/** `mm:ss.s` (the transport and the video stamps). */
export function fmtClock(seconds: number): string {
  const s = Math.max(0, Number.isFinite(seconds) ? seconds : 0);
  const m = Math.floor(s / 60);
  const rest = s - m * 60;
  // 59.96 s rounds to 60.0: carry it into the minutes instead of printing "00:60.0"
  const tenths = Math.round(rest * 10);
  if (tenths >= 600) return `${String(m + 1).padStart(2, '0')}:00.0`;
  return `${String(m).padStart(2, '0')}:${(tenths / 10).toFixed(1).padStart(4, '0')}`;
}

/** How many frames the episode's timeline has. */
export function frameCount(ep: Pick<VizEpisode, 'frames' | 'timeline'>): number {
  if (ep.timeline.kind === 'timestamp' && ep.timeline.frame_times) return ep.timeline.frame_times.length;
  return Math.max(0, ep.frames);
}

/**
 * The frame shown at episode time t: with an fps timeline the nearest frame (frame k covers
 * [k/fps - half, k/fps + half)); with a timestamp timeline (mcap) the last frame whose time is <= t.
 */
export function frameAt(timeline: VizTimeline, frames: number, t: number): number {
  if (frames <= 0) return 0;
  if (timeline.kind === 'timestamp' && timeline.frame_times && timeline.frame_times.length) {
    return clamp(bisectRight(timeline.frame_times, t + 1e-9), 0, frames - 1);
  }
  const fps = timeline.fps ?? 0;
  if (fps <= 0) return 0;
  return clamp(Math.round(t * fps), 0, frames - 1);
}

/** Episode time of frame f. */
export function timeOfFrame(timeline: VizTimeline, frames: number, f: number): number {
  if (frames <= 0) return 0;
  const k = clamp(Math.round(f), 0, frames - 1);
  if (timeline.kind === 'timestamp' && timeline.frame_times && timeline.frame_times.length) return timeline.frame_times[k];
  const fps = timeline.fps ?? 0;
  return fps > 0 ? k / fps : 0;
}

/** One frame's length around t (for drift tolerances and "the last frame" of the episode). */
export function frameStep(timeline: VizTimeline, frames: number, t: number): number {
  if (timeline.kind === 'timestamp' && timeline.frame_times && timeline.frame_times.length > 1) {
    const ft = timeline.frame_times;
    const k = clamp(bisectRight(ft, t), 0, ft.length - 2);
    const step = ft[k + 1] - ft[k];
    if (step > 0) return step;
    return (ft[ft.length - 1] - ft[0]) / Math.max(1, ft.length - 1) || 1 / 30;
  }
  const fps = timeline.fps ?? 0;
  return fps > 0 ? 1 / fps : frames > 0 ? 1 / 30 : 1 / 30;
}

/** The time n frames away from the frame shown at t. */
export function stepFrom(timeline: VizTimeline, frames: number, t: number, n: number): number {
  return timeOfFrame(timeline, frames, frameAt(timeline, frames, t) + n);
}

/**
 * Where a camera's video is at episode time t: its own clock starts at `offset_s` (the camera's first
 * frame) and, for a LeRobot v3 file holding several episodes, at `from_ts` inside the file.
 */
export function mediaTime(cam: Pick<VizEpisodeCamera, 'offset_s' | 'from_ts'>, t: number): number {
  return t - (cam.offset_s ?? 0) + (cam.from_ts ?? 0);
}

/** Episode time of a camera's media time (the inverse of mediaTime). */
export function episodeTimeOfMedia(cam: Pick<VizEpisodeCamera, 'offset_s' | 'from_ts'>, m: number): number {
  return m + (cam.offset_s ?? 0) - (cam.from_ts ?? 0);
}

/** The segment under t (index into segments, -1 for a gap); segments are sorted by start. */
export function segmentAt(segments: readonly Pick<VizSegment, 'start_s' | 'end_s'>[], t: number): number {
  let lo = 0;
  let hi = segments.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (segments[mid].start_s <= t) lo = mid + 1;
    else hi = mid;
  }
  // the last segment that starts at or before t; overlapping segments: the latest one that covers t
  for (let i = lo - 1; i >= 0 && i >= lo - 4; i -= 1) {
    if (t < segments[i].end_s || (segments[i].end_s === segments[i].start_s && t === segments[i].start_s)) return i;
  }
  return -1;
}

/** A finding's place on the timeline (findings 2.0: `frames` and / or `time_s`, either end open). */
export interface EvidenceSpan {
  frames?: readonly (number | null)[] | null;
  time_s?: readonly (number | null)[] | null;
}

/**
 * [start, end] in episode seconds of a finding the checks reported. Check frames count on the
 * check reader's clock (`check_clock`: its anchor and rate - for mcap the first action message and
 * the action rate, design doc 18 §4.6); without one, the episode's own timeline. Times the checks
 * report (`time_s`) are seconds on that same clock. A point becomes one frame long.
 */
export function evidenceRange(span: EvidenceSpan, timeline: VizTimeline, frames: number, clock: VizCheckClock | null | undefined): [number, number] | null {
  const offset = clock?.offset_s ?? 0;
  const ts = span.time_s;
  if (ts && ts.length && ts[0] != null) {
    const a = offset + ts[0];
    const b = ts.length > 1 && ts[1] != null ? offset + ts[1] : a;
    return [a, Math.max(a, b)];
  }
  const fr = span.frames;
  if (fr && fr.length && fr[0] != null) {
    const a0 = fr[0];
    const b0 = fr.length > 1 && fr[1] != null ? fr[1] : a0;
    const rate = clock?.fps ?? 0;
    if (clock && rate > 0) {
      return [offset + a0 / rate, offset + (b0 + 1) / rate];
    }
    const a = timeOfFrame(timeline, frames, a0);
    const last = timeOfFrame(timeline, frames, b0);
    return [a, last + frameStep(timeline, frames, last)];
  }
  return null;
}

/**
 * Whether an episode number exists: `episode_indices` is null when the numbers are 0 … count − 1,
 * else the compact list the preflight writes (`5,9,12`, `0-28,40`).
 */
export function hasEpisode(compact: string | null | undefined, count: number, n: number): boolean {
  if (!Number.isInteger(n) || n < 0) return false;
  if (!compact) return n < count;
  return compact.split(',').some((part) => {
    const m = /^\s*(\d+)(?:-(\d+))?\s*$/.exec(part);
    if (!m) return false;
    const a = Number(m[1]);
    const b = m[2] !== undefined ? Number(m[2]) : a;
    return n >= a && n <= b;
  });
}

/** The first episode number. */
export function firstEpisode(compact: string | null | undefined, count: number): number | null {
  if (!compact) return count > 0 ? 0 : null;
  const m = /^\s*(\d+)/.exec(compact);
  return m ? Number(m[1]) : null;
}
