// The planner (C2 plan.json) writes its gates, merge strategy and estimate notes in English, for
// the command line; plans already on disk keep them. The task page says them in Chinese (fourth
// round): the known note forms are translated here, anything else is shown as it is.
import { zh } from '../locales/zh';

const T = () => zh.taskDetail;

/** A VLM gate of a plan stage, e.g. `probe 64` -> 打分 64. */
export function planGateText(gate: string, n: number): string {
  return `${T().planGates[gate] ?? gate} ${n}`;
}

export function planMergeText(strategy: string): string {
  return T().planMerge[strategy] ?? strategy;
}

const ROUGH = /^rough estimate: every selected episode is assumed to pass the hard gates; ([\d.]+) s per request at (\d+%) gate use \(v1, ([\d-]+)\)$/;
const UNCOUNTED = /^no request model for \[(.*)\]; not counted$/;

/** One estimate note in Chinese; `name` gives a module id its Chinese name. */
export function planNoteText(note: string, name: (moduleId: string) => string): string {
  const rough = ROUGH.exec(note);
  if (rough) return T().planNoteRough(rough[1], rough[2], rough[3]);
  if (note === 'task_success arbitration and label-guard calls depend on the data and are not counted') return T().planNoteTaskSuccess;
  if (note === 'skill_profile text calls (taxonomy, label audit) are per dataset and not counted') return T().planNoteSkillProfile;
  const uncounted = UNCOUNTED.exec(note);
  if (uncounted) {
    const ids = [...uncounted[1].matchAll(/'([^']+)'/g)].map((m) => m[1]);
    if (ids.length) return T().planNoteUncounted(ids.map(name).join('、'));
  }
  return note;
}
