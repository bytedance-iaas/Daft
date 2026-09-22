// The adjudication rules as the page applies them (06 §5.1, F3.3). The server derives card status
// and checks every rule again; the page keeps the decisions made since the list was loaded on top
// of what the server sent, so a card does not jump away from under the user after each click.
//
// Review kinds come from the registry's review_lines catalog (D43): its titles, which decisions a
// line offers and whether an open item counts as pending. label, task_verdict and reject_appeal
// have dedicated views on the page; any other line is rendered from its catalog entry.
import type { AdjudicationCard, AdjudicationLine, AdjudicationQuestion, Decision, DecisionValue, ReviewLine } from '../api/types';
import { zh } from '../locales/zh';

export interface EffectiveDecision {
  decision: DecisionValue;
  new_label: string | null;
  applied: boolean;
  /** Clicks of this page session are numbered in order; absent on a decision the server sent. */
  seq?: number;
}

/** Decisions recorded in this page session, by `${episode}:${line}`. */
export type LocalDecisions = Record<string, EffectiveDecision>;

export const decisionKey = (ep: number, line: AdjudicationLine): string => `${ep}:${line}`;

let clicks = 0;

/** A decision clicked in this page session, numbered after every earlier click. */
export function clicked(decision: DecisionValue, new_label: string | null = null): EffectiveDecision {
  clicks += 1;
  return { decision, new_label, applied: false, seq: clicks };
}

export type CardStatus = AdjudicationCard['status'];

export type ReviewCatalog = readonly ReviewLine[];

type FollowUpSpec = NonNullable<ReviewLine['follow_ups']>[number];

/** The lines with a dedicated view on the adjudication page. */
export const KNOWN_LINES: readonly string[] = ['label', 'task_verdict', 'reject_appeal'];

/** The task verdict a follow-up asks when the catalog does not say (v1's optional verdict). */
const VERDICT_FOLLOW_UP: readonly string[] = ['success', 'failure', 'unsure'];

export function catalogLine(catalog: ReviewCatalog | undefined, line: string): ReviewLine | undefined {
  return catalog?.find((l) => l.id === line);
}

export function lineTitle(catalog: ReviewCatalog | undefined, line: string): string {
  return catalogLine(catalog, line)?.title_zh ?? zh.adjudication.lineName[line] ?? line;
}

/** A decision's button title: the catalog's, else the page's own words, else the raw value. */
export function decisionTitle(catalog: ReviewCatalog | undefined, line: string, decision: string): string {
  return catalogLine(catalog, line)?.decisions.find((d) => d.const === decision)?.title ?? zh.adjudication.decisionText[decision] ?? decision;
}

/** The decisions a line offers, in catalog order; `fallback` when the catalog does not know it. */
export function lineDecisions(catalog: ReviewCatalog | undefined, line: string, fallback: readonly string[] = []): { const: string; title: string }[] {
  const l = catalogLine(catalog, line);
  if (l) return l.decisions.map((d) => ({ const: d.const, title: d.title }));
  return fallback.map((d) => ({ const: d, title: decisionTitle(catalog, line, d) }));
}

/** Whether an open item on this line must be decided (appeals are optional). */
export function countsAsPending(catalog: ReviewCatalog | undefined, line: string): boolean {
  const l = catalogLine(catalog, line);
  return l ? l.counts_as_pending : line !== 'reject_appeal';
}

/** Whether a line offers 「其它原因，整条弃用」. */
export function offersDiscard(catalog: ReviewCatalog | undefined, line: string): boolean {
  const l = catalogLine(catalog, line);
  return l ? l.decisions.some((d) => d.const === 'discard') : line === 'label' || line === 'task_verdict';
}

/**
 * A question the card gained as a follow-up of its answer on another line (C4 1.5.2
 * `follow_up_of`), not one of its own. A server before 1.5.2 does not say: such a question then
 * reads as the card's own, and the page offers follow-ups from the catalog alone.
 */
export function isFollowUp(q: AdjudicationQuestion): boolean {
  return q.follow_up_of !== null && q.follow_up_of !== undefined;
}

