import { describe, expect, it } from 'vitest';
import type { AdjudicationCard, AdjudicationQuestion, Decision } from '../api/types';
import { applySummary, decisionKey, keepCard, statusQuery, viewCard } from './adjudication';

const labelQ: AdjudicationQuestion = {
  line: 'label',
  source_module: 'skill_profile',
  reason: '标注与画面归入不同技能族',
  annotation: 'Put the orange thing in the can',
  caption: 'pour rice into the green bowl',
  suggestion: 'pour rice into the green bowl',
  priority: '重点',
  latest_decision: null,
};
const verdictQ: AdjudicationQuestion = { line: 'task_verdict', source_module: 'task_success', reason: '证据不足，弃权', annotation: 'Put the orange thing in the can', latest_decision: null };

const decided = (line: Decision['line'], decision: Decision['decision'], applied = false, new_label: string | null = null): Decision => ({
  id: 1,
  episode_index: 29,
  line,
  decision,
  new_label,
  note: null,
  decided_by: 'galbot',
  decided_at: 1,
  applied,
});

const card = (questions: AdjudicationQuestion[], status: AdjudicationCard['status'] = 'pending'): AdjudicationCard => ({ episode_index: 29, status, questions });

describe('adjudication rules (06 §5.1)', () => {
  it('rule 1: 整条弃用 overrides the verdict and decides the card', () => {
    const v = viewCard(card([labelQ, verdictQ]), {
      [decisionKey(29, 'task_verdict')]: { decision: 'success', new_label: null, applied: false },
      [decisionKey(29, 'label')]: { decision: 'discard', new_label: null, applied: false },
    });
    expect(v.discarded).toBe(true);
    expect(v.humanVerdict).toBeNull();
    expect(v.rerunsModel).toBe(false);
    expect(v.status).toBe('decided');
    // Only the discard is executed; the contradictory 判成功 is not.
    expect(v.unapplied).toEqual([{ line: 'label', decision: 'discard', new_label: null }]);
  });

  it('rule 3: 拿不准 is an answer that keeps the card pending and is never executed', () => {
    const v = viewCard(card([verdictQ]), { [decisionKey(29, 'task_verdict')]: { decision: 'unsure', new_label: null, applied: false } });
    expect(v.status).toBe('unsure');
    expect(v.unapplied).toEqual([]);
  });

  it('rule 4: a relabel re-runs task_success unless a person gave the verdict', () => {
    const relabel = { [decisionKey(29, 'label')]: { decision: 'adopt_suggestion' as const, new_label: null, applied: false } };
    const v = viewCard(card([labelQ, verdictQ]), relabel);
    expect(v.newLabel).toBe('pour rice into the green bowl');
    expect(v.rerunsModel).toBe(true);
    expect(v.status).toBe('decided');
    const w = viewCard(card([labelQ, verdictQ]), { ...relabel, [decisionKey(29, 'task_verdict')]: { decision: 'failure', new_label: null, applied: false } });
    expect(w.rerunsModel).toBe(false);
    expect(w.humanVerdict).toBe('failure');
    // C4 1.5: a decision answers a question the card has; a verdict without one is not part of the card.
    const x = viewCard(card([labelQ]), { ...relabel, [decisionKey(29, 'task_verdict')]: { decision: 'success', new_label: null, applied: false } });
    expect(x.hasVerdictQuestion).toBe(false);
    expect(x.rerunsModel).toBe(true);
    expect(x.unapplied.map((u) => u.line)).toEqual(['label']);
  });

  it('a rewritten label carries the new text; keeping the label leaves an open verdict pending', () => {
    const v = viewCard(card([labelQ, verdictQ]), { [decisionKey(29, 'label')]: { decision: 'custom_label', new_label: 'pour rice', applied: false } });
    expect(v.newLabel).toBe('pour rice');
    const w = viewCard(card([labelQ, verdictQ]), { [decisionKey(29, 'label')]: { decision: 'keep_label', new_label: null, applied: false } });
    expect(w.status).toBe('pending');
  });

  it('server decisions are used when nothing changed in this session; applied ones are not executed again', () => {
    const v = viewCard(card([{ ...verdictQ, latest_decision: decided('task_verdict', 'success', true) }], 'applied'));
    expect(v.status).toBe('applied');
    expect(v.unapplied).toEqual([]);
    expect(v.sources).toEqual(['task_success']);
  });

  it('summarises what 执行裁决 applies and which relabels are judged again (D39)', () => {
    const relabel = { decision: 'adopt_suggestion' as const, new_label: null, applied: false };
    const a = viewCard({ episode_index: 1, status: 'decided', questions: [labelQ, verdictQ] }, { [decisionKey(1, 'label')]: relabel });
    const b = viewCard({ episode_index: 2, status: 'decided', questions: [labelQ, verdictQ] }, {
      [decisionKey(2, 'label')]: relabel,
      [decisionKey(2, 'task_verdict')]: { decision: 'success', new_label: null, applied: false },
    });
    const c = viewCard({ episode_index: 3, status: 'pending', questions: [verdictQ] }, { [decisionKey(3, 'task_verdict')]: { decision: 'unsure', new_label: null, applied: false } });
    const d = viewCard({ episode_index: 4, status: 'decided', questions: [labelQ, verdictQ] }, { [decisionKey(4, 'label')]: { decision: 'discard', new_label: null, applied: false } });
    const s = applySummary([a, b, c, d]);
    expect([s.episodes, s.decisions, s.rerun, s.humanJudged]).toEqual([3, 4, 1, 1]);
    expect(s.views.map((v) => v.ep)).toEqual([1, 2, 4]);
  });

  it('maps the status filter onto the C4 query and client-side filters', () => {
    expect(statusQuery('unsure')).toEqual({ status: 'pending', onlyUnsure: true });
    expect(statusQuery('unapplied')).toEqual({ status: 'unapplied', onlyUnsure: false });
    expect(statusQuery('whatever')).toEqual({ status: 'pending', onlyUnsure: false });
    const c = card([labelQ, verdictQ], 'unsure');
    expect(keepCard(c, { sources: ['task_success', 'dedup'], line: '', onlyUnsure: false })).toBe(true);
    expect(keepCard(c, { sources: ['dedup'], line: '', onlyUnsure: false })).toBe(false);
    expect(keepCard(c, { sources: [], line: 'reject_appeal', onlyUnsure: false })).toBe(false);
    expect(keepCard(c, { sources: [], line: '', onlyUnsure: true })).toBe(true);
    expect(keepCard({ ...c, status: 'pending' }, { sources: [], line: '', onlyUnsure: true })).toBe(false);
  });
});
