import { describe, expect, it } from 'vitest';
import { registry } from '../mocks/world';
import type { StageProgress } from '../api/types';
import { actionsFor, currentStage, exportedBefore, moduleProblems, presetOf } from './taskView';

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
