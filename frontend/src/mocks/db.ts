// The mutable in-memory state behind the mock handlers. resetDb() re-seeds it (every test does).
import type { AdjudicationCard, Credential, DatasetDetail, Decision, Declaration, LogLine, PreflightResult, ResultRecord, ReviewLine, Subtask, Task, TaskListItem, TimelineEntry, VizDisplayConfig, VizMapping, VizTemplate, VlmBackend } from '../api/types';
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
import { FINDINGS_TASK, findingsTask, withCodes } from './findings';
import { seedVizMappings } from './vizWorld';

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
  /** Module records a test adds to a task's episode views: task id → episode → module → record. */
  extraRecords: Map<string, Map<number, Record<string, ResultRecord>>>;
  /** design doc 18: the confirmed mcap field mappings, by dataset id (C7, versioned). */
  vizMappings: Map<string, { mapping: VizMapping; version: number; updatedAt: number }>;
  /** design doc 18 §6: the site's own mapping templates (the built-ins are code). */
  vizTemplates: VizTemplate[];
  /** design doc 25 §3: the confirmed dataset declarations, by dataset id (C7, versioned). */
  declarations: Map<string, { doc: Declaration; version: number; updatedAt: number }>;
  /** design doc 18 §4.2: transcodes that were asked for once (the next ask finds them ready). */
  vizTranscodes: Set<string>;
  /** depth packs asked for once (the first ask answers 202, design doc 21 §5.3) */
  vizDepthPacks: Set<string>;
  /** design doc 21 §6: the registrations' display configurations (null config: restored) */
  vizDisplays: Map<string, { config: VizDisplayConfig | null; version: number; updatedAt: number }>;
  /** POST /uploads: the uploads by id, as the Daemon keeps them. */
  uploads: Map<string, Record<string, unknown>>;
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
  extraRecords: new Map(),
  vizMappings: new Map(),
  vizTemplates: [],
  declarations: new Map(),
  vizTranscodes: new Set(),
  vizDepthPacks: new Set(),
  vizDisplays: new Map(),
  uploads: new Map(),
};

export function resetDb(now: number = Date.now()): MockDb {
  db.now = now;
  db.credentials = seedCredentials(now);
  db.backends = seedBackends(now);
  db.datasets = seedDatasets(now);
  db.tasks = [...seedTasks(now), findingsTask(now), seedDeletedTask(now)];
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
  db.extraRecords = new Map();
  db.vizMappings = seedVizMappings(now);
  db.vizTemplates = [];
  db.declarations = new Map();
  db.vizTranscodes = new Set();
  db.vizDepthPacks = new Set();
  db.vizDisplays = new Map();
  db.uploads = new Map();
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
  if (taskId === FINDINGS_TASK) return withCodes(tab === 'review' ? baseQuestions() : appealQuestions());
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

type Question = AdjudicationCard['questions'][number];

/** The Daemon's card_status (one card, one question per line). */
function cardStatus(answers: (Decision | null)[]): AdjudicationCard['status'] {
  const discard = answers.find((d) => d?.decision === 'discard');
  if (discard) return discard.applied ? 'applied' : 'decided'; // rule 1
  if (answers.some((d) => d?.decision === 'unsure')) return 'unsure'; // rule 3
  if (answers.every((d) => d)) return answers.every((d) => d!.applied) ? 'applied' : 'decided';
  // Rule 4: an answer carrying a rewritten task text decides the card; executing judges it again.
  if (answers.some((d) => d && d.new_label && !d.applied)) return 'decided';
  return 'pending';
}

/** Cards with their latest decisions and derived status (pending / decided / unsure / applied). */
export function cardsOf(taskId: string, tab: 'review' | 'appeals'): AdjudicationCard[] {
  const cards: AdjudicationCard[] = [];
  for (const [ep, own] of questionsFor(taskId, tab)) {
    const asked: Question[] = own.map((q) => ({ ...q, latest_decision: latest(taskId, ep, q.line) }));
    cards.push({ episode_index: ep, status: cardStatus(asked.map((q) => q.latest_decision ?? null)), questions: asked });
  }
  return cards;
}

/** What 执行裁决 hands to the CLI (the Daemon's Queue.executable): every standing answer. */
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
  const counted = all.filter((c) => c.questions.some((q) => countsAsPending(q.line)));
  const decided = counted.filter((c) => c.status === 'decided' || c.status === 'applied').length;
  const pending = counted.length - decided;
  // A 拿不准 changes nothing (rule 3) unless it rewrote the task text: that is judged again.
  const unapplied = all.filter((c) => c.questions.some((q) => q.latest_decision && !q.latest_decision.applied && (q.latest_decision.decision !== 'unsure' || q.latest_decision.new_label))).length;
  return { decided, pending, unapplied };
}
