// The adjudication rules as the page applies them (06 §5.1, F3.3). The server derives card status
// and checks every rule again; the page keeps the decisions made since the list was loaded on top
// of what the server sent, so a card does not jump away from under the user after each click.
//
// Review kinds come from the registry's review_lines catalog (D43): its titles, which decisions a
// line offers and whether an open item counts as pending. label, task_verdict and reject_appeal
// have dedicated views on the page; any other line is rendered from its catalog entry.
import type { AdjudicationCard, AdjudicationLine, AdjudicationQuestion, DecisionValue, ReviewLine } from '../api/types';
import { zh } from '../locales/zh';

export interface EffectiveDecision {
  decision: DecisionValue;
  new_label: string | null;
  applied: boolean;
}

/** Decisions recorded in this page session, by `${episode}:${line}`. */
export type LocalDecisions = Record<string, EffectiveDecision>;

export const decisionKey = (ep: number, line: AdjudicationLine): string => `${ep}:${line}`;

export type CardStatus = AdjudicationCard['status'];

export type ReviewCatalog = readonly ReviewLine[];

/** The lines with a dedicated view on the adjudication page. */
export const KNOWN_LINES: readonly string[] = ['label', 'task_verdict', 'reject_appeal'];

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

export interface CardView {
  ep: number;
  questions: (AdjudicationQuestion & { effective: EffectiveDecision | null })[];
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

function effectiveOf(card: AdjudicationCard, line: AdjudicationLine, local: LocalDecisions): EffectiveDecision | null {
  const l = local[decisionKey(card.episode_index, line)];
  if (l) return l;
  const q = card.questions.find((x) => x.line === line);
  const d = q?.latest_decision;
  return d ? { decision: d.decision, new_label: d.new_label ?? null, applied: d.applied } : null;
}

export function viewCard(card: AdjudicationCard, local: LocalDecisions = {}, catalog?: ReviewCatalog): CardView {
  const questions = card.questions.map((q) => ({ ...q, effective: effectiveOf(card, q.line, local) }));
  // C4 1.5: a decision always answers a question the card has (anything else is 400).
  const lines = [...new Set(card.questions.map((q) => q.line))];
  const byLine = new Map(lines.map((l) => [l, effectiveOf(card, l, local)] as const));
  const discardOn = lines.find((l) => byLine.get(l)?.decision === 'discard') ?? null;
  const discard = discardOn ? byLine.get(discardOn)! : null;
  const label = byLine.get('label');
  const newLabel = label && (label.decision === 'adopt_suggestion' || label.decision === 'custom_label') ? label.new_label ?? card.questions.find((q) => q.line === 'label')?.suggestion ?? null : null;
  const verdict = byLine.get('task_verdict');
  const humanVerdict = !discard && verdict && (verdict.decision === 'success' || verdict.decision === 'failure') ? verdict.decision : null;
  const sources = [...new Set(card.questions.map((q) => q.source_module))];

  let status: CardStatus;
  if (discard) status = discard.applied ? 'applied' : 'decided';
  else if (questions.some((q) => q.effective?.decision === 'unsure')) status = 'unsure';
  else if (questions.every((q) => q.effective)) status = questions.every((q) => q.effective!.applied) ? 'applied' : 'decided';
  else if (newLabel) status = 'decided';
  else status = 'pending';

  const unapplied = discard
    ? discard.applied
      ? []
      : [{ line: discardOn!, decision: 'discard', new_label: null }]
    : lines
        .map((line) => ({ line, d: byLine.get(line) }))
        .filter((x): x is { line: AdjudicationLine; d: EffectiveDecision } => Boolean(x.d) && !x.d!.applied && x.d!.decision !== 'unsure')
        .map((x) => ({ line: x.line, decision: x.d.decision, new_label: x.d.new_label }));

  return {
    ep: card.episode_index,
    questions,
    sources,
    discarded: Boolean(discard),
    discardLine: card.questions.find((q) => offersDiscard(catalog, q.line))?.line ?? null,
    discardOn,
    newLabel,
    hasVerdictQuestion: card.questions.some((q) => q.line === 'task_verdict'),
    humanVerdict,
    rerunsModel: Boolean(newLabel) && !humanVerdict && !discard,
    optional: card.questions.every((q) => !countsAsPending(catalog, q.line)),
    status,
    unapplied,
  };
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
 * status the server sent, not this session's clicks, so a card never vanishes right after a click.
 */
export function keepCard(card: AdjudicationCard, f: { sources: readonly string[]; line: string; onlyUnsure: boolean }): boolean {
  if (f.sources.length && !card.questions.some((q) => f.sources.includes(q.source_module))) return false;
  if (f.line && !card.questions.some((q) => q.line === f.line)) return false;
  if (f.onlyUnsure && card.status !== 'unsure') return false;
  return true;
}
