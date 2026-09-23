import { describe, expect, it } from 'vitest';
import { registry } from '../mocks/world';
import type { StageProgress, Subtask } from '../api/types';
import {
  actionsFor,
  activeSubtask,
  currentStage,
  displayState,
  exportedBefore,
  groupStages,
  mergedStageState,
  moduleProblems,
  overallPercent,
  presetOf,
  progressStageLabel,
  progressStages,
  stageLabel,
} from './taskView';

const all = registry.modules.map((m) => m.id);
const without = (...ids: string[]) => all.filter((id) => !ids.includes(id));
const nonVlm = registry.modules.filter((m) => !(m.needs as string[]).includes('vlm')).map((m) => m.id);

describe('presetOf (07 §4.1 preset line, derived from the registry)', () => {
  it('recognises full and quick, tolerating modules the dataset could not support', () => {
    expect(presetOf(all, registry)).toBe('full');
    expect(presetOf(without('kinematic_limits'), registry)).toBe('full');
    expect(presetOf(without('kinematic_limits', 'motion_quality'), registry)).toBe('full');
    expect(presetOf(nonVlm, registry)).toBe('quick');
    expect(presetOf(nonVlm.filter((id) => id !== 'kinematic_limits'), registry)).toBe('quick');
  });

  it('anything else is 自选', () => {
    expect(presetOf(without('dedup'), registry)).toBe('custom');
    expect(presetOf(without('skill_profile'), registry)).toBe('custom');
    expect(presetOf(['task_success'], registry)).toBe('custom');
    expect(presetOf(nonVlm.filter((id) => id !== 'dedup'), registry)).toBe('custom');
    expect(presetOf([], registry)).toBe('custom');
    expect(presetOf(all, undefined)).toBe('custom');
  });
});

const stage = (id: StageProgress['id'], state: StageProgress['state'], done = 0, total = 0): StageProgress => ({ id, state, done, total });

describe('stages and export', () => {
  it('current stage is the running one, else the last started', () => {
    expect(currentStage([stage('numeric', 'succeeded'), stage('vlm', 'running', 3, 9)])?.id).toBe('vlm');
    expect(currentStage([stage('numeric', 'succeeded'), stage('frame', 'succeeded'), stage('vlm', 'pending')])?.id).toBe('frame');
  });

  it('「导出」 until an export ever succeeded, then 「重新导出」', () => {
    expect(exportedBefore([stage('export', 'skipped')])).toBe(false);
    expect(exportedBefore([stage('export', 'succeeded')])).toBe(true);
    expect(
      exportedBefore([stage('export', 'skipped')], [{ id: 's', task_id: 't', kind: 'reexport', scope: {}, state: 'succeeded', created_at: 1 }]),
    ).toBe(true);
  });

  it('module problems: red errors, gray not run', () => {
    expect(moduleProblems({ succeeded: 5, failed: 1, completed_with_errors: 1, skipped: 2 })).toEqual({ errors: 2, notRun: 2 });
  });
});

