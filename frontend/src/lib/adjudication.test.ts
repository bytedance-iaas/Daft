import { describe, expect, it } from 'vitest';
import type { AdjudicationCard, AdjudicationQuestion, Decision, ReviewLine } from '../api/types';
import { answerOn, applySummary, clicked, countsAsPending, decisionKey, decisionTitle, keepCard, lineDecisions, lineTitle, offersDiscard, repeats, statusQuery, viewCard } from './adjudication';

/** The one question of a card whose task text the kill guard suspects (LABEL-4 on the verdict). */
const conflictQ: AdjudicationQuestion = {
  line: 'task_verdict',
  source_module: 'task_success',
  reason: '标注与画面对不上；证据不足，弃权',
  annotation: 'Put the orange thing in the can',
  caption: 'pour rice into the green bowl',
  suggestion: 'pour rice into the green bowl',
  priority: '重点',
  latest_decision: null,
};
const verdictQ: AdjudicationQuestion = { line: 'task_verdict', source_module: 'task_success', reason: '证据不足，弃权', annotation: 'Put the orange thing in the can', latest_decision: null };
const eefQ: AdjudicationQuestion = { line: 'eef_check', source_module: 'eef_video_consistency', reason: 'CPU 与模型不一致', latest_decision: null };

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

const answer = (decision: string, new_label: string | null = null) => ({ decision, new_label, applied: false });

