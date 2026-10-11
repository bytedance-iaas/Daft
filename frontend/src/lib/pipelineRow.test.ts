import { describe, expect, it } from 'vitest';
import { groupSeconds, rowFinished, rowStages, rowState, waitingStages } from './pipelineRow';

describe('a row of the episode pipeline, its stages in groups (design doc 26 §2)', () => {
  it('a two-block row waits for the first open group of each block', () => {
    const row = { stages: { integrity: 'done', numeric: 'error', frame: 'waiting', duplicate: 'waiting', vlm: 'waiting' }, verdict: null, reason: null } as const;
    expect(waitingStages(row)).toEqual(['CPU 检查', '模型检查']);
    expect(rowFinished(row)).toBe(false);
    expect(rowState(row)).toBe('等待 CPU 检查、模型检查');
    expect(rowStages(row)).toBe('数据完整性 ✓ · CPU 检查 … · 模型检查 …');
    // a group is done once all its stages are, with an error if any had one
    expect(rowStages({ ...row, stages: { numeric: 'error', frame: 'done', duplicate: 'done', vlm: 'done' } })).toBe('CPU 检查 ✗ · 模型检查 ✓');
  });

  it("a model module's CPU half is part of CPU 检查; its model half waits for it (registry 5.3)", () => {
    const row = { stages: { numeric: 'done', vlm_prep: 'done', vlm: 'running' }, verdict: null, reason: null } as const;
    expect(waitingStages(row)).toEqual(['模型检查']);
    expect(rowStages(row)).toBe('CPU 检查 ✓ · 模型检查 …');
    expect(waitingStages({ ...row, stages: { numeric: 'done', vlm_prep: 'waiting', vlm: 'waiting' } })).toEqual(['CPU 检查', '模型检查']);
  });

  it("an episode's time in a group adds up its stages'", () => {
    expect(groupSeconds({ stage_processing_s: { numeric: 0.2, frame: 3, duplicate: 0.05, vlm: 12 } }, ['numeric', 'frame', 'duplicate', 'vlm_prep'])).toBeCloseTo(3.25);
    expect(groupSeconds({ stage_processing_s: { numeric: 0.2 } }, ['vlm'])).toBeNull();
    expect(groupSeconds({}, ['vlm'])).toBeNull();
  });

  it('a finished two-block row shows its provisional verdict', () => {
    const row = { stages: { numeric: 'done', vlm: 'done' }, verdict: 'drop', reason: null, provisional: true } as const;
    expect(rowFinished(row)).toBe(true);
    expect(rowState(row)).toBe('暂判拒绝');
    expect(rowState({ ...row, verdict: null })).toBe('已完成');
    // the run is over: the current results' verdict (C4 5.3.2)
    expect(rowState({ ...row, provisional: false })).toBe('拒绝');
    expect(rowState({ ...row, provisional: false, verdict: 'keep' })).toBe('通过');
  });

  it('a funnel row (a task made before) keeps its position, named by its group', () => {
    expect(rowState({ next_stage: 'frame', last_stage: 'numeric', verdict: null, reason: null })).toBe('等待 CPU 检查');
    expect(rowState({ next_stage: 'done', last_stage: 'vlm', verdict: 'keep', reason: null })).toBe('漏斗保留');
    expect(rowStages({ next_stage: 'done', last_stage: 'vlm', verdict: 'keep', reason: null })).toBe('模型检查');
    expect(rowState({ next_stage: 'done', last_stage: 'numeric', verdict: null, reason: 'missing' })).toBe('源文件缺失');
  });
});
