// What the EEF module's record says about one episode (design 12 C.9, D49), read for a person: the
// module's conclusion and why, the CPU's sub-item readings per camera and the model's answer per
// review window with its marked crops. Shared by the adjudication card (F5.11) and the report's
// Episode tab (F5.12). Only classes, frame ids and the model's own words are shown - never raw JSON.
import { zh } from '../locales/zh';
import { fmt, signed } from './sectionStats';

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
  /** e.g. 「工具中心点（TCP） · 朝向 Z 轴（接近方向）」 */
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

/** The record's evidence the windows and the dataset-record block do not show: the CPU's overlay frames. */
export function eefCpuEvidence(evidence: readonly string[], windows: readonly EefWindowRow[], elsewhere: readonly string[] = []): string[] {
  const shown = new Set([...windows.flatMap((w) => w.evidence), ...elsewhere]);
  return evidence.filter((p) => !shown.has(p));
}

// ---------------------------------------------------------------- the model's opinion (D-E15)

export interface EefOpinionSegment {
  key: string;
  startFrame: number;
  endFrame: number;
  startS: number | null;
  endS: number | null;
  aspect: string;
  /** How sure the model is that the stretch does NOT match, 0-1. */
  confidence: number;
  /** Sample frames (from 0) the model cited; the report seeks the camera's player to them. */
  evidenceFrames: number[];
  observation: string;
}

export interface EefOpinionCamera {
  camera: string;
  status: string;
  point: string | null;
  axis: string | null;
  /** The fingers' line B (drawn orange), when drawn. */
  fingerAxis: string | null;
  /** The model's one-line summaries, one per clip part. */
  summaries: string[];
  /** Why a part got no answer, readable. */
  failures: string[];
  /** Parts answered on the repair turn: why the first answer was rejected, readable. */
  repairs: string[];
  /** The gripper could not be seen in some part. */
  unseen: boolean;
  segments: EefOpinionSegment[];
  reason: string | null;
}

export interface EefOpinion {
  status: string;
  flagged: boolean;
  maxConfidence: number | null;
  failure: string | null;
  cameras: EefOpinionCamera[];
  /** a handheld gripper (design doc 22 §5.2): each wrist camera marks only its own hand */
  handheld: boolean;
  /** the pose gaps the checks bridged (the default gap, and how many frames per hand), or null */
  bridged: { maxGapMs: number; hands: [string, number][] } | null;
}

const n = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/**
 * No gripper reference (design doc 12 §10.5, D-E15): the model's advisory opinion on each camera's
 * whole clip - the stretches it finds mismatched, each with its confidence and evidence frames. Null
 * for a record the module judged. Nothing marked is saved: the report draws the marks live (design doc 20).
 */
export function eefOpinion(details: D): EefOpinion | null {
  if (details.assessment_mode !== 'vlm_opinion') return null;
  const op = obj(details.opinion);
  const cameras = Object.entries(obj(op.cameras)).map(([camera, raw]): EefOpinionCamera => {
    const c = obj(raw);
    const clips = arr(c.clips).map(obj);
    return {
      camera,
      status: s(c.status) ?? 'failed',
      point: s(c.point_id),
      axis: s(c.axis_id),
      fingerAxis: s(c.finger_axis_id),
      summaries: clips.map((x) => s(x.summary)).filter((x): x is string => Boolean(x)),
      failures: clips
        .filter((x) => x.status === 'failed')
        .map((x) => {
          const code = s(obj(x.failure).code) ?? '';
          return `${Z().frames(n(x.start_frame) ?? 0, n(x.end_frame) ?? 0, (n(x.end_frame) ?? 0) - (n(x.start_frame) ?? 0) + 1)}：${Z().failure[code] ?? (code || Z().failed)}`;
        }),
      repairs: clips
        .filter((x) => x.repaired)
        .map((x) => {
          const r = obj(x.repaired);
          const code = s(r.code) ?? '';
          const why = [Z().failure[code] ?? code, s(r.message)].filter(Boolean).join('：');
          return Z().opinion.repaired(Z().frames(n(x.start_frame) ?? 0, n(x.end_frame) ?? 0, (n(x.end_frame) ?? 0) - (n(x.start_frame) ?? 0) + 1), why);
        }),
      unseen: clips.some((x) => x.gripper_visible === false),
      reason: s(c.reason),
      segments: arr(c.segments)
        .map(obj)
        .map((x, i) => ({
          key: `${camera}-${i}`,
          startFrame: n(x.start_frame) ?? 0,
          endFrame: n(x.end_frame) ?? 0,
          startS: n(x.start_s),
          endS: n(x.end_s),
          aspect: s(x.aspect) ?? '',
          confidence: n(x.confidence) ?? 0,
          evidenceFrames: arr(x.evidence_frames).map(n).filter((f): f is number => f !== null),
          observation: s(x.observation) ?? '',
        }))
        .sort((a, b) => b.confidence - a.confidence || a.startFrame - b.startFrame),
    };
  });
  const interp = obj(op.interpolation);
  const gap = n(interp.max_gap_s);
  const bridged = gap !== null ? { maxGapMs: Math.round(gap * 1000), hands: Object.entries(obj(interp.frames)).map(([h, v]): [string, number] => [h, n(v) ?? 0]) } : null;
  return {
    status: s(op.status) ?? 'failed',
    flagged: op.flagged === true,
    maxConfidence: n(op.max_confidence),
    failure: s(op.failure),
    cameras,
    handheld: (s(op.prompt_version) ?? '').startsWith('umi-'),
    bridged,
  };
}