describe('merged stages (requester item 11)', () => {
  it('终判 + 报告 read as 报告生成, 导出 + 交付核验 as 交付; other stages keep their names', () => {
    expect(progressStageLabel('final')).toBe('报告生成');
    expect(progressStageLabel('report')).toBe('报告生成');
    expect(progressStageLabel('export')).toBe('交付');
    expect(progressStageLabel('verify')).toBe('交付');
    expect(progressStageLabel('vlm')).toBe('VLM 档');
    expect(progressStageLabel('brand_new_stage')).toBe('brand_new_stage');
    // The raw names stay for the log filter and the execution plan.
    expect(stageLabel('final')).toBe('终判');
    expect(stageLabel('verify')).toBe('交付核验');
  });

  it('merged state: failed > running > completed_with_errors, then pending / succeeded / under way', () => {
    expect(mergedStageState(['succeeded', 'failed'])).toBe('failed');
    expect(mergedStageState(['completed_with_errors', 'running'])).toBe('running');
    expect(mergedStageState(['succeeded', 'completed_with_errors'])).toBe('completed_with_errors');
    expect(mergedStageState(['pending', 'pending'])).toBe('pending');
    expect(mergedStageState(['skipped', 'pending'])).toBe('pending');
    expect(mergedStageState(['succeeded', 'skipped'])).toBe('succeeded');
    expect(mergedStageState(['succeeded', 'pending'])).toBe('running');
    expect(mergedStageState(['skipped', 'skipped'])).toBe('skipped');
  });

  it('groups where the first member stands, in any stage order, adding counts, times and notes', () => {
    const views = groupStages([
      stage('vlm', 'succeeded', 49, 49),
      { ...stage('final', 'succeeded', 1, 1), elapsed_s: 1 },
      { ...stage('export', 'skipped'), note: '主流程结束时没有可交付的条目，没有导出' },
      { ...stage('report', 'succeeded', 1, 1), elapsed_s: 6 },
      stage('verify', 'skipped'),
    ]);
    expect(views.map((v) => [v.key, v.label, v.members.join('+'), v.state])).toEqual([
      ['vlm', 'VLM 档', 'vlm', 'succeeded'],
      ['report_generation', '报告生成', 'final+report', 'succeeded'],
      ['delivery', '交付', 'export+verify', 'skipped'],
    ]);
    expect(views[1]).toMatchObject({ done: 2, total: 2, percent: 100, elapsed_s: 7 });
    expect(views[2]).toMatchObject({ percent: 0, elapsed_s: null, note: '主流程结束时没有可交付的条目，没有导出' });
  });

  it("a group's bar weighs its members the same and only moves forward", () => {
    // The Daemon lists a stage it has not reached as 0 / 0, and export has no total of its own.
    const at = (final: StageProgress, report: StageProgress) => groupStages([final, report])[0];
    expect(at(stage('final', 'pending'), stage('report', 'pending'))).toMatchObject({ state: 'pending', percent: 0 });
    expect(at(stage('final', 'running', 0, 1), stage('report', 'pending'))).toMatchObject({ state: 'running', percent: 0 });
    expect(at(stage('final', 'succeeded', 1, 1), stage('report', 'pending'))).toMatchObject({ state: 'running', percent: 50 });
    expect(at(stage('final', 'succeeded', 1, 1), stage('report', 'running', 0, 1))).toMatchObject({ state: 'running', percent: 50 });
    expect(at(stage('final', 'succeeded', 1, 1), stage('report', 'succeeded', 1, 1))).toMatchObject({ state: 'succeeded', percent: 100 });
    const delivery = groupStages([stage('export', 'succeeded', 0, 0), stage('verify', 'running', 120, 300)])[0];
    expect(delivery).toMatchObject({ key: 'delivery', state: 'running', done: 120, total: 300, percent: 70 });
    // A stage that failed shows how far it got.
    expect(groupStages([stage('frame', 'failed', 3, 20)])[0]).toMatchObject({ state: 'failed', percent: 15 });
  });

  it('the whole task: every stage not skipped weighs the same, the current one by its bar', () => {
    const running = [
      stage('autolabel', 'succeeded', 640, 640),
      stage('numeric', 'succeeded', 640, 640),
      stage('frame', 'succeeded', 631, 631),
      stage('vlm', 'running', 410, 631),
      ...['verdict', 'dedup', 'profile', 'final', 'report', 'export', 'verify'].map((id) => stage(id, 'pending')),
    ];
    // 9 rows once 报告生成 and 交付 are merged: (3 + 0.65) / 9
    expect(overallPercent(groupStages(running))).toBe(41);
    expect(overallPercent(groupStages([stage('numeric', 'succeeded', 5, 5), stage('frame', 'skipped'), stage('vlm', 'pending')]))).toBe(50);
    expect(overallPercent(groupStages(allDoneStages()))).toBe(100);
    expect(overallPercent([])).toBe(0);
  });
});

