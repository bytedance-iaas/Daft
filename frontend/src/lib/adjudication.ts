// The adjudication rules as the page applies them (06 §5.1, F3.3). The server derives card status
// and checks every rule again; the page keeps the decisions made since the list was loaded on top
// of what the server sent, so a card does not jump away from under the user after each click.
//
// Review kinds come from the registry's review_lines catalog (D43): its titles, which decisions a
// line offers and whether an open item counts as pending. task_verdict, reject_appeal and eef_check
// have dedicated views on the page; any other line is rendered from its catalog entry. One card asks
// one question per line, and a task verdict may carry the task text the person rewrote (`new_label`).
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

/** The lines with a dedicated view on the adjudication page. */
export const KNOWN_LINES: readonly string[] = ['task_verdict', 'reject_appeal', 'eef_check'];

/** The one line whose answer may carry a rewritten task text. */
export const RELABEL_LINE = 'task_verdict';

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
  return l ? l.decisions.some((d) => d.const === 'discard') : line === 'task_verdict';
}

export interface CardView {
  ep: number;
  /** One question per line, as the card asks them. */
  questions: (AdjudicationQuestion & { effective: EffectiveDecision | null })[];
  sources: string[];
  /** A 「整条弃用」 on any line: it overrides every verdict on the card (rule 1). */
  discarded: boolean;
  /** Where a new 整条弃用 is recorded: the first question whose line offers it; null = no discard here. */
  discardLine: AdjudicationLine | null;
  /** The line currently holding 整条弃用 (withdrawing it records 拿不准 there: C4 has no «clear»). */
  discardOn: AdjudicationLine | null;
  /** The task text the person rewrote while judging (`new_label` of the task verdict answer). */
  newLabel: string | null;
  hasVerdictQuestion: boolean;
  /** success / failure given by a person; ignored when discarded (rule 1). */
  humanVerdict: 'success' | 'failure' | null;
  /** Relabelled without a human verdict: executing re-runs task_success on the new label (rule 4). */
  rerunsModel: boolean;
  /** Every question is on a line that does not count as pending: a person may act (appeals). */
  optional: boolean;
  status: CardStatus;
  /** Decisions 执行裁决 would apply: not applied yet, and not a bare 拿不准 (rule 3; one that
   * rewrote the task text is executed, because the episode is judged again under it). */
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

export function viewCard(card: AdjudicationCard, local: LocalDecisions = {}, catalog?: ReviewCatalog): CardView {
  const own = card.questions;
  const questions = own.map((q) => ({ ...q, effective: effectiveOf(card.episode_index, q, local) }));
  const lines = [...new Set(own.map((q) => q.line))];
  const byLine = new Map<string, EffectiveDecision | null>(questions.map((q) => [q.line, q.effective] as const));
  const discardOn = lines.find((l) => byLine.get(l)?.decision === 'discard') ?? null;
  const discard = discardOn ? byLine.get(discardOn)! : null;
  const verdict = byLine.get(RELABEL_LINE) ?? null;
  const newLabel = (verdict?.new_label ?? null) || null;
  const humanVerdict = !discard && verdict && (verdict.decision === 'success' || verdict.decision === 'failure') ? verdict.decision : null;
  const sources = [...new Set(own.map((q) => q.source_module))];

  const unapplied = discard
    ? discard.applied
      ? []
      : [{ line: discardOn!, decision: 'discard', new_label: null }]
    : lines
        .map((line) => ({ line, d: byLine.get(line) ?? null }))
        .filter((x): x is { line: AdjudicationLine; d: EffectiveDecision } => Boolean(x.d) && !x.d!.applied && (x.d!.decision !== 'unsure' || Boolean(x.d!.new_label)))
        .map((x) => ({ line: x.line, decision: x.d.decision, new_label: x.d.new_label }));

  // Rule 1 (discard) first, then rule 3 (拿不准 keeps the card), then every question answered;
  // an answer carrying a rewritten task text decides the card too - executing judges it again.
  let status: CardStatus;
  if (discard) status = discard.applied ? 'applied' : 'decided';
  else if (questions.some((q) => q.effective?.decision === 'unsure')) status = 'unsure';
  else if (questions.every((q) => q.effective)) status = questions.every((q) => q.effective!.applied) && !unapplied.length ? 'applied' : 'decided';
  else if (unapplied.some((u) => u.new_label)) status = 'decided';
  else status = 'pending';

  return {
    ep: card.episode_index,
    questions,
    sources,
    discarded: Boolean(discard),
    discardLine: own.find((q) => offersDiscard(catalog, q.line))?.line ?? null,
    discardOn,
    newLabel,
    hasVerdictQuestion: own.some((q) => q.line === RELABEL_LINE),
    humanVerdict,
    rerunsModel: Boolean(newLabel) && !humanVerdict && !discard,
    optional: own.every((q) => !countsAsPending(catalog, q.line)),
    status,
    unapplied,
  };
}

/** The answer in force on a line of the card. */
export function answerOn(view: CardView, line: string): EffectiveDecision | null {
  return view.questions.find((q) => q.line === line)?.effective ?? null;
}

/**
 * Whether recording `decision` would only repeat the answer in force, the rewritten task text
 * included. The server keeps the latest row per line, so a repeat is still a new answer; the page
 * does not send one.
 */
export function repeats(current: EffectiveDecision | null, decision: string, newLabel: string | null = null): boolean {
  if (!current || current.decision !== decision) return false;
  return (current.new_label ?? null) === newLabel;
}

export interface ApplySummary {
  /** Episodes with something to apply. */
  episodes: number;
  /** Decisions that will be applied (拿不准 never is). */
  decisions: number;
  /** Episodes with a rewritten task text, judged again by task_success (no human verdict). */
  rerun: number;
  /** Episodes whose rewritten text came with a person's verdict: not judged again (rule 4). */
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
 * status the server sent, not this session's clicks, so a card never vanishes right after a click.
 */
export function keepCard(card: AdjudicationCard, f: { sources: readonly string[]; line: string; onlyUnsure: boolean }): boolean {
  const own = card.questions;
  if (f.sources.length && !own.some((q) => f.sources.includes(q.source_module))) return false;
  if (f.line && !own.some((q) => q.line === f.line)) return false;
  if (f.onlyUnsure && card.status !== 'unsure') return false;
  return true;
}