/** The questions a card asks itself: the ones its source modules raised. */
export function ownQuestions(card: AdjudicationCard): AdjudicationQuestion[] {
  return card.questions.filter((q) => !isFollowUp(q));
}

/**
 * A question a card gains after certain answers on another of its lines (registry follow_ups,
 * C4 1.5.1): v1's optional task verdict after adopting or rewriting a label. Only on cards that
 * do not ask that line themselves; never pending, never keeps a card from 已裁. Its answer
 * lapses — not shown, not applied, not counted — once the answer that opened it changes, even
 * to another answer that opens it again (the answer must be newer than the opening one).
 */
export interface FollowUpView {
  line: AdjudicationLine;
  /** The line whose answer opens it. */
  openedBy: AdjudicationLine;
  decisions: { const: string; title: string }[];
  optional: boolean;
  /** The answer in force on `openedBy` is one that opens it: the question is shown. */
  open: boolean;
  /** The answer that stands: given while open, after the opening answer; null when unanswered or lapsed. */
  effective: EffectiveDecision | null;
  /** The question as the server listed it (C4 1.5.2); null when only the catalog gives it. */
  question: AdjudicationQuestion | null;
  /** The server's record of `effective` when that is the answer in force (who, when). */
  decided: Decision | null;
}

export interface CardView {
  ep: number;
  /** The card's own questions; follow-ups are in `followUps`. */
  questions: (AdjudicationQuestion & { effective: EffectiveDecision | null })[];
  /** Follow-up questions of this card: the catalog's, and the ones the server listed; open or not. */
  followUps: FollowUpView[];
  sources: string[];
  /** A 「整条弃用」 on any line: it overrides every verdict on the card (rule 1). */
  discarded: boolean;
  /** Where a new 整条弃用 is recorded: the first question whose line offers it; null = no discard here. */
  discardLine: AdjudicationLine | null;
  /** The line currently holding 整条弃用 (withdrawing it records 拿不准 there: C4 has no «clear»). */
  discardOn: AdjudicationLine | null;
  /** The new task text when the label was changed (adopted or rewritten). */
  newLabel: string | null;
  hasVerdictQuestion: boolean;
  /** success / failure given by a person; ignored when discarded (rule 1). */
  humanVerdict: 'success' | 'failure' | null;
  /** Relabelled without a human verdict: executing re-runs task_success on the new label (rule 4). */
  rerunsModel: boolean;
  /** Every question is on a line that does not count as pending: a person may act (appeals). */
  optional: boolean;
  status: CardStatus;
  /** Decisions that 执行裁决 would apply (not applied yet, and not 拿不准 — rule 3). */
  unapplied: { line: AdjudicationLine; decision: DecisionValue; new_label: string | null }[];
}

function fromServer(d: Decision): EffectiveDecision {
  return { decision: d.decision, new_label: d.new_label ?? null, applied: d.applied };
}

function effectiveOf(ep: number, q: AdjudicationQuestion, local: LocalDecisions): EffectiveDecision | null {
  const l = local[decisionKey(ep, q.line)];
  if (l) return l;
  return q.latest_decision ? fromServer(q.latest_decision) : null;
}

/**
 * Whether `answer` was given after `opener`: clicks in their order, the server's decisions before
 * any click. Between two of the server's, the server has decided already: it lists a follow-up's
 * answer only while it stands (C4 1.5.2).
 */
function givenAfter(answer: EffectiveDecision, opener: EffectiveDecision): boolean {
  if (answer.seq === undefined) return opener.seq === undefined;
  return opener.seq === undefined || answer.seq > opener.seq;
}

/**
 * The card's follow-ups: the ones the catalog gives on lines it does not ask itself, opened by
 * lines it does (the rule for this session's clicks, and for a server before 1.5.2), merged with
 * the ones the server listed (`follow_up_of`), which carry the answers given before this session.
 */
