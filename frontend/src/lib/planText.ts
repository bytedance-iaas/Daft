// The planner (C2 plan.json) writes its gates, merge strategy and estimate notes in English, for
// the command line. The task page says them in Chinese (fourth round): the known note forms are
// translated here, anything else is shown as it is.
import { zh } from '../locales/zh';

const T = () => zh.taskDetail;

/** A VLM gate of a plan stage, e.g. `probe 64` -> 打分 64; a module's own gate keeps its name. */
export function planGateText(gate: string, n: number): string {
  return `${T().planGates[gate] ?? gate} ${n}`;
}

export function planMergeText(strategy: string): string {
  return T().planMerge[strategy] ?? strategy;
}

// the two blocks side by side (design doc 17 §3)
const ROUGH_BLOCKS = /^rough estimate: every selected episode goes through both blocks, which run side by side; ([\d.]+) s per request at (\d+%) gate use \(image-request baseline from v1, ([\d-]+); video latency is not calibrated\)$/;
const UNCOUNTED = /^no request model for \[(.*)\]; not counted$/;
// episodes without a task text are not judged by task_success (the caption pass is gone)
const UNLABELED = /^(\d+) selected episode\(s\) have no task text: task_success does not judge them \(their cameras are still checked for picture defects, one request each\), every other check still runs on them$/;

/** One estimate note in Chinese; `name` gives a module id its Chinese name. */
export function planNoteText(note: string, name: (moduleId: string) => string): string {
  const blocks = ROUGH_BLOCKS.exec(note);
  if (blocks) return T().planNoteRoughBlocks(blocks[1], blocks[2], blocks[3]);
  if (note === 'data_integrity: its decode test (decode_test), when on, is not counted - about one more decode of every frame') return T().planNoteDecode;
  const unlabeled = UNLABELED.exec(note);
  if (unlabeled) return T().planNoteUnlabeled(unlabeled[1]);
  const uncounted = UNCOUNTED.exec(note);
  if (uncounted) {
    const ids = [...uncounted[1].matchAll(/'([^']+)'/g)].map((m) => m[1]);
    if (ids.length) return T().planNoteUncounted(ids.map(name).join('、'));
  }
  return note;
}
