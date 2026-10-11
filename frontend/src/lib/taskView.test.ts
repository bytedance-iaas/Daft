import { describe, expect, it } from 'vitest';
import { registry } from '../mocks/world';
import type { StageProgress, Subtask } from '../api/types';
import {
  actionsFor,
  activeSubtask,
  currentStage,
  displayState,
  EPISODE_STAGES,
  groupsOf,
  groupStages,
  hasResult,
  mergedStageState,
  moduleProblems,
  overallPercent,
  presetOf,
  progressStageLabel,
  progressStages,
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
    expect(presetOf(without('duplicate_check'), registry)).toBe('custom');
    expect(presetOf(without('task_success'), registry)).toBe('custom');
    expect(presetOf(['task_success'], registry)).toBe('custom');
    expect(presetOf(nonVlm.filter((id) => id !== 'duplicate_check'), registry)).toBe('custom');
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

  it('module problems: red errors, gray not run', () => {
    expect(moduleProblems({ succeeded: 5, failed: 1, completed_with_errors: 1, skipped: 2 })).toEqual({ errors: 2, notRun: 2 });
  });
});

describe('stages in groups (design doc 26 §2, D88)', () => {
  it('数据完整性, CPU 检查, 模型检查, 生成报告; a stage of no group keeps its name', () => {
    expect(progressStageLabel('integrity')).toBe('数据完整性');
    for (const id of ['numeric', 'frame', 'duplicate', 'vlm_prep']) expect(progressStageLabel(id)).toBe('CPU 检查');
    expect(progressStageLabel('vlm')).toBe('模型检查');
    for (const id of ['final', 'report', 'export', 'verify']) expect(progressStageLabel(id)).toBe('生成报告');
    expect(progressStageLabel('brand_new_stage')).toBe('brand_new_stage');
    expect(EPISODE_STAGES).toEqual(['integrity', 'numeric', 'frame', 'duplicate', 'vlm_prep', 'vlm']);
    // the log filter's options: the groups present, each with its stages, then the rest
    expect(groupsOf(['numeric', 'frame', 'duplicate', 'vlm', 'final', 'report', 'system'])).toEqual([
      { key: 'cpu', label: 'CPU 检查', members: ['numeric', 'frame', 'duplicate'], perEpisode: true },
      { key: 'model', label: '模型检查', members: ['vlm'], perEpisode: true },
      { key: 'report', label: '生成报告', members: ['final', 'report'], perEpisode: false },
      { key: 'system', label: 'system', members: ['system'], perEpisode: false },
    ]);
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

  it('a group of the episodes has done what all its stages have; its time is the union of their runs', () => {
    const run = (from: number, to: number | null) => ({ inflight: 0, queued: 0, capacity: 1, dispatches: 1, recent: [], started_at: from * 1000, finished_at: to === null ? null : to * 1000, updated_at: (to ?? 500) * 1000 });
    const views = groupStages([
      { ...stage('integrity', 'succeeded', 50, 50), elapsed_s: 30 },
      { ...stage('numeric', 'succeeded', 50, 50), elapsed_s: 20, pipeline: run(0, 20) },
      { ...stage('frame', 'running', 31, 50), elapsed_s: 300, pipeline: run(5, null) },
      { ...stage('duplicate', 'running', 30, 50), elapsed_s: 290, pipeline: run(15, null) },
      { ...stage('vlm', 'completed_with_errors', 50, 50), elapsed_s: 600, note: '2 条出错' },
      stage('final', 'pending'),
    ]);
    expect(views.map((v) => [v.key, v.label, v.members.join('+'), v.state])).toEqual([
      ['integrity', '数据完整性', 'integrity', 'succeeded'],
      ['cpu', 'CPU 检查', 'numeric+frame+duplicate', 'running'],
      ['model', '模型检查', 'vlm', 'completed_with_errors'],
      ['report', '生成报告', 'final', 'pending'],
    ]);
    // 30 of 50: the slowest stage's; the runs 0-20, 5-500 and 15-500 s overlap into 0-500 s
    expect(views[1]).toMatchObject({ done: 30, total: 50, elapsed_s: 500 });
    expect(views[2]).toMatchObject({ elapsed_s: 600, note: '2 条出错' });
    // without the runs' times: the longest stage's
    expect(groupStages([{ ...stage('numeric', 'succeeded', 5, 5), elapsed_s: 2 }, { ...stage('frame', 'succeeded', 5, 5), elapsed_s: 9 }])[0].elapsed_s).toBe(9);
  });

  it('生成报告 adds its steps up and is placed where the first of them stands', () => {
    const views = groupStages([
      stage('vlm', 'succeeded', 49, 49),
      { ...stage('final', 'succeeded', 1, 1), elapsed_s: 1 },
      { ...stage('export', 'skipped'), note: '主流程结束时没有可交付的条目，没有导出' },
      { ...stage('report', 'succeeded', 1, 1), elapsed_s: 6 },
      stage('verify', 'skipped'),
    ]);
    expect(views.map((v) => [v.key, v.members.join('+'), v.state])).toEqual([
      ['model', 'vlm', 'succeeded'],
      ['report', 'final+export+report+verify', 'succeeded'],
    ]);
    expect(views[1]).toMatchObject({ done: 2, total: 2, percent: 100, elapsed_s: 7, note: '主流程结束时没有可交付的条目，没有导出' });
  });

  it("a group's bar weighs its members the same and only moves forward", () => {
    // The Daemon lists a stage it has not reached as 0 / 0, and export has no total of its own.
    const at = (final: StageProgress, report: StageProgress) => groupStages([final, report])[0];
    expect(at(stage('final', 'pending'), stage('report', 'pending'))).toMatchObject({ state: 'pending', percent: 0 });
    expect(at(stage('final', 'running', 0, 1), stage('report', 'pending'))).toMatchObject({ state: 'running', percent: 0 });
    expect(at(stage('final', 'succeeded', 1, 1), stage('report', 'pending'))).toMatchObject({ state: 'running', percent: 50 });
    expect(at(stage('final', 'succeeded', 1, 1), stage('report', 'succeeded', 1, 1))).toMatchObject({ state: 'succeeded', percent: 100 });
    const delivery = groupStages([stage('export', 'succeeded', 0, 0), stage('verify', 'running', 120, 300)])[0];
    expect(delivery).toMatchObject({ key: 'report', state: 'running', done: 120, total: 300, percent: 70 });
    // A stage that failed shows how far it got.
    expect(groupStages([stage('frame', 'failed', 3, 20)])[0]).toMatchObject({ key: 'cpu', state: 'failed', percent: 15 });
  });

  it('the whole task: every group not skipped weighs the same, the current one by its bar', () => {
    const running = [
      stage('integrity', 'succeeded', 640, 640),
      stage('numeric', 'succeeded', 640, 640),
      stage('frame', 'succeeded', 640, 640),
      stage('duplicate', 'succeeded', 640, 640),
      stage('vlm', 'running', 410, 640),
      ...['final', 'report', 'verify'].map((id) => stage(id, 'pending')),
    ];
    // four rows: (2 + 0.64) / 4
    expect(overallPercent(groupStages(running))).toBe(66);
    // a funnel run's verdict is left out (sixth round)
    expect(groupStages([...running, stage('verdict', 'pending')]).map((v) => v.key)).not.toContain('verdict');
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
    expect(displayState({ state: 'succeeded', active_subtask: sub('s4', 'apply_adjudication', 'paused', { pause_reason: 'system' }) })).toEqual({ state: 'paused', pauseReason: 'system', subtask: '执行裁决', bySubtask: true });
    expect(displayState({ state: 'succeeded', active_subtask: sub('s4', 'apply_adjudication', 'stopping') })).toMatchObject({ state: 'stopping', subtask: '执行裁决' });
  });

  it('reads the stages of a subtask progress document defensively', () => {
    expect(progressStages({ stages: [stage('vlm', 'running', 1, 2), { id: 'x' }, null] })).toEqual([stage('vlm', 'running', 1, 2)]);
    expect(progressStages(null)).toEqual([]);
    expect(progressStages({ stages: 'nope' })).toEqual([]);
  });
});

function allDoneStages(): StageProgress[] {
  return ['numeric', 'frame', 'duplicate', 'vlm', 'final', 'report', 'verify'].map((id) => stage(id, 'succeeded', 1, 1));
}

describe('actionsFor (07 §4.1)', () => {
  const base = { pause_reason: null, pending_adjudication: 0, summary: null, deleted_at: null, active_subtask: null } as const;
  it('by state', () => {
    expect(actionsFor({ ...base, state: 'created' })).toMatchObject({ primary: 'start', more: ['edit', 'copy', 'delete'] });
    expect(actionsFor({ ...base, state: 'running' }).more).toEqual(['pause', 'stop', 'delete']);
    expect(actionsFor({ ...base, state: 'running' }).disabled.delete).toBeTruthy();
    expect(actionsFor({ ...base, state: 'paused', pause_reason: 'system' }).disabled.resume).toBe('系统暂停的任务会自动恢复，不需要手动恢复');
    expect(actionsFor({ ...base, state: 'paused', pause_reason: 'user' }).disabled.resume).toBeUndefined();
    expect(actionsFor({ ...base, state: 'completed_with_errors' }).more[0]).toBe('retry');
    expect(actionsFor({ ...base, state: 'failed' }).primary).toBe('continue');
    expect(actionsFor({ ...base, state: 'stopped' }).primary).toBe('continue');
    expect(actionsFor({ ...base, state: 'succeeded', pending_adjudication: 3 }).more).toEqual(['adjudicate', 'copy', 'purge', 'delete']);
    expect(actionsFor({ ...base, state: 'succeeded', deleted_at: 1 })).toEqual({ primary: 'restore', more: [], disabled: {} });
  });

  it('人工裁决 stands once there is a result, pending items or not (D47)', () => {
    const summary = { total: 10, passed: 9, rejected: 1, held: 0, review: 0, pass_rate: 0.9 };
    expect(actionsFor({ ...base, state: 'succeeded', summary }).more).toEqual(['adjudicate', 'copy', 'purge', 'delete']);
    expect(actionsFor({ ...base, state: 'completed_with_errors', summary }).more).toEqual(['retry', 'adjudicate', 'copy', 'purge', 'delete']);
    // The detail page's Task knows its revision even without a summary.
    expect(actionsFor({ ...base, state: 'succeeded', result_rev: 2 }).more).toContain('adjudicate');
    // No result: no entry (a stopped run without a report, or one not finished).
    expect(actionsFor({ ...base, state: 'stopped' }).more).not.toContain('adjudicate');
    expect(actionsFor({ ...base, state: 'succeeded' }).more).not.toContain('adjudicate');
    expect(actionsFor({ ...base, state: 'running' }).more).not.toContain('adjudicate');
    expect(actionsFor({ ...base, state: 'created' }).more).not.toContain('adjudicate');
    expect(hasResult({ summary: null, result_rev: 0, pending_adjudication: 0 })).toBe(false);
    expect(hasResult({ summary, pending_adjudication: 0 })).toBe(true);
  });
});