function followUpsOf(card: AdjudicationCard, own: readonly AdjudicationQuestion[], local: LocalDecisions, catalog: ReviewCatalog | undefined, byLine: Map<string, EffectiveDecision | null>): FollowUpView[] {
  const asked = new Set(own.map((q) => q.line));
  const listed = card.questions.filter(isFollowUp);
  const out: FollowUpView[] = [];
  const add = (line: AdjudicationLine, openedBy: AdjudicationLine, spec: FollowUpSpec | undefined, q: AdjudicationQuestion | undefined) => {
    if (asked.has(line) || out.some((x) => x.line === line)) return;
    const opener = byLine.get(openedBy) ?? null;
    // Without the catalog's entry, the answer the server saw opening it is the one known to.
    const serverOpening = own.find((x) => x.line === openedBy)?.latest_decision?.decision;
    const after: readonly string[] = spec?.after ?? (serverOpening ? [serverOpening] : []);
    const decisions = spec
      ? spec.decisions.map((d) => ({ const: d, title: decisionTitle(catalog, line, d) }))
      : lineDecisions(catalog, line, line === 'task_verdict' ? VERDICT_FOLLOW_UP : []).filter((d) => d.const !== 'discard');
    const open = Boolean(opener && after.includes(opener.decision));
    const mine = local[decisionKey(card.episode_index, line)];
    const answer = mine ?? (q?.latest_decision ? fromServer(q.latest_decision) : null);
    const stands = Boolean(open && opener && answer && givenAfter(answer, opener) && decisions.some((d) => d.const === answer.decision));
    out.push({
      line,
      openedBy,
      decisions,
      optional: spec?.optional ?? true,
      open,
      effective: stands ? answer : null,
      question: q ?? null,
      decided: stands && !mine && q?.latest_decision ? q.latest_decision : null,
    });
  };
  for (const q of own) {
    for (const f of catalogLine(catalog, q.line)?.follow_ups ?? []) {
      add(f.line, q.line, f, listed.find((x) => x.line === f.line && x.follow_up_of === q.line));
    }
  }
  for (const q of listed) {
    const openedBy = q.follow_up_of!;
    add(q.line, openedBy, catalogLine(catalog, openedBy)?.follow_ups?.find((f) => f.line === q.line), q);
  }
  return out;
}

export function viewCard(card: AdjudicationCard, local: LocalDecisions = {}, catalog?: ReviewCatalog): CardView {
  const own = ownQuestions(card);
  const questions = own.map((q) => ({ ...q, effective: effectiveOf(card.episode_index, q, local) }));
  // C4 1.5: a decision answers a question the card has, or an open follow-up (1.5.1).
  const lines = [...new Set(own.map((q) => q.line))];
  const byLine = new Map<string, EffectiveDecision | null>(questions.map((q) => [q.line, q.effective] as const));
  const followUps = followUpsOf(card, own, local, catalog, byLine);
  const answeredFollowUps = followUps.filter((f) => f.effective);
  const discardOn = lines.find((l) => byLine.get(l)?.decision === 'discard') ?? null;
  const discard = discardOn ? byLine.get(discardOn)! : null;
  const label = byLine.get('label');
  const newLabel = label && (label.decision === 'adopt_suggestion' || label.decision === 'custom_label') ? label.new_label ?? own.find((q) => q.line === 'label')?.suggestion ?? null : null;
  // A person's verdict: the card's own task-verdict question, or the follow-up that asks it.
  const verdict = byLine.get('task_verdict') ?? answeredFollowUps.find((f) => f.line === 'task_verdict')?.effective ?? null;
  const humanVerdict = !discard && verdict && (verdict.decision === 'success' || verdict.decision === 'failure') ? verdict.decision : null;
  const sources = [...new Set(own.map((q) => q.source_module))];

  const unapplied = discard
    ? discard.applied
      ? []
      : [{ line: discardOn!, decision: 'discard', new_label: null }]
    : [...lines.map((line) => ({ line, d: byLine.get(line) ?? null })), ...answeredFollowUps.map((f) => ({ line: f.line, d: f.effective }))]
        .filter((x): x is { line: AdjudicationLine; d: EffectiveDecision } => Boolean(x.d) && !x.d!.applied && x.d!.decision !== 'unsure')
        .map((x) => ({ line: x.line, decision: x.d.decision, new_label: x.d.new_label }));

  // Status reads the card's own questions: a follow-up never makes it pending or unsure. An
  // answer on one must still be executed before the card is 已执行.
  let status: CardStatus;
  if (discard) status = discard.applied ? 'applied' : 'decided';
  else if (questions.some((q) => q.effective?.decision === 'unsure')) status = 'unsure';
  else if (questions.every((q) => q.effective)) status = questions.every((q) => q.effective!.applied) && !unapplied.length ? 'applied' : 'decided';
  else if (newLabel) status = 'decided';
  else status = 'pending';

  return {
    ep: card.episode_index,
    questions,
    followUps,
    sources,
    discarded: Boolean(discard),
    discardLine: own.find((q) => offersDiscard(catalog, q.line))?.line ?? null,
    discardOn,
    newLabel,
    hasVerdictQuestion: own.some((q) => q.line === 'task_verdict'),
    humanVerdict,
    rerunsModel: Boolean(newLabel) && !humanVerdict && !discard,
    optional: own.every((q) => !countsAsPending(catalog, q.line)),
    status,
    unapplied,
  };
}