// ---------------------------------------------------------------- a wrist camera's own motion (design doc 22 §5.3)

export interface EefEgoSegment {
  key: string;
  startFrame: number;
  endFrame: number;
  startS: number | null;
  endS: number | null;
  /** rotation | time_offset */
  reason: string;
  magnitude: number;
  /** deg | s */
  unit: string;
  /** minor | moderate | severe */
  band: string;
  evidenceFrames: number[];
  /** a time offset's lag (s): the picture at t shows the pose recorded at t + lag */
  lagS: number | null;
}

export interface EefEgoCamera {
  camera: string;
  /** ok | suspect | unknown | unsupported */
  status: string;
  reason: string | null;
  rotationMedian: number | null;
  rotationP95: number | null;
  directionMedian: number | null;
  lagS: number | null;
  lagConfidence: number | null;
  lagFlagged: boolean;
  coverage: number | null;
  pairs: number | null;
  segments: EefEgoSegment[];
  unmatched: { startFrame: number; endFrame: number; startS: number | null; endS: number | null }[];
}

export interface EefEgoMotion {
  status: string;
  /** good | bad | unknown */
  verdict: string;
  explanation: string;
  uncalibrated: boolean;
  /** 「按假设值：…」 from the trajectory file, when it rests on assumptions */
  assumed: string | null;
  windowS: number | null;
  cameras: EefEgoCamera[];
}

/** The wrist cameras' own motion against their recorded poses (``details.ego_motion``); null without it. */
export function eefEgoMotion(details: D): EefEgoMotion | null {
  const ego = obj(details.ego_motion);
  if (!s(ego.status)) return null;
  const cameras = Object.entries(obj(ego.cameras)).map(([camera, raw]): EefEgoCamera => {
    const c = obj(raw);
    const m = obj(c.metrics);
    const lag = obj(c.lag);
    return {
      camera,
      status: s(c.status) ?? 'unknown',
      reason: s(c.reason),
      rotationMedian: n(m.rotation_median_deg),
      rotationP95: n(m.rotation_p95_deg),
      directionMedian: n(m.direction_median_deg),
      lagS: n(lag.lag_s) ?? n(m.lag_s),
      lagConfidence: n(lag.confidence) ?? n(m.lag_confidence),
      lagFlagged: lag.flagged === true,
      coverage: n(m.coverage),
      pairs: n(m.pairs),
      segments: arr(c.segments)
        .map(obj)
        .map((g, i) => ({
          key: `${camera}-${i}`,
          startFrame: n(g.start_frame) ?? 0,
          endFrame: n(g.end_frame) ?? 0,
          startS: n(g.start_s),
          endS: n(g.end_s),
          reason: s(g.reason) ?? '',
          magnitude: n(g.magnitude) ?? 0,
          unit: s(g.unit) ?? '',
          band: s(g.band) ?? '',
          evidenceFrames: arr(g.evidence_frames).map(n).filter((f): f is number => f !== null),
          lagS: n(g.lag_s),
        })),
      unmatched: arr(c.unmatched)
        .map(obj)
        .map((u) => ({ startFrame: n(u.start_frame) ?? 0, endFrame: n(u.end_frame) ?? 0, startS: n(u.start_s), endS: n(u.end_s) })),
    };
  });
  return {
    status: s(ego.status) ?? 'unknown',
    verdict: s(ego.verdict) ?? 'unknown',
    explanation: s(ego.explanation_zh) ?? '',
    uncalibrated: ego.uncalibrated !== false,
    assumed: s(ego.assumed),
    windowS: n(ego.window_s),
    cameras,
  };
}

