import type { EpisodeFinding, ResultRecord, VizEpisode } from '../../api/types';
import { evidenceRange, frameCount } from '../../lib/vizTime';
import { zh } from '../../locales/zh';
import type { MotionSeg } from './Transport';
// Opening the mini player on a moment that is not a finding (design doc 22 §3.3): a sample frame of a
// camera of the task's EEF bundle, as the opinion's evidence frames cite it.
import { createContext } from 'react';

/** A camera of the EEF bundle (its own id, as the opinion names it) and a sample frame of it. */
export interface MiniSeek {
  camera: string;
  frame: number;
}

/** Given by a page that can open the mini player (the report's Episode 明细); null where none can. */
export const MiniPlayerOpen = createContext<((seek: MiniSeek) => void) | null>(null);


/** The motion strip of the mini player (design doc 18 §4.6): the rest at the edges, the pauses and the still
 * stretches the motion check read (registry 8.11 readings), and its stuck findings, in player seconds. The still
 * stretches come first so the darker idle / pause / stuck marks lie over them. */
export function motionStrip(record: ResultRecord | undefined | null, findings: readonly EpisodeFinding[], ep: VizEpisode | null | undefined): MotionSeg[] {
  if (!record || !ep) return [];
  const rd = (record.readings ?? {}) as Record<string, unknown>;
  const tl = ep.timeline;
  const frames = frameCount(ep);
  const clock = ep.check_clock;
  const span = (a: number, b: number) => evidenceRange({ time_s: [a, b] }, tl, frames, clock);
  const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const out: MotionSeg[] = [];
  const M = zh.viz.motion;
  for (const s of Array.isArray(rd.still_segments) ? (rd.still_segments as Record<string, unknown>[]) : []) {
    const a = num(s.start_s);
    const b = num(s.end_s);
    if (a === null || b === null || b <= a) continue;
    const at = span(a, b);
    if (at) out.push({ kind: 'still', start: at[0], end: at[1], label: M.still });
  }
  for (const s of Array.isArray(rd.idle_mid_segments) ? (rd.idle_mid_segments as Record<string, unknown>[]) : []) {
    const a = num(s.start_s);
    const d = num(s.dur_s);
    if (a === null || d === null || d <= 0) continue;
    const at = span(a, a + d);
    if (at) out.push({ kind: 'pause', start: at[0], end: at[1], label: M.pause });
  }
  const head = num(rd.idle_head_s);
  if (head !== null && head > 0) {
    const at = span(0, head);
    if (at) out.push({ kind: 'idle_head', start: at[0], end: at[1], label: M.idleHead });
  }
  const tail = num(rd.idle_tail_s);
  if (tail !== null && tail > 0 && ep.duration_s > 0) {
    const offset = clock?.offset_s ?? 0;
    const endCheck = ep.duration_s - offset;
    const at = span(Math.max(0, endCheck - tail), endCheck);
    if (at) out.push({ kind: 'idle_tail', start: at[0], end: at[1], label: M.idleTail });
  }
  for (const f of findings) {
    if (f.module !== 'motion_quality' || f.finding.code !== 'stuck') continue;
    const at = evidenceRange(f.finding, tl, frames, clock);
    if (at) out.push({ kind: 'stuck', start: at[0], end: at[1], label: f.finding.message_zh || M.stuck });
  }
  return out;
}
