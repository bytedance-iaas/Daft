// The adjudication rules as the page applies them (06 §5.1, F3.3). The server derives card status
// and checks every rule again; the page keeps the decisions made since the list was loaded on top
// of what the server sent, so a card does not jump away from under the user after each click.
import type { AdjudicationCard, AdjudicationLine, AdjudicationQuestion, DecisionValue } from '../api/types';

export interface EffectiveDecision {
  decision: DecisionValue;
  new_label: string | null;
  applied: boolean;
}

/** Decisions recorded in this page session, by `${episode}:${line}`. */
export type LocalDecisions = Record<string, EffectiveDecision>;

export const decisionKey = (ep: number, line: AdjudicationLine): string => `${ep}:${line}`;

export type CardStatus = AdjudicationCard['status'];

export interface CardView {
  ep: number;
  questions: (AdjudicationQuestion & { effective: EffectiveDecision | null })[];
  sources: string[];
  /** A 「整条弃用」 on any line: it overrides every verdict on the card (rule 1). */
  discarded: boolean;
  /** Where a new 整条弃用 is recorded: the first question's line. */
  discardLine: AdjudicationLine;
  /** The line currently holding 整条弃用 (withdrawing it records 拿不准 there: C4 has no «clear»). */
  discardOn: AdjudicationLine | null;
  /** The new task text when the label was changed (adopted or rewritten). */
  newLabel: string | null;
  hasVerdictQuestion: boolean;
  /** success / failure given by a person; ignored when discarded (rule 1). */
  humanVerdict: 'success' | 'failure' | null;
  /** Relabelled without a human verdict: executing re-runs task_success on the new label (rule 4). */
  rerunsModel: boolean;
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

export function viewCard(card: AdjudicationCard, local: LocalDecisions = {}): CardView {
  const questions = card.questions.map((q) => ({ ...q, effective: effectiveOf(card, q.line, local) }));
  const lines: AdjudicationLine[] = ['label', 'task_verdict', 'reject_appeal'];
  // A verdict may be given on a card without a verdict question (the optional verdict after a relabel).
  const byLine = new Map(lines.map((l) => [l, effectiveOf(card, l, local)] as const));
  const all = [...byLine.values()].filter((d): d is EffectiveDecision => Boolean(d));
  const discard = all.find((d) => d.decision === 'discard') ?? null;
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
      : [{ line: lines.find((l) => byLine.get(l)?.decision === 'discard')!, decision: 'discard' as const, new_label: null }]
    : lines
        .map((line) => ({ line, d: byLine.get(line) }))
        .filter((x): x is { line: AdjudicationLine; d: EffectiveDecision } => Boolean(x.d) && !x.d!.applied && x.d!.decision !== 'unsure')
        .map((x) => ({ line: x.line, decision: x.d.decision, new_label: x.d.new_label }));

  return {
    ep: card.episode_index,
    questions,
    sources,
    discarded: Boolean(discard),
    discardLine: card.questions[0].line,
    discardOn: lines.find((l) => byLine.get(l)?.decision === 'discard') ?? null,
    newLabel,
    hasVerdictQuestion: card.questions.some((q) => q.line === 'task_verdict'),
    humanVerdict,
    rerunsModel: Boolean(newLabel) && !humanVerdict && !discard,
    status,
    unapplied,
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
