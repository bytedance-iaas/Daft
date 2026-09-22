// The mutable in-memory state behind the mock handlers. resetDb() re-seeds it (every test does).
import type {
  AdjudicationCard,
  Credential,
  DatasetDetail,
  Decision,
  LogLine,
  PreflightResult,
  Subtask,
  Task,
  TaskListItem,
  TimelineEntry,
  VlmBackend,
} from '../api/types';
import {
  MAIN_TASK,
  appealQuestions,
  baseQuestions,
  mainLogs,
  mainSubtasks,
  mainTimeline,
  registry,
  seedBackends,
  seedCredentials,
  seedDatasets,
  seedDecisions,
  seedDeletedTask,
  seedTasks,
  taskText,
} from './world';

export interface StoredPreflight {
  id: string;
  expiresAt: number;
  input: { source: string; uri: string; region?: string; credential?: string | null; datasetId?: string };
  result: PreflightResult;
}

export interface MockDb {
  now: number;
  credentials: Credential[];
  backends: VlmBackend[];
  datasets: DatasetDetail[];
  tasks: Task[];
  subtasks: Map<string, Subtask[]>;
  timelines: Map<string, TimelineEntry[]>;
  logs: Map<string, LogLine[]>;
  decisions: Map<string, Decision[]>;
  preflights: Map<string, StoredPreflight>;
  /** Idempotency-Key → first response body and status (doc 03 §8). */
  idempotency: Map<string, { status: number; body: unknown }>;
  seq: number;
}

export const db: MockDb = {
  now: 0,
  credentials: [],
  backends: [],
  datasets: [],
  tasks: [],
  subtasks: new Map(),
  timelines: new Map(),
  logs: new Map(),
  decisions: new Map(),
  preflights: new Map(),
  idempotency: new Map(),
  seq: 1,
};

export function resetDb(now: number = Date.now()): MockDb {
  db.now = now;
  db.credentials = seedCredentials(now);
  db.backends = seedBackends(now);
  db.datasets = seedDatasets(now);
  db.tasks = [...seedTasks(now), seedDeletedTask(now)];
  db.subtasks = new Map([[MAIN_TASK, mainSubtasks(now)]]);
  db.timelines = new Map([[MAIN_TASK, mainTimeline(now)]]);
  db.logs = new Map([[MAIN_TASK, mainLogs(now)]]);
  db.decisions = new Map([[MAIN_TASK, seedDecisions(now)]]);
  db.preflights = new Map();
  db.idempotency = new Map();
  db.seq = 100;
  // Datasets know their tasks (newest first) and their last task.
  for (const d of db.datasets) {
    const refs = db.tasks
      .filter((t) => t.dataset_id === d.id && !t.deleted_at)
      .sort((a, b) => b.created_at - a.created_at)
      .map((t) => ({ id: t.id, name: t.name, state: t.state, created_at: t.created_at }));
    d.tasks = refs.slice(0, 20);
    d.last_task = refs[0] ?? null;
  }
  return db;
}

export function nextId(prefix: string): string {
  db.seq += 1;
  return `${prefix}_${db.seq.toString(36).toUpperCase().padStart(6, '0')}`;
}

export function clock(): number {
  return Math.max(Date.now(), db.now);
}

export function findTask(id: string): Task | undefined {
  return db.tasks.find((t) => t.id === id);
}

/** The dataset name shown in the task list. */
export function datasetName(t: Task): string {
  const d = t.dataset_id ? db.datasets.find((x) => x.id === t.dataset_id) : undefined;
  return d?.name ?? t.input.uri.replace(/\/+$/, '').split('/').pop() ?? t.input.uri;
}