// ---------------------------------------------------------------- the dataset's own record (D-E16)

export interface EefRecordCurves {
  frame: number[];
  position: (number | null)[];
  positionAfter: (number | null)[];
  rotation: (number | null)[];
  rotationAfter: (number | null)[];
}

export interface EefRecordRelation {
  label: string;
  value: string;
  bad: boolean;
}

export interface EefRecordSource {
  kind: string;
  /** 位姿列 / 关节角正解 */
  name: string;
  status: string;
  statusText: string;
  /** Where the record is read, e.g. 「列 action · 机器人 franka_panda」. */
  where: string;
  /** e.g. 「记录的帧 panda_link8 → 上传的帧 tcp」 */
  frames: string | null;
  /** e.g. 「按帧号对齐，比了 280 / 287 帧」 */
  alignment: string | null;
  reasons: string[];
  notes: string[];
  /** 按声明关系的差 / 扣掉恒定差后: median, P95 and max of position (mm) and rotation (deg). */
  residual: { key: string; what: string; position: string; rotation: string }[];
  /** 声明的关系 / 拟合的恒定差 / 与声明相差. */
  relation: EefRecordRelation[];
  /** The time offset between the two tracks, always said when it was estimated. */
  lag: string | null;
  segments: string[];
  curves: EefRecordCurves | null;
}

export interface EefDatasetRecord {
  status: string;
  statusText: string;
  /** Why nothing could be compared (unsupported / error), readable. */
  reasons: string[];
  message: string | null;
  sources: EefRecordSource[];
  internal: { text: string; bad: boolean } | null;
  overlays: { path: string; camera: string; frame: number }[];
  /** The overlay colours, e.g. 「红：上传的轨迹」. */
  legend: string[];
  evidenceError: string | null;
}

const R = () => zh.eefDetail.record;
const nums = (v: unknown): (number | null)[] => arr(v).map(n);

function stat(v: unknown, digits: number): string {
  const x = obj(v);
  return n(x.median) === null ? '—' : R().stat(fmt(n(x.median), digits), fmt(n(x.p95), digits), fmt(n(x.max), digits));
}

function transform(v: unknown): string {
  const x = obj(v);
  return R().transform(fmt(n(x.translation_norm_mm), 1), fmt(n(x.rotation_deg), 2));
}

