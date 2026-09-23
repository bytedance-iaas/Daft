// One episode's module readings in Chinese (the report's Episode tab, F6.2): pure functions over a
// result record's `details` - the shapes the checks write (C2 result-record: details is free) -
// that the blocks render. Keys a reading does not know are left to the generic block.
import { zh } from '../locales/zh';
import { fmt, num, signed, str } from './sectionStats';

type Details = Record<string, unknown>;

const E = () => zh.episodeTab;

export interface Fact {
  label: string;
  value: string;
  warn?: boolean;
}

// ---------------------------------------------------------------------------- timestamps

export function timestampFacts(d: Details): { facts: Fact[]; gaps: string[] } {
  const Z = E().timestamp;
  const facts: Fact[] = [];
  if (num(d.duration_s) !== null) facts.push({ label: Z.duration, value: zh.sections.timestamp.seconds(num(d.duration_s)!) });
  if (num(d.n) !== null) facts.push({ label: Z.frames, value: String(num(d.n)) });
  if (num(d.dt_nominal) !== null) facts.push({ label: Z.dtNominal, value: zh.sections.timestamp.seconds(num(d.dt_nominal)!) });
  if (num(d.max_dt) !== null) facts.push({ label: Z.maxDt, value: zh.sections.timestamp.seconds(num(d.max_dt)!), warn: Array.isArray(d.gap_frames) && d.gap_frames.length > 0 });
  if (num(d.jitter_ratio) !== null) facts.push({ label: Z.jitter, value: `${fmt(num(d.jitter_ratio)! * 100, 1)}%` });
  const gaps = (Array.isArray(d.gap_frames) ? d.gap_frames : [])
    .filter((g): g is { frame: number; dt: number } => Boolean(g) && typeof g === 'object' && num((g as Details).frame) !== null)
    .map((g) => Z.gap(g.frame, fmt(num(g.dt))));
  return { facts, gaps };
}

// ---------------------------------------------------------------------------- kinematics

export interface ViolationRow {
  key: string;
  type: string;
  joint: string;
  frame: string;
  value: string;
  limit: string;
}

export function violationRows(d: Details, max = 20): { rows: ViolationRow[]; more: number } {
  const K = zh.sections.kinematics;
  const all = [...(Array.isArray(d.violations) ? d.violations : []), ...(Array.isArray(d.transient_violations) ? d.transient_violations : [])].filter((v): v is Details => Boolean(v) && typeof v === 'object');
  const rows = all.slice(0, max).map((v, i) => ({
    key: String(i),
    type: K.types[String(v.type)] ?? String(v.type ?? '—'),
    joint: v.joint === undefined || v.joint === null ? '—' : K.joint(String(v.joint)),
    frame: num(v.frame) === null ? '—' : String(num(v.frame)),
    value: num(v.value) === null ? '—' : fmt(num(v.value), 4),
    limit: Array.isArray(v.limit) ? `${fmt(num(v.limit[0]), 3)} ~ ${fmt(num(v.limit[1]), 3)}` : num(v.limit) === null ? String(v.limit ?? '—') : fmt(num(v.limit), 4),
  }));
  const total = Math.max(all.length, num(d.n_violations) ?? 0);
  return { rows, more: Math.max(0, total - rows.length) };
}

// ---------------------------------------------------------------------------- motion

/** Sub-scores in the check's order; true = part of the total (motion_quality's _EXCLUDE). */
export const MOTION_KEYS: [string, boolean][] = [
  ['smoothness', true],
  ['spike', true],
  ['gripper_jitter', true],
  ['actuator_saturation', true],
  ['path_efficiency', false],
  ['joint_stability', false],
  ['fluency', false],
];

const NA_REASON: Record<string, string> = { actuator_saturation: 'saturation_reason', spike: 'spike_reason', gripper_jitter: 'gripper_reason', fluency: 'fluency_reason' };

export interface SubscoreRow {
  key: string;
  name: string;
  value: string;
  role: string;
  note: string;
}

export function motionRows(d: Details): SubscoreRow[] {
  const M = zh.sections.motion;
  const rows: SubscoreRow[] = [];
  for (const [k, inTotal] of MOTION_KEYS) {
    if (!(k in d)) continue;
    const v = num(d[k]);
    rows.push({
      key: k,
      name: M.sub[k] ?? k,
      value: v === null ? E().motion.na : fmt(v, 3),
      role: v === null ? '—' : inTotal ? M.inTotal : M.reportOnly,
      note: v === null ? str(d[NA_REASON[k] ?? '']) ?? M.naGeneric : '',
    });
  }
  return rows;
}