export function toListItem(t: Task): TaskListItem {
  const selected = t.modules.filter((m) => m.selected);
  const counts: Record<string, number> = {};
  for (const m of selected) counts[m.state] = (counts[m.state] ?? 0) + 1;
  return {
    id: t.id,
    name: t.name,
    state: t.state,
    pause_reason: t.pause_reason ?? null,
    dataset: datasetName(t),
    dataset_id: t.dataset_id ?? null,
    created_at: t.created_at,
    progress: t.progress,
    summary: t.summary ?? null,
    pending_adjudication: t.pending_adjudication,
    delivery_stale: t.delivery_stale,
    active_subtask: t.active_subtask ? t.active_subtask.id : null,
    modules: registry.modules.map((m) => m.id).filter((id) => selected.some((s) => s.id === id)),
    module_counts: counts,
    usage: t.usage,
    deleted_at: t.deleted_at ?? null,
  };
}

// ------------------------------------------------------------------ adjudication

function questionsFor(taskId: string, tab: 'review' | 'appeals'): Map<number, AdjudicationCard['questions']> {
  if (taskId === MAIN_TASK) return tab === 'review' ? baseQuestions() : appealQuestions();
  const t = findTask(taskId);
  const out = new Map<number, AdjudicationCard['questions']>();
  if (!t || tab === 'appeals') return out;
  for (let i = 0; i < t.pending_adjudication; i += 1) {
    const ep = i * 5 + 1;
    out.set(ep, [
      {
        line: 'task_verdict',
        source_module: 'task_success',
        reason: '证据不足，弃权：打分层拿不准 → 复核分歧 → 转人工',
        annotation: taskText(ep).text,
        caption: null,
        suggestion: null,
        priority: null,
        latest_decision: null,
      },
    ]);
  }
  return out;
}

export function decisionsOf(taskId: string): Decision[] {
  if (!db.decisions.has(taskId)) db.decisions.set(taskId, []);
  return db.decisions.get(taskId)!;
}

function latest(taskId: string, ep: number, line: Decision['line']): Decision | null {
  const all = decisionsOf(taskId).filter((d) => d.episode_index === ep && d.line === line);
  return all.length ? all[all.length - 1] : null;
}

/** Cards with their latest decisions and derived status (pending / decided / unsure / applied). */
export function cardsOf(taskId: string, tab: 'review' | 'appeals'): AdjudicationCard[] {
  const qs = questionsFor(taskId, tab);
  const cards: AdjudicationCard[] = [];
  for (const [ep, questions] of qs) {
    const withDecisions = questions.map((q) => ({ ...q, latest_decision: latest(taskId, ep, q.line) }));
    const decs = withDecisions.map((q) => q.latest_decision);
    // «整条弃用» on any line decides the whole card (rule 1).
    const discarded = decs.some((d) => d?.decision === 'discard');
    const allApplied = decs.every((d) => d?.applied);
    let status: AdjudicationCard['status'];
    if (discarded) status = decs.find((d) => d?.decision === 'discard')!.applied ? 'applied' : 'decided';
    else if (decs.some((d) => d?.decision === 'unsure')) status = 'unsure';
    else if (decs.every((d) => d && d.decision !== 'unsure')) status = allApplied ? 'applied' : 'decided';
    else if (decs.some((d) => d && d.decision !== 'unsure')) {
      // A label answered but the verdict open: the label is enough to act on (the verdict is re-run).
      const label = withDecisions.find((q) => q.line === 'label')?.latest_decision;
      status = label && label.decision !== 'unsure' && label.decision !== 'keep_label' ? 'decided' : 'pending';
    } else status = 'pending';
    cards.push({ episode_index: ep, status, questions: withDecisions });
  }
  return cards;
}

export function countsOf(taskId: string): { decided: number; pending: number; unapplied: number } {
  const cards = cardsOf(taskId, 'review');
  const appeals = cardsOf(taskId, 'appeals');
  const decided = cards.filter((c) => c.status === 'decided' || c.status === 'applied').length;
  const pending = cards.length - decided;
  const unapplied = [...cards, ...appeals].filter((c) => c.questions.some((q) => q.latest_decision && !q.latest_decision.applied && q.latest_decision.decision !== 'unsure')).length;
  return { decided, pending, unapplied };
}
