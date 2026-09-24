// What the EEF module's record says about one episode (design 12 C.9, D49), read for a person: the
// module's conclusion and why, the CPU's sub-item readings per camera and the model's answer per
// review window with its marked crops. Shared by the adjudication card (F5.11) and the report's
// Episode tab (F5.12). Only classes, frame ids and the model's own words are shown - never raw JSON.
import { zh } from '../locales/zh';

type D = Record<string, unknown>;
const obj = (v: unknown): D => (v && typeof v === 'object' && !Array.isArray(v) ? (v as D) : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const s = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);
const Z = () => zh.eefDetail;

/** The sub-items a camera reads, in display order (state motion is the episode's, not a camera's). */
export const EEF_CAMERA_SUBITEMS = ['position_2d', 'orientation_2d', 'temporal_alignment', 'camera_motion', 'input_consistency'] as const;

export interface EefCall {
  code: string;
  text: string;
  camera: string | null;
}

export interface EefConclusion {
  outcome: string | null;
  reason: string | null;
  /** Why it went to a person (decide.py's human items), in order. */
  human: EefCall[];
  /** The defects the CPU and the model agree on (a reject). */
  confirmed: EefCall[];
  /** Sub-items the CPU could not assess, e.g. 「朝向（相机 ext）：无法评估」. */
  unchecked: string[];
}

const subitemName = (k: string) => zh.sections.eef.subitem[k] ?? k;
const statusName = (k: string) => zh.sections.eef.status[k] ?? k;

function calls(v: unknown): EefCall[] {
  return arr(v)
    .map(obj)
    .filter((c) => s(c.text))
    .map((c) => ({ code: s(c.code) ?? '', text: s(c.text) ?? '', camera: s(c.camera_id) }));
}

export function eefConclusion(details: D): EefConclusion {
  const d = obj(details.decision);
  return {
    outcome: s(d.outcome),
    reason: s(details.reason),
    human: calls(d.human),
    confirmed: calls(d.confirmed),
    unchecked: arr(d.unchecked)
      .map(obj)
      .map((u) => {
        const where = s(u.camera_id) ? Z().onCamera(s(u.camera_id) ?? '') : '';
        return `${subitemName(s(u.subitem) ?? '')}${where}：${statusName(s(u.status) ?? '')}`;
      }),
  };
}

export interface EefCpuRow {
  key: string;
  camera: string;
  mount: string;
  /** sub-item -> its status name ('—' when the camera has no reading) */
  cells: Record<string, string>;
  /** sub-items the CPU finds suspect on this camera */
  suspect: string[];
}

export function eefCpuRows(details: D): EefCpuRow[] {
  return Object.entries(obj(details.cameras)).map(([camera, raw]) => {
    const cam = obj(raw);
    const cells: Record<string, string> = {};
    const suspect: string[] = [];
    for (const k of EEF_CAMERA_SUBITEMS) {
      const st = s(obj(obj(cam.subitems)[k]).status);
      cells[k] = st ? statusName(st) : '—';
      if (st === 'suspect') suspect.push(k);
    }
    return { key: camera, camera, mount: s(cam.mount) ?? '—', cells, suspect };
  });
}

/** The episode-level state motion reading, e.g. 「可疑」, or null when the record has none. */
export function eefStateMotion(details: D): string | null {
  const st = s(obj(details.state_motion).status);
  return st ? statusName(st) : null;
}

export interface EefVote {
  label: string;
  value: string;
  /** refute is shown as a warning, support as agreement */
  tone: 'good' | 'bad' | 'none';
}

export interface EefWindowRow {
  key: string;
  camera: string;
  kind: string;
  /** e.g. 「候选段 · 位置」 or 「均匀抽样」 */
  title: string;
  /** e.g. 「帧 120–168（3 帧）」 */
  frames: string;
  /** e.g. 「点 block_center · 轴 gripper_x」 */
  target: string | null;
  answered: boolean;
  votes: EefVote[];
  offset: string | null;
  explanation: string | null;
  failure: string | null;
  conflict: string | null;
  cached: boolean;
  evidence: string[];
}

const VOTES: [string, string][] = [
  ['position_support', 'position'],
  ['orientation_support', 'orientation'],
  ['tracking_target_correct', 'tracking'],
];

function windowRow(camera: string, raw: D, i: number): EefWindowRow {
  const kind = s(raw.kind) ?? '';
  const sub = s(raw.subitem);
  const frames = arr(raw.frames).filter((f): f is number => typeof f === 'number');
  const answer = obj(raw.answer);
  const answered = raw.status === 'answered' && Object.keys(answer).length > 0;
  const votes: EefVote[] = answered
    ? VOTES.map(([field, label]) => {
        const v = s(answer[field]) ?? '';
        return { label: Z().vote[label], value: zh.sections.eef.reviewClasses[v] ?? v, tone: v === 'refute' ? 'bad' : v === 'support' ? 'good' : 'none' };
      })
    : [];
  const dir = s(answer.offset_direction);
  const mag = s(answer.offset_magnitude_class);
  const offset = answered && dir && dir !== 'none' ? Z().offset(Z().offsetDirection[dir] ?? dir, mag ? Z().offsetMagnitude[mag] ?? mag : '') : null;
  const failure = obj(raw.failure);
  const conflict = obj(raw.conflict);
  const point = s(raw.point_id);
  const axis = s(raw.axis_id);
  return {
    key: `${camera}-${i}`,
    camera,
    kind,
    title: [Z().kind[kind] ?? kind, sub ? subitemName(sub) : null].filter(Boolean).join(' · '),
    frames: frames.length ? Z().frames(Math.min(...frames), Math.max(...frames), frames.length) : '—',
    target: point ? Z().target(point, axis) : null,
    answered,
    votes,
    offset,
    explanation: answered ? s(answer.explanation) : null,
    failure: raw.status === 'failed' ? Z().failure[s(failure.code) ?? ''] ?? s(failure.code) ?? Z().failed : null,
    conflict: s(conflict.subitem) ? Z().conflict(subitemName(s(conflict.subitem) ?? ''), statusName(s(conflict.cpu) ?? ''), zh.sections.eef.reviewClasses[s(conflict.vlm) ?? ''] ?? '') : null,
    cached: raw.cache_hit === true,
    evidence: arr(raw.evidence).filter((p): p is string => typeof p === 'string' && p.length > 0),
  };
}

/** Every review window of the episode, camera by camera, in the order they were asked. */
export function eefWindowRows(details: D): EefWindowRow[] {
  const cams = obj(obj(details.review).cameras);
  return Object.entries(cams).flatMap(([camera, raw]) => arr(obj(raw).windows).map((w, i) => windowRow(camera, obj(w), i)));
}

/** The record's evidence the windows do not show: the CPU's overlay frames. */
export function eefCpuEvidence(evidence: readonly string[], windows: readonly EefWindowRow[]): string[] {
  const shown = new Set(windows.flatMap((w) => w.evidence));
  return evidence.filter((p) => !shown.has(p));
}