export function motionFacts(d: Details): Fact[] {
  const Z = E().motion;
  const facts: Fact[] = [];
  const stuck = Array.isArray(d.stuck_joints) ? d.stuck_joints.filter((j): j is Details => Boolean(j) && typeof j === 'object') : [];
  if (stuck.length) {
    const joints = stuck.map((j) => Z.stuckJoint(String(j.axis ?? j.joint ?? '?'), num(j.freeze_start_frame) ?? 0, num(j.freeze_end_frame) ?? 0)).join('、');
    facts.push({ label: Z.stuck, value: Z.stuckYes(joints), warn: true });
  } else if ('stuck' in d && d.stuck === null) {
    facts.push({ label: Z.stuck, value: `${E().motion.na}：${str(d.stuck_reason) ?? zh.sections.motion.naGeneric}` });
  } else if ('stuck' in d || 'stuck_joints' in d) {
    facts.push({ label: Z.stuck, value: Z.stuckNo });
  }
  if (num(d.idle_head_s) !== null || num(d.idle_tail_s) !== null) {
    facts.push({ label: Z.idle, value: Z.idleValue(fmt(num(d.idle_head_s) ?? 0, 1), fmt(num(d.idle_tail_s) ?? 0, 1), num(d.idle_mid_count) ?? 0, fmt(num(d.idle_mid_total_s) ?? 0, 1)) });
  }
  if (num(d.active_ratio) !== null) facts.push({ label: Z.active, value: `${fmt(num(d.active_ratio)! * 100, 0)}%` });
  return facts;
}

// ---------------------------------------------------------------------------- visual

export interface CameraScoreRow {
  key: string;
  camera: string;
  score: string;
  sharpness: string;
  exposure: string;
  integrity: string;
  frozen: string;
  status: string;
  low: boolean;
}

const short = (cam: string) => cam.split('.').pop() ?? cam;

export function visualRows(d: Details): CameraScoreRow[] {
  const V = E().visual;
  const per = (d.per_camera_detail ?? d.per_camera) as Record<string, unknown> | undefined;
  if (!per || typeof per !== 'object') return [];
  const padded = new Set((Array.isArray(d.padded_channels) ? d.padded_channels : []).map((c) => short(String(c))));
  const dead = new Set((((d.camera_liveness as Details | undefined)?.dead_or_padded as unknown[]) ?? []).map((c) => short(String(c))));
  return Object.entries(per)
    .map(([cam, v]) => {
      const o: Details = v && typeof v === 'object' ? (v as Details) : { score: v };
      const name = short(cam);
      const score = num(o.score);
      return {
        key: cam,
        camera: name,
        score: fmt(score, 3),
        sharpness: fmt(num(o.sharpness), 3),
        exposure: fmt(num(o.exposure), 3),
        integrity: fmt(num(o.integrity), 3),
        frozen: num(o.frozen_ratio) === null ? '—' : `${fmt(num(o.frozen_ratio)! * 100, 1)}%`,
        status: padded.has(name) ? V.padded : dead.has(name) ? V.dead : V.ok,
        low: score !== null && score < 0.6,
      };
    })
    .sort((a, b) => a.camera.localeCompare(b.camera));
}

// ---------------------------------------------------------------------------- sync

/** The episode's sync badge: v1's four readings, 疑似错位 for an annotation with only suspects. */
export function syncBadge(d: Details): { key: string; text: string; color: string } {
  const v = str(d.verdict) ?? '';
  const suspectOnly = v === 'annotated' && Array.isArray(d.suspect_cameras) && d.suspect_cameras.length > 0 && !(Array.isArray(d.flagged_cameras) && d.flagged_cameras.length);
  const key = suspectOnly ? 'suspect' : v;
  const color: Record<string, string> = { aligned: 'green', annotated: 'orange', suspect: 'orange', undecidable: 'arcoblue', misaligned_all: 'red' };
  return { key, text: E().sync.badge[key] ?? (v || '—'), color: color[key] ?? 'gray' };
}

export interface SyncCameraRow {
  key: string;
  camera: string;
  lag: string;
  peak: string;
  zero: string;
  trusted: string;
  label: string;
  text: string;
  flagged: boolean;
}