describe('adjudication rules (06 §5.1)', () => {
  it('rule 1: 整条弃用 decides the card and nothing else is executed', () => {
    const v = viewCard(card([verdictQ, eefQ]), {
      [decisionKey(29, 'eef_check')]: answer('consistent'),
      [decisionKey(29, 'task_verdict')]: answer('discard'),
    });
    expect(v.discarded).toBe(true);
    expect(v.humanVerdict).toBeNull();
    expect(v.rerunsModel).toBe(false);
    expect(v.status).toBe('decided');
    expect(v.unapplied).toEqual([{ line: 'task_verdict', decision: 'discard', new_label: null }]);
  });

  it('rule 3: 拿不准 is an answer that keeps the card pending and is never executed', () => {
    const v = viewCard(card([verdictQ]), { [decisionKey(29, 'task_verdict')]: answer('unsure') });
    expect(v.status).toBe('unsure');
    expect(v.unapplied).toEqual([]);
  });

  it('rule 4: a rewritten task text is judged again unless the same answer concluded it', () => {
    const rewritten = { [decisionKey(29, 'task_verdict')]: answer('unsure', 'pour rice into the green bowl') };
    const v = viewCard(card([conflictQ]), rewritten);
    expect(v.newLabel).toBe('pour rice into the green bowl');
    expect(v.rerunsModel).toBe(true);
    expect(v.status).toBe('unsure');                      // still in the queue: the model judges it
    expect(v.unapplied).toEqual([{ line: 'task_verdict', decision: 'unsure', new_label: 'pour rice into the green bowl' }]);
    const w = viewCard(card([conflictQ]), { [decisionKey(29, 'task_verdict')]: answer('failure', 'pour rice') });
    expect([w.newLabel, w.rerunsModel, w.humanVerdict, w.status]).toEqual(['pour rice', false, 'failure', 'decided']);
  });

  it('an answer with a rewritten text decides a card whose other question is open', () => {
    const v = viewCard(card([verdictQ, eefQ]), { [decisionKey(29, 'task_verdict')]: answer('failure', 'pour rice') });
    expect([v.status, v.hasVerdictQuestion]).toEqual(['decided', true]);
    expect(v.unapplied.map((u) => u.line)).toEqual(['task_verdict']);
  });

  it('server decisions are used when nothing changed in this session; applied ones are not executed again', () => {
    const v = viewCard(card([{ ...verdictQ, latest_decision: decided('task_verdict', 'success', true) }], 'applied'));
    expect(v.status).toBe('applied');
    expect(v.unapplied).toEqual([]);
    expect(v.sources).toEqual(['task_success']);
    // the text in force comes from the answer the server sent
    const w = viewCard(card([{ ...conflictQ, latest_decision: decided('task_verdict', 'unsure', false, 'pour rice') }]));
    expect([w.newLabel, w.rerunsModel]).toEqual(['pour rice', true]);
  });

  it('summarises what 执行裁决 applies and which rewritten texts are judged again (D39)', () => {
    const rewritten = answer('unsure', 'pour rice');
    const a = viewCard({ episode_index: 1, status: 'unsure', questions: [conflictQ] }, { [decisionKey(1, 'task_verdict')]: rewritten });
    const b = viewCard({ episode_index: 2, status: 'decided', questions: [conflictQ] }, { [decisionKey(2, 'task_verdict')]: answer('success', 'pour rice') });
    const c = viewCard({ episode_index: 3, status: 'pending', questions: [verdictQ] }, { [decisionKey(3, 'task_verdict')]: answer('unsure') });
    const d = viewCard({ episode_index: 4, status: 'decided', questions: [verdictQ] }, { [decisionKey(4, 'task_verdict')]: answer('discard') });
    const s = applySummary([a, b, c, d]);
    expect([s.episodes, s.decisions, s.rerun, s.humanJudged]).toEqual([3, 3, 1, 1]);
    expect(s.views.map((v) => v.ep)).toEqual([1, 2, 4]);
  });

  it('the registry catalog gives titles, decisions, pending and 整条弃用 per line (D43)', () => {
    const catalog: ReviewLine[] = [
      { id: 'reject_appeal', review_kind: 'reject_appeal', title_zh: '被拒复议', applies_to: 'reject', counts_as_pending: false, decisions: [{ const: 'restore', title: '恢复为可用' }, { const: 'keep_rejected', title: '维持拒绝' }, { const: 'unsure', title: '拿不准' }] },
      { id: 'grip_check', review_kind: 'grip_check', title_zh: '夹爪状态核对', applies_to: 'passed', counts_as_pending: true, decisions: [{ const: 'grip_ok', title: '夹爪正常' }, { const: 'discard', title: '整条不要' }] },
    ];
    expect(lineTitle(catalog, 'grip_check')).toBe('夹爪状态核对');
    expect(lineTitle(catalog, 'task_verdict')).toBe('任务成败');
    expect(lineTitle(undefined, 'weird')).toBe('weird');
    expect(decisionTitle(catalog, 'reject_appeal', 'restore')).toBe('恢复为可用');
    expect(decisionTitle(catalog, 'grip_check', 'nope')).toBe('nope');
    expect(lineDecisions(catalog, 'grip_check').map((d) => d.const)).toEqual(['grip_ok', 'discard']);
    expect(lineDecisions(undefined, 'task_verdict', ['success', 'failure']).map((d) => d.title)).toEqual(['判成功', '判失败']);
    expect([countsAsPending(catalog, 'reject_appeal'), countsAsPending(catalog, 'grip_check'), countsAsPending(undefined, 'reject_appeal')]).toEqual([false, true, false]);
    expect([offersDiscard(catalog, 'grip_check'), offersDiscard(catalog, 'reject_appeal'), offersDiscard(undefined, 'task_verdict')]).toEqual([true, false, true]);
    const appeal: AdjudicationQuestion = { line: 'reject_appeal', source_module: 'dedup', reason: '重复', duplicate_of: 43, latest_decision: null };
    const a = viewCard({ episode_index: 44, status: 'pending', questions: [appeal] }, {}, catalog);
    expect([a.optional, a.discardLine]).toEqual([true, null]);
    const g = viewCard({ episode_index: 12, status: 'pending', questions: [{ line: 'grip_check', source_module: 'motion_quality', reason: 'r', latest_decision: null }] }, {}, catalog);
    expect([g.optional, g.discardLine]).toEqual([false, 'grip_check']);
  });

  it('a click that only repeats the answer in force is not sent (the rewritten text included)', () => {
    const v = viewCard(card([{ ...conflictQ, latest_decision: decided('task_verdict', 'failure', false, 'pour rice') }]), {});
    expect(answerOn(v, 'task_verdict')?.decision).toBe('failure');
    expect(answerOn(v, 'eef_check')).toBeNull();
    const stored = { decision: 'failure', new_label: 'pour rice', applied: false };
    expect([repeats(stored, 'failure', 'pour rice'), repeats(stored, 'failure', 'pour rice soup'), repeats(stored, 'success', 'pour rice'), repeats(null, 'failure')]).toEqual([true, false, false, false]);
    const typed = clicked('success', 'push the plate back');
    expect([repeats(typed, 'success', 'push the plate back'), repeats(typed, 'success')]).toEqual([true, false]);
  });

  it('maps the status filter onto the C4 query and client-side filters', () => {
    expect(statusQuery('unsure')).toEqual({ status: 'pending', onlyUnsure: true });
    expect(statusQuery('unapplied')).toEqual({ status: 'unapplied', onlyUnsure: false });
    expect(statusQuery('whatever')).toEqual({ status: 'pending', onlyUnsure: false });
    const c = card([verdictQ, eefQ], 'unsure');
    expect(keepCard(c, { sources: ['task_success', 'dedup'], line: '', onlyUnsure: false })).toBe(true);
    expect(keepCard(c, { sources: ['dedup'], line: '', onlyUnsure: false })).toBe(false);
    expect(keepCard(c, { sources: [], line: 'reject_appeal', onlyUnsure: false })).toBe(false);
    expect(keepCard(c, { sources: [], line: '', onlyUnsure: true })).toBe(true);
    expect(keepCard({ ...c, status: 'pending' }, { sources: [], line: '', onlyUnsure: true })).toBe(false);
  });
});