/** The answer in force on a line of the card: its own question's, or a follow-up's that stands. */
export function answerOn(view: CardView, line: string): EffectiveDecision | null {
  return view.questions.find((q) => q.line === line)?.effective ?? view.followUps.find((f) => f.line === line)?.effective ?? null;
}

/**
 * Whether recording `decision` would only repeat the answer in force (a rewritten label's text
 * included). The server keeps the latest row per line, so a repeat is still a new answer, and a
 * new label answer makes the follow-up's answer lapse: the page does not send one.
 */
export function repeats(current: EffectiveDecision | null, decision: string, newLabel: string | null = null): boolean {
  if (!current || current.decision !== decision) return false;
  return decision !== 'custom_label' || (current.new_label ?? null) === newLabel;
}

export interface ApplySummary {
  /** Episodes with something to apply. */
  episodes: number;
  /** Decisions that will be applied (拿不准 never is). */
  decisions: number;
  /** Relabelled episodes judged again by task_success (no human verdict, not discarded). */
  rerun: number;
  /** Relabelled episodes a person already judged success/failure: not judged again (rule 4). */
  humanJudged: number;
  views: CardView[];
}

/** What 执行裁决 will do (07 §6, D39): counted from every card with unapplied decisions. */
export function applySummary(views: readonly CardView[]): ApplySummary {
  const pending = views.filter((v) => v.unapplied.length);
  return {
    episodes: pending.length,
    decisions: pending.reduce((n, v) => n + v.unapplied.length, 0),
    rerun: pending.filter((v) => v.rerunsModel).length,
    humanJudged: pending.filter((v) => Boolean(v.newLabel) && Boolean(v.humanVerdict) && !v.discarded).length,
    views: pending,
  };
}

/** The status filter of the page: the C4 query value plus the client-side part (拿不准). */
export function statusQuery(filter: string): { status: 'pending' | 'decided' | 'unapplied' | 'all'; onlyUnsure: boolean } {
  if (filter === 'unsure') return { status: 'pending', onlyUnsure: true };
  if (filter === 'decided' || filter === 'unapplied' || filter === 'all') return { status: filter, onlyUnsure: false };
  return { status: 'pending', onlyUnsure: false };
}

/**
 * Client-side filters: several source modules (C4 takes one), question type, 拿不准. They read the
 * status the server sent, not this session's clicks, so a card never vanishes right after a click;
 * and the card's own questions, so a follow-up does not change which filters a card matches.
 */
export function keepCard(card: AdjudicationCard, f: { sources: readonly string[]; line: string; onlyUnsure: boolean }): boolean {
  const own = ownQuestions(card);
  if (f.sources.length && !own.some((q) => f.sources.includes(q.source_module))) return false;
  if (f.line && !own.some((q) => q.line === f.line)) return false;
  if (f.onlyUnsure && card.status !== 'unsure') return false;
  return true;
}