describe('display state while a subtask runs (D46)', () => {
  const sub = (id: string, kind: Subtask['kind'], state: Subtask['state'], extra: Partial<Subtask> = {}): Subtask => ({ id, task_id: 't', kind, scope: {}, state, created_at: 1, ...extra });
  const retry1 = sub('s1', 'retry', 'succeeded');
  const retry2 = sub('s2', 'retry', 'queued');

  it("the task's own state when nothing runs, or when the cached subtask already ended", () => {
    expect(displayState({ state: 'completed_with_errors', active_subtask: null })).toEqual({ state: 'completed_with_errors', pauseReason: null, subtask: null, bySubtask: false });
    expect(displayState({ state: 'paused', pause_reason: 'system' })).toMatchObject({ state: 'paused', pauseReason: 'system', subtask: null });
    expect(displayState({ state: 'succeeded', active_subtask: sub('s2', 'retry', 'succeeded') })).toMatchObject({ state: 'succeeded', bySubtask: false });
    expect(activeSubtask({ active_subtask: sub('s2', 'retry', 'failed') })).toBeNull();
  });

  it('运行中 named after the subtask where known: its ordinal once listed, else its kind', () => {
    expect(displayState({ state: 'completed_with_errors', active_subtask: retry2 }, [retry1, retry2])).toEqual({ state: 'running', pauseReason: null, subtask: '重试 #2', bySubtask: true });
    expect(displayState({ state: 'completed_with_errors', active_subtask: retry2 })).toMatchObject({ state: 'running', subtask: '重试' });
    expect(displayState({ state: 'stopped', active_subtask: sub('s3', 'resume', 'running') })).toMatchObject({ state: 'running', subtask: '继续运行' });
    // A list row only carries the id.
    expect(displayState({ state: 'succeeded', active_subtask: 's9' })).toEqual({ state: 'running', pauseReason: null, subtask: null, bySubtask: true });
  });

  it('a subtask paused by the system or being stopped shows that, still named', () => {
    expect(displayState({ state: 'succeeded', active_subtask: sub('s4', 'reexport', 'paused', { pause_reason: 'system' }) })).toEqual({ state: 'paused', pauseReason: 'system', subtask: '重新导出', bySubtask: true });
    expect(displayState({ state: 'succeeded', active_subtask: sub('s4', 'reexport', 'stopping') })).toMatchObject({ state: 'stopping', subtask: '重新导出' });
  });

  it('reads the stages of a subtask progress document defensively', () => {
    expect(progressStages({ stages: [stage('vlm', 'running', 1, 2), { id: 'x' }, null] })).toEqual([stage('vlm', 'running', 1, 2)]);
    expect(progressStages(null)).toEqual([]);
    expect(progressStages({ stages: 'nope' })).toEqual([]);
  });
});

function allDoneStages(): StageProgress[] {
  return ['autolabel', 'numeric', 'frame', 'vlm', 'verdict', 'dedup', 'profile', 'final', 'export', 'report', 'verify'].map((id) => stage(id, 'succeeded', 1, 1));
}

describe('actionsFor (07 §4.1)', () => {
  const base = { pause_reason: null, pending_adjudication: 0, delivery_stale: false, summary: null, deleted_at: null, active_subtask: null } as const;
  it('by state', () => {
    expect(actionsFor({ ...base, state: 'created' })).toMatchObject({ primary: 'start', more: ['edit', 'copy', 'delete'] });
    expect(actionsFor({ ...base, state: 'running' }).more).toEqual(['pause', 'stop', 'delete']);
    expect(actionsFor({ ...base, state: 'running' }).disabled.delete).toBeTruthy();
    expect(actionsFor({ ...base, state: 'paused', pause_reason: 'system' }).disabled.resume).toBe('系统暂停的任务会自动恢复，不需要手动恢复');
    expect(actionsFor({ ...base, state: 'paused', pause_reason: 'user' }).disabled.resume).toBeUndefined();
    expect(actionsFor({ ...base, state: 'completed_with_errors' }).more[0]).toBe('retry');
    expect(actionsFor({ ...base, state: 'failed' }).primary).toBe('continue');
    expect(actionsFor({ ...base, state: 'stopped' }).primary).toBe('continue');
    expect(actionsFor({ ...base, state: 'succeeded', pending_adjudication: 3, delivery_stale: true }).more).toEqual(['adjudicate', 'export', 'copy', 'purge', 'delete']);
    expect(actionsFor({ ...base, state: 'succeeded', deleted_at: 1 })).toEqual({ primary: 'restore', more: [], disabled: {} });
  });
});
