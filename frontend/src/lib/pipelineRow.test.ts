import { describe, expect, it } from 'vitest';
import { rowFinished, rowStages, rowState, waitingStages } from './pipelineRow';

describe('a row of the episode pipeline', () => {
  it('a two-block row waits for the first open stage of each block', () => {
    const row = { stages: { integrity: 'done', numeric: 'error', frame: 'waiting', vlm: 'waiting' }, verdict: null, reason: null } as const;
    expect(waitingStages(row)).toEqual(['frame', 'vlm']);
    expect(rowFinished(row)).toBe(false);
    expect(rowState(row)).toBe('等待视频验证、模型验证');
    expect(rowStages(row)).toBe('完整性验证 ✓ · 数值验证 ✗ · 视频验证 … · 模型验证 …');
  });

  it('a model module of two halves waits for its CPU half, then its model half (registry 5.3)', () => {
    const row = { stages: { numeric: 'done', vlm_prep: 'done', vlm: 'running' }, verdict: null, reason: null } as const;
    expect(waitingStages(row)).toEqual(['vlm']);
    expect(rowStages(row)).toBe('数值验证 ✓ · 模型准备 ✓ · 模型验证 …');
    expect(waitingStages({ ...row, stages: { numeric: 'done', vlm_prep: 'waiting', vlm: 'waiting' } })).toEqual(['vlm_prep']);
  });

  it('a finished two-block row shows its provisional verdict', () => {
    const row = { stages: { numeric: 'done', vlm: 'done' }, verdict: 'drop', reason: null, provisional: true } as const;
    expect(rowFinished(row)).toBe(true);
    expect(rowState(row)).toBe('暂判拒绝');
    expect(rowState({ ...row, verdict: null })).toBe('已完成');
  });

  it('a funnel row (a task made before) keeps its position', () => {
    expect(rowState({ next_stage: 'frame', last_stage: 'numeric', verdict: null, reason: null })).toBe('等待视频验证');
    expect(rowState({ next_stage: 'done', last_stage: 'vlm', verdict: 'keep', reason: null })).toBe('漏斗保留');
    expect(rowStages({ next_stage: 'done', last_stage: 'vlm', verdict: 'keep', reason: null })).toBe('模型验证');
    expect(rowState({ next_stage: 'done', last_stage: 'numeric', verdict: null, reason: 'missing' })).toBe('源文件缺失');
  });
});
