// The mutable in-memory state behind the mock handlers. resetDb() re-seeds it (every test does).
import type {
  AdjudicationCard,
  Credential,
  DatasetDetail,
  Decision,
  LogLine,
  PreflightResult,
  ReviewLine,
  Subtask,
  Task,
  TaskListItem,
  TimelineEntry,
  VlmBackend,
} from '../api/types';
import {
  MAIN_TASK,
  SO101_TASK,
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
  so101Subtasks,
  so101Timeline,
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
  /** Review lines a test adds to the registry catalog (D43); the mock world itself has none. */
  extraReviewLines: ReviewLine[];
  /** Questions a test adds to a task's review tab: task id → episode → questions. */
  extraQuestions: Map<string, Map<number, AdjudicationCard['questions']>>;
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
  extraReviewLines: [],
  extraQuestions: new Map(),
};

export function resetDb(now: number = Date.now()): MockDb {
  db.now = now;
  db.credentials = seedCredentials(now);
  db.backends = seedBackends(now);
  db.datasets = seedDatasets(now);
  db.tasks = [...seedTasks(now), seedDeletedTask(now)];
  db.subtasks = new Map([
    [MAIN_TASK, mainSubtasks(now)],
    [SO101_TASK, so101Subtasks(now)],
  ]);
  db.timelines = new Map([
    [MAIN_TASK, mainTimeline(now)],
    [SO101_TASK, so101Timeline(now)],
  ]);
  db.logs = new Map([[MAIN_TASK, mainLogs(now)]]);
  db.decisions = new Map([[MAIN_TASK, seedDecisions(now)]]);
  db.preflights = new Map();
  db.idempotency = new Map();
  db.extraReviewLines = [];
  db.extraQuestions = new Map();
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

/**
 * A new record id the way the Daemon makes them since D45: `<prefix>-<9 lowercase letters>`.
 * The letters spell a counter (base 26), so the mock world stays deterministic; the seeded
 * records keep their older `<prefix>_…` ids, as real ones do.
 */
export function nextId(prefix: string): string {
  db.seq += 1;
  let n = db.seq;
  let letters = '';
  for (let i = 0; i < 9; i += 1) {
    letters = String.fromCharCode(97 + (n % 26)) + letters;
    n = Math.floor(n / 26);
  }
  return `${prefix}-${letters}`;
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

/** The registry's review_lines catalog as the mock serves it (plus lines a test added). */
export function reviewCatalog(): ReviewLine[] {
  return [...registry.review_lines, ...db.extraReviewLines];
}

function questionsFor(taskId: string, tab: 'review' | 'appeals'): Map<number, AdjudicationCard['questions']> {
  const out = baseQuestionsFor(taskId, tab);
  if (tab === 'review') {
    for (const [ep, qs] of db.extraQuestions.get(taskId) ?? []) out.set(ep, [...(out.get(ep) ?? []), ...qs]);
  }
  return out;
}

function baseQuestionsFor(taskId: string, tab: 'review' | 'appeals'): Map<number, AdjudicationCard['questions']> {
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

export function latest(taskId: string, ep: number, line: Decision['line']): Decision | null {
  const all = decisionsOf(taskId).filter((d) => d.episode_index === ep && d.line === line);
  return all.length ? all[all.length - 1] : null;
}

/** Why the Daemon says a card gained its follow-up verdict (results/catalog.py FOLLOW_UP_REASONS). */
export const FOLLOW_UP_REASON = '改标之后可以一并判成败（选填）：判了就以人的结论为准，不再按新标注重判；不判则按新标注重新判定';

type Question = AdjudicationCard['questions'][number];

/**
 * The follow-up a card's answers opened (registry follow_ups, C4 1.5.1): the line it asks, when
 * the answer in force on a line the card asks itself is one of `after` and the card does not ask it.
 */
export function openFollowUp(taskId: string, ep: number, own: Question[], line: string, answers: (l: string) => string | undefined = (l) => latest(taskId, ep, l)?.decision) {
  if (own.some((q) => q.line === line)) return null;
  for (const q of own) {
    const f = reviewCatalog().find((l) => l.id === q.line)?.follow_ups?.find((x) => x.line === line);
    const opening = answers(q.line);
    if (f && opening && f.after.includes(opening)) return { followUp: f, opener: q };
  }
  return null;
}

/**
 * A follow-up's answer that stands (the Daemon's Queue._stands): one of the follow-up's decisions,
 * given after the answer that opened it. Any newer answer on the opening line makes it lapse.
 */
function standingAnswer(taskId: string, ep: number, line: string, opened: NonNullable<ReturnType<typeof openFollowUp>>): Decision | null {
  const answer = latest(taskId, ep, line);
  const opening = latest(taskId, ep, opened.opener.line);
  return answer && opening && answer.id > opening.id && opened.followUp.decisions.includes(answer.decision) ? answer : null;
}

/** The Daemon's card_status: optional follow-ups never block 已裁, but their answers must be applied. */
function cardStatus(required: (Decision | null)[], extra: Decision[]): AdjudicationCard['status'] {
  const discard = required.find((d) => d?.decision === 'discard');
  if (discard) return discard.applied ? 'applied' : 'decided'; // rule 1
  if (required.some((d) => d?.decision === 'unsure')) return 'unsure'; // rule 3
  if (required.every((d) => d)) return [...required, ...extra].every((d) => d!.applied) ? 'applied' : 'decided';
  // Rule 4: a relabel not executed yet decides the card; executing re-judges it.
  if (required.some((d) => d && (d.decision === 'adopt_suggestion' || d.decision === 'custom_label') && !d.applied)) return 'decided';
  return 'pending';
}

/**
 * Cards with their latest decisions and derived status (pending / decided / unsure / applied).
 * Like the Daemon (C4 1.5.2): an open follow-up is listed on the card with `follow_up_of`, the
 * source module of the question that opened it and only an answer that stands; once the opening
 * answer changes, it is left out.
 */
export function cardsOf(taskId: string, tab: 'review' | 'appeals'): AdjudicationCard[] {
  const qs = questionsFor(taskId, tab);
  const cards: AdjudicationCard[] = [];
  const followUpLines = new Set(reviewCatalog().flatMap((l) => (l.follow_ups ?? []).map((f) => f.line)));
  for (const [ep, own] of qs) {
    const asked: Question[] = own.map((q) => ({ ...q, follow_up_of: null, latest_decision: latest(taskId, ep, q.line) }));
    const gained: Question[] = [];
    for (const line of followUpLines) {
      const opened = openFollowUp(taskId, ep, own, line);
      if (!opened) continue;
      const o = opened.opener;
      gained.push({ line, source_module: o.source_module, reason: FOLLOW_UP_REASON, annotation: o.annotation, follow_up_of: o.line, latest_decision: standingAnswer(taskId, ep, line, opened) });
    }
    const extra = gained.map((q) => q.latest_decision).filter((d): d is Decision => Boolean(d) && d!.decision !== 'unsure');
    cards.push({ episode_index: ep, status: cardStatus(asked.map((q) => q.latest_decision ?? null), extra), questions: [...asked, ...gained] });
  }
  return cards;
}

/** What 执行裁决 hands to the CLI (the Daemon's Queue.executable): standing answers, lapsed ones left out. */
export function executable(taskId: string): Decision[] {
  const cards = [...cardsOf(taskId, 'review'), ...cardsOf(taskId, 'appeals')];
  return cards.flatMap((c) => c.questions.map((q) => q.latest_decision).filter((d): d is Decision => Boolean(d) && !d!.applied));
}

/**
 * AdjudicationCounts as C4 1.5 spells them out: cards (episodes) over the whole task; pending and
 * decided only cover cards with a question on a line that counts as pending (appeals never do);
 * unapplied covers both tabs.
 */
export function countsOf(taskId: string): { decided: number; pending: number; unapplied: number } {
  const catalog = reviewCatalog();
  const countsAsPending = (line: string) => catalog.find((l) => l.id === line)?.counts_as_pending ?? line !== 'reject_appeal';
  const all = [...cardsOf(taskId, 'review'), ...cardsOf(taskId, 'appeals')];
  // An optional follow-up never makes a card count as pending.
  const counted = all.filter((c) => c.questions.some((q) => !q.follow_up_of && countsAsPending(q.line)));
  const decided = counted.filter((c) => c.status === 'decided' || c.status === 'applied').length;
  const pending = counted.length - decided;
  const unapplied = all.filter((c) => c.questions.some((q) => q.latest_decision && !q.latest_decision.applied && q.latest_decision.decision !== 'unsure')).length;
  return { decided, pending, unapplied };
}