function recordSource(kind: string, raw: D): EefRecordSource {
  const src = obj(raw.source);
  const status = s(raw.status) ?? 'error';
  const reasons = arr(raw.reasons).map((r) => s(r) ?? '');
  const notes = arr(raw.notes).map((r) => s(r) ?? '');
  const key = s(src.key);
  const where = [
    key ? R().column(key) : s(src.topic) ? R().topic(s(src.topic) ?? '', s(src.fields)) : null,
    s(src.layout),
    s(src.robot) ? R().robot(s(src.robot) ?? '') : null,
  ]
    .filter(Boolean)
    .join(' · ');
  const ids = obj(raw.frame_ids);
  const how = s(raw.alignment);
  const compared = n(raw.frames_compared);
  const rel = obj(raw.relation);
  const relation: EefRecordRelation[] = [];
  if (rel.declared === true && rel.expected) relation.push({ label: R().expected, value: transform(rel.expected), bad: false });
  const shifted = Object.keys(obj(rel.fitted_after_time_offset)).length > 0;
  if (rel.fitted) relation.push({ label: shifted ? R().fittedShifted : R().fitted, value: transform(shifted ? rel.fitted_after_time_offset : rel.fitted), bad: false });
  if (rel.deviation) relation.push({ label: R().deviation, value: transform(rel.deviation), bad: reasons.includes('constant_mismatch') });
  const off = obj(raw.time_offset);
  const lagFrames = n(off.lag_frames);
  const lag =
    lagFrames === null
      ? null
      : notes.includes('time_offset')
        ? R().lag(fmt(Math.abs(lagFrames), 2), fmt(Math.abs(n(off.lag_s) ?? 0), 3), lagFrames > 0)
        : R().lagSmall(signed(lagFrames, 2));
  const residual = raw.residual
    ? [
        { key: 'raw', what: R().rows.raw, position: stat(obj(raw.residual).position_mm, 1), rotation: stat(obj(raw.residual).rotation_deg, 2) },
        ...(raw.residual_after_constant
          ? [{ key: 'after', what: R().rows.after, position: stat(obj(raw.residual_after_constant).position_mm, 1), rotation: stat(obj(raw.residual_after_constant).rotation_deg, 2) }]
          : []),
      ]
    : [];
  const c = obj(raw.curves);
  const frame = arr(c.frame).map(n).filter((f): f is number => f !== null);
  return {
    kind,
    name: R().source[kind] ?? kind,
    status,
    statusText: R().status[status] ?? status,
    where,
    frames: s(ids.upload) ? (s(ids.record) ? R().frames(s(ids.record) ?? '', s(ids.upload) ?? '') : R().framesUndeclared(s(ids.upload) ?? '')) : null,
    alignment: how ? [R().alignment[how] ?? how, compared !== null ? R().compared(compared, n(raw.frames_with_pose) ?? compared) : null].filter(Boolean).join('，') : null,
    reasons: reasons.map((r) => R().reason[r] ?? r),
    notes: notes.filter((x) => x !== 'time_offset').map((x) => R().note[x] ?? x),
    residual,
    relation,
    lag,
    segments: arr(raw.segments)
      .map(obj)
      .map((g) => {
        const aspect = s(g.aspect) ?? 'position';
        const from = n(g.start_frame) ?? 0;
        const to = n(g.end_frame) ?? 0;
        return R().segment(Z().frames(from, to, to - from + 1), R().aspect[aspect] ?? aspect, (R().peak[aspect] ?? String)(fmt(n(g.peak), aspect === 'position' ? 1 : 2)));
      }),
    curves: frame.length
      ? { frame, position: nums(c.position_mm), positionAfter: nums(c.position_after_constant_mm), rotation: nums(c.rotation_deg), rotationAfter: nums(c.rotation_after_constant_deg) }
      : null,
  };
}

/**
 * The uploaded trajectory against the dataset's own record (design doc 12 §8.7, D-E16): per source (the
 * pose columns, the joints through the robot's kinematics) how it was paired, the residuals, the constant
 * against the declared relation, the time offset and the stretches that differ; the dataset's own two
 * records against each other; the overlay frames. Reported only. Null for a record written before it.
 */
export function eefDatasetRecord(details: D): EefDatasetRecord | null {
  if (!details.record || typeof details.record !== 'object') return null;
  const r = obj(details.record);
  const status = s(r.status) ?? 'error';
  const sources = Object.entries(obj(r.sources)).map(([k, v]) => recordSource(k, obj(v)));
  const internal = obj(r.internal);
  const pos = obj(internal.position_mm);
  const rot = obj(internal.rotation_deg);
  const shown = arr(r.evidence).map(obj);
  const legend = [...new Set(shown.flatMap((e) => Object.keys(obj(e.legend))))];
  return {
    status,
    statusText: R().status[status] ?? status,
    reasons: sources.length ? [] : arr(r.reasons).map((x) => R().reason[s(x) ?? ''] ?? s(x) ?? ''),
    message: s(r.message),
    sources,
    internal:
      internal.compared === true
        ? { text: R().internalValue(internal.consistent === true, fmt(n(pos.p95), 1), fmt(n(rot.p95), 2)), bad: internal.consistent === false }
        : internal.compared === false
          ? { text: R().internalNot, bad: false }
          : null,
    overlays: shown
      .filter((e) => s(e.path))
      .map((e) => ({ path: s(e.path) ?? '', camera: s(e.camera_id) ?? '', frame: n(e.frame_index) ?? 0 })),
    legend: legend.map((k) => R().legend[k] ?? k),
    evidenceError: s(r.evidence_error),
  };
}
