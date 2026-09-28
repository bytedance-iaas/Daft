// A parameter file the preflight drafted from the dataset's metadata (C2 `drafts`, design doc 12 §8.7,
// D-E17), read for the person who confirms it: what each drafted part reads, what was assumed rather
// than read, and what was left out and why. The EEF module's record mapping is the first such file.
import type { PreflightResult } from '../api/types';
import { zh } from '../locales/zh';
import { availabilityOf } from './preflight';

type D = Record<string, unknown>;
const obj = (v: unknown): D => (v && typeof v === 'object' && !Array.isArray(v) ? (v as D) : {});
const str = (v: unknown): string => (typeof v === 'string' ? v : '');
const Z = () => zh.taskForm.draft;

export interface ParamDraftView {
  /** The file to upload when the person confirms it; null when nothing could be drafted. */
  document: D | null;
  /** One line per drafted part, e.g. 「关节角：observation.state.joint_position · franka_panda · rad」. */
  parts: string[];
  /** What was taken by convention, readable, in the order the server listed them. */
  assumptions: string[];
  /** What was left out and why, readable. */
  notDrafted: string[];
}

interface Note {
  code?: unknown;
  source?: unknown;
  args?: unknown;
}

function noteText(n: Note, texts: Record<string, (a: D) => string>): string {
  const code = str(n.code);
  const fn = texts[code];
  const text = fn ? fn(obj(n.args)) : code;
  const source = Z().source[str(n.source)];
  return source ? `${source}：${text}` : text;
}

function slice(v: unknown): string {
  return Array.isArray(v) && v.length === 2 ? Z().slice(Number(v[0]), Number(v[1])) : '';
}

function parts(document: D | null): string[] {
  const record = obj(obj(document).record);
  const out: string[] = [];
  const pose = obj(record.pose);
  if (Object.keys(pose).length) {
    const units = obj(pose.units);
    out.push(Z().poseLine(`${str(pose.key) || str(pose.topic)}${slice(pose.slice)}`, str(pose.layout), `${str(units.position)} / ${str(units.angle)}`, str(pose.frame_id) || null));
  }
  const joints = obj(record.joints);
  if (Object.keys(joints).length) out.push(Z().jointsLine(`${str(joints.key) || str(joints.topic)}${slice(joints.slice)}`, str(joints.robot), str(joints.units)));
  return out;
}

/** The draft of a module's parameter in a preflight result, or null when there is none. */
export function paramDraft(preflight: PreflightResult | null | undefined, moduleId: string, key: string): ParamDraftView | null {
  const raw = obj(obj(availabilityOf(preflight, moduleId)?.drafts)[key]);
  if (!Object.keys(raw).length) return null;
  const document = raw.document && typeof raw.document === 'object' ? (raw.document as D) : null;
  const notes = (v: unknown): Note[] => (Array.isArray(v) ? (v as Note[]) : []);
  return {
    document,
    parts: parts(document),
    assumptions: notes(raw.assumptions).map((n) => noteText(n, Z().assumption)),
    notDrafted: notes(raw.not_drafted).map((n) => noteText(n, Z().why)),
  };
}
