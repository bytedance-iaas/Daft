import { describe, expect, it } from 'vitest';
import type { AdjudicationCard, AdjudicationQuestion, Decision, ReviewLine } from '../api/types';
import { answerOn, applySummary, clicked, countsAsPending, decisionKey, decisionTitle, keepCard, lineDecisions, lineTitle, offersDiscard, repeats, statusQuery, viewCard } from './adjudication';

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

/** The registry's label line with v1's optional verdict as its follow-up (C1 1.3). */
const followUpCatalog: ReviewLine[] = [
  {
    id: 'label',
    review_kind: 'label_conflict',
    title_zh: '标注分歧',
    applies_to: 'passed',
    counts_as_pending: true,
    decisions: [{ const: 'adopt_suggestion', title: '采纳新标注' }, { const: 'custom_label', title: '自行改写标注' }, { const: 'keep_label', title: '维持原标注' }, { const: 'discard', title: '其它原因，整条弃用' }],
    follow_ups: [{ after: ['adopt_suggestion', 'custom_label'], line: 'task_verdict', decisions: ['success', 'failure', 'unsure'], optional: true }],
  },
  { id: 'task_verdict', review_kind: 'task_verdict', title_zh: '任务成败弃权', applies_to: 'passed', counts_as_pending: true, decisions: [{ const: 'success', title: '判成功' }, { const: 'failure', title: '判失败' }, { const: 'unsure', title: '拿不准' }] },
];

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

  it('the registry catalog gives titles, decisions, pending and 整条弃用 per line (D43)', () => {
    const catalog: ReviewLine[] = [
      { id: 'reject_appeal', review_kind: 'reject_appeal', title_zh: '被拒复议', applies_to: 'reject', counts_as_pending: false, decisions: [{ const: 'restore', title: '恢复为可用' }, { const: 'keep_rejected', title: '维持拒绝' }, { const: 'unsure', title: '拿不准' }] },
      { id: 'grip_check', review_kind: 'grip_check', title_zh: '夹爪状态核对', applies_to: 'passed', counts_as_pending: true, decisions: [{ const: 'grip_ok', title: '夹爪正常' }, { const: 'discard', title: '整条不要' }] },
    ];
    expect(lineTitle(catalog, 'grip_check')).toBe('夹爪状态核对');
    expect(lineTitle(catalog, 'label')).toBe('标注分歧');
    expect(lineTitle(undefined, 'weird')).toBe('weird');
    expect(decisionTitle(catalog, 'reject_appeal', 'restore')).toBe('恢复为可用');
    expect(decisionTitle(catalog, 'grip_check', 'nope')).toBe('nope');
    expect(lineDecisions(catalog, 'grip_check').map((d) => d.const)).toEqual(['grip_ok', 'discard']);
    expect(lineDecisions(undefined, 'task_verdict', ['success', 'failure']).map((d) => d.title)).toEqual(['判成功', '判失败']);
    expect([countsAsPending(catalog, 'reject_appeal'), countsAsPending(catalog, 'grip_check'), countsAsPending(undefined, 'reject_appeal')]).toEqual([false, true, false]);
    expect([offersDiscard(catalog, 'grip_check'), offersDiscard(catalog, 'reject_appeal'), offersDiscard(undefined, 'label')]).toEqual([true, false, true]);
    const appeal: AdjudicationQuestion = { line: 'reject_appeal', source_module: 'dedup', reason: '重复', duplicate_of: 43, latest_decision: null };
    const a = viewCard({ episode_index: 44, status: 'pending', questions: [appeal] }, {}, catalog);
    expect([a.optional, a.discardLine]).toEqual([true, null]);
    const g = viewCard({ episode_index: 12, status: 'pending', questions: [{ line: 'grip_check', source_module: 'motion_quality', reason: 'r', latest_decision: null }] }, {}, catalog);
    expect([g.optional, g.discardLine]).toEqual([false, 'grip_check']);
  });

  it('a follow-up opens after its `after` answers, never counts as pending and lapses (C4 1.5.1)', () => {
    const catalog = followUpCatalog;
    const answer = (decision: string) => ({ decision, new_label: null, applied: false });
    const labelOnly = card([labelQ]);
    // Closed before a relabel.
    expect(viewCard(labelOnly, {}, catalog).followUps.map((f) => [f.line, f.open])).toEqual([['task_verdict', false]]);
    // Open after adopting; answered success: the person's verdict stands, not judged again.
    const yes = viewCard(labelOnly, { [decisionKey(29, 'label')]: answer('adopt_suggestion'), [decisionKey(29, 'task_verdict')]: answer('success') }, catalog);
    expect(yes.followUps[0]).toMatchObject({ open: true, optional: true, decisions: [{ const: 'success', title: '判成功' }, { const: 'failure', title: '判失败' }, { const: 'unsure', title: '拿不准' }] });
    expect([yes.humanVerdict, yes.rerunsModel, yes.status]).toEqual(['success', false, 'decided']);
    expect(yes.unapplied.map((u) => `${u.line}:${u.decision}`)).toEqual(['label:adopt_suggestion', 'task_verdict:success']);
    // 拿不准 on it is like leaving it open: judged again, and the card stays 已裁.
    const unsure = viewCard(labelOnly, { [decisionKey(29, 'label')]: answer('adopt_suggestion'), [decisionKey(29, 'task_verdict')]: answer('unsure') }, catalog);
    expect([unsure.humanVerdict, unsure.rerunsModel, unsure.status]).toEqual([null, true, 'decided']);
    // Lapsed once the label answer changes: not counted, not applied.
    const lapsed = viewCard(labelOnly, { [decisionKey(29, 'label')]: answer('keep_label'), [decisionKey(29, 'task_verdict')]: answer('success') }, catalog);
    expect([lapsed.followUps[0].open, lapsed.humanVerdict]).toEqual([false, null]);
    expect(lapsed.unapplied.map((u) => u.line)).toEqual(['label']);
    // A card that asks the verdict itself gets no follow-up.
    expect(viewCard(card([labelQ, verdictQ]), { [decisionKey(29, 'label')]: answer('adopt_suggestion') }, catalog).followUps).toEqual([]);
  });

  it('a follow-up the server lists (follow_up_of, C4 1.5.2) is the optional block; its answer lapses on a newer label answer', () => {
    const catalog = followUpCatalog;
    const opened: AdjudicationQuestion = { ...labelQ, latest_decision: { ...decided('label', 'adopt_suggestion'), id: 5 } };
    const listed: AdjudicationQuestion = {
      line: 'task_verdict',
      source_module: 'skill_profile',
      reason: '改标之后可以一并判成败（选填）',
      annotation: labelQ.annotation,
      follow_up_of: 'label',
      latest_decision: { ...decided('task_verdict', 'failure'), id: 6 },
    };
    const c = card([opened, listed]);
    const v = viewCard(c, {}, catalog);
    // Not a question of the card: no verdict question; sources and filters come from the label alone.
    expect(v.questions.map((q) => q.line)).toEqual(['label']);
    expect([v.hasVerdictQuestion, v.sources, keepCard(c, { sources: [], line: 'task_verdict', onlyUnsure: false })]).toEqual([false, ['skill_profile'], false]);
    expect(v.followUps).toHaveLength(1);
    expect(v.followUps[0]).toMatchObject({ line: 'task_verdict', openedBy: 'label', open: true, effective: { decision: 'failure' }, question: listed, decided: listed.latest_decision });
    expect([v.humanVerdict, v.rerunsModel, v.status]).toEqual(['failure', false, 'decided']);
    expect(v.unapplied.map((u) => `${u.line}:${u.decision}`)).toEqual(['label:adopt_suggestion', 'task_verdict:failure']);
    // Before the registry has loaded, the listed question alone gives the block.
    expect(viewCard(c, {}, undefined).followUps.map((f) => [f.open, f.effective?.decision, f.decisions.map((d) => d.const).join()])).toEqual([[true, 'failure', 'success,failure,unsure']]);
    // A new answer on the label, even one that keeps the block open, makes the earlier verdict lapse ...
    const relabel = clicked('custom_label', 'push the plate back');
    const lapsed = viewCard(c, { [decisionKey(29, 'label')]: relabel }, catalog);
    expect([lapsed.followUps[0].open, lapsed.followUps[0].effective, lapsed.humanVerdict, lapsed.rerunsModel]).toEqual([true, null, null, true]);
    expect(lapsed.unapplied.map((u) => u.line)).toEqual(['label']);
    // ... an answer given after it stands ...
    const again = viewCard(c, { [decisionKey(29, 'label')]: relabel, [decisionKey(29, 'task_verdict')]: clicked('success') }, catalog);
    expect([again.followUps[0].effective?.decision, again.followUps[0].decided, again.humanVerdict]).toEqual(['success', null, 'success']);
    // ... and one clicked before the relabel lapses as well.
    const early = clicked('success');
    expect(viewCard(c, { [decisionKey(29, 'task_verdict')]: early, [decisionKey(29, 'label')]: clicked('custom_label', 'x') }, catalog).followUps[0].effective).toBeNull();
    // The label executed, the verdict not yet: 已裁 until the verdict is executed too.
    const done = card([{ ...opened, latest_decision: { ...opened.latest_decision!, applied: true } }, listed]);
    expect(viewCard(done, {}, catalog).status).toBe('decided');
    expect(viewCard(card([done.questions[0], { ...listed, latest_decision: { ...listed.latest_decision!, applied: true } }]), {}, catalog).status).toBe('applied');
    // Nothing is sent when a click only repeats the answer in force: a new label answer would lapse the verdict.
    expect([answerOn(v, 'label')?.decision, answerOn(v, 'task_verdict')?.decision, answerOn(lapsed, 'task_verdict')]).toEqual(['adopt_suggestion', 'failure', null]);
    const stored = { decision: 'adopt_suggestion', new_label: 'pour rice into the green bowl', applied: false };
    expect([repeats(stored, 'adopt_suggestion'), repeats(stored, 'keep_label'), repeats(null, 'adopt_suggestion')]).toEqual([true, false, false]);
    expect([repeats(relabel, 'custom_label', 'push the plate back'), repeats(relabel, 'custom_label', 'push it back')]).toEqual([true, false]);
    // Without the flag (a server before 1.5.2) the same question reads as the card's own.
    const own = viewCard(card([opened, { ...listed, follow_up_of: null }]), {}, catalog);
    expect([own.questions.map((q) => q.line), own.followUps, own.hasVerdictQuestion]).toEqual([['label', 'task_verdict'], [], true]);
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