export function syncRows(d: Details): SyncCameraRow[] {
  const per = d.per_camera as Record<string, unknown> | undefined;
  if (!per || typeof per !== 'object') return [];
  const flagged = new Set([...(Array.isArray(d.flagged_cameras) ? d.flagged_cameras : []), ...(Array.isArray(d.suspect_cameras) ? d.suspect_cameras : [])].map(String));
  return Object.entries(per).map(([cam, v]) => {
    const r: Details = v && typeof v === 'object' ? (v as Details) : {};
    const diag = (r.diagnosis ?? {}) as Details;
    return {
      key: cam,
      camera: cam,
      lag: signed(num(r.lag_s)),
      peak: fmt(num(r.corr_peak)),
      zero: fmt(num(r.corr_at_zero)),
      trusted: r.trusted === true ? zh.common.yes : r.trusted === false ? zh.common.no : '—',
      label: str(diag.label) ?? '',
      text: str(diag.text) ?? str(r.note) ?? '',
      flagged: flagged.has(cam),
    };
  });
}

// ---------------------------------------------------------------------------- task_success

export interface TrailStep {
  layer: string;
  text: string;
  reached: boolean;
  tone?: 'good' | 'bad' | 'warn';
}

/**
 * The judgement trail (v1's 判定链): scoring → per-camera review → the label guard → the evidence
 * arbitration → the conclusion, each with what it said; the layers the episode never reached read
 * 未触发.
 */
export function taskTrail(d: Details, verdict: string): TrailStep[] {
  const T = E().task;
  const steps: TrailStep[] = [];
  const init = str(d.init_verdict) ?? str(d.verdict);
  if (init) {
    let t = T.init[init] ?? init;
    if (init === 'success' && typeof d.strong_score === 'boolean') t += d.strong_score ? T.strong : T.weak;
    steps.push({ layer: T.layers.probe, text: t, reached: true, tone: init === 'success' || init === 'recovery' ? 'good' : init === 'failure' ? 'bad' : 'warn' });
  } else {
    steps.push({ layer: T.layers.probe, text: T.notReached, reached: false });
  }
  const tally = str(d.review);
  const votes = d.cam_votes && typeof d.cam_votes === 'object' ? Object.values(d.cam_votes as Record<string, unknown>).map(String) : [];
  if (tally || votes.length) {
    const yes = votes.filter((v) => v === 'yes').length;
    const no = votes.filter((v) => v === 'no').length;
    const text = [tally ? T.tally[tally] ?? tally : '', votes.length ? `（${T.votes(yes, no, votes.length - yes - no)}）` : ''].join('');
    steps.push({ layer: T.layers.endstate, text, reached: true, tone: tally === 'yes' ? 'good' : tally === 'no' ? 'bad' : 'warn' });
  } else {
    steps.push({ layer: T.layers.endstate, text: T.notReached, reached: false });
  }
  const guard = d.label_check && typeof d.label_check === 'object' ? (d.label_check as Details) : null;
  if (guard) {
    const outcome = String(guard.outcome ?? '');
    const key = outcome.startsWith('error') ? 'error' : outcome;
    steps.push({ layer: T.layers.label_guard, text: T.guard[key] ?? outcome, reached: true, tone: key === 'different' || key === 'error' ? 'warn' : undefined });
  } else {
    steps.push({ layer: T.layers.label_guard, text: T.notReached, reached: false });
  }
  const arb = d.arbitration && typeof d.arbitration === 'object' ? (d.arbitration as Details) : null;
  if (arb) {
    const final = String(arb.final ?? arb.consensus ?? 'abstain');
    steps.push({ layer: T.layers.arbitration, text: T.arbitration(num(arb.n_effective) ?? 0, T.consensus[final] ?? final), reached: true, tone: final === 'yes' ? 'good' : final === 'no' ? 'bad' : 'warn' });
  } else {
    steps.push({ layer: T.layers.arbitration, text: T.notReached, reached: false });
  }
  const tone: Record<string, TrailStep['tone']> = { pass: 'good', fail: 'bad', abstain: 'warn', error: 'warn' };
  steps.push({ layer: T.layers.final, text: T.final[verdict] ?? verdict, reached: true, tone: tone[verdict] });
  return steps;
}

/** The judgement code's name (the report section's labels). */
export function judgementName(d: Details): string | null {
  const v = str(d.verdict);
  return v ? zh.sections.task.judgement[v] ?? v : null;
}
