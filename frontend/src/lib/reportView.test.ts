import { describe, expect, it } from 'vitest';
import type { Subtask, Task, TimelineEntry, UsageRow } from '../api/types';
import { callKindLabel, detailsDigest, fieldLabel, formatValue, readable, reasonLine, recordError, revisionOptions, runParts, seconds, subtaskName, usageByCallKind } from './reportView';

const sub = (id: string, kind: Subtask['kind']): Subtask => ({ id, task_id: 't', kind, scope: {}, state: 'succeeded', created_at: 1 });

describe('report view helpers', () => {
  it('labels known keys and keeps unknown ones raw', () => {
    expect(fieldLabel('episode_index')).toBe('Episode');
    expect(fieldLabel('mean')).toBe('平均分');
    expect(fieldLabel('robot_type')).toBe('机器人型号');
    expect(fieldLabel('brand_new_metric')).toBe('brand_new_metric');
  });

  it('formats cells', () => {
    expect(formatValue(null)).toBe('—');
    expect(formatValue(0.8567)).toBe('0.857');
    expect(formatValue(1200)).toBe('1,200');
    expect(formatValue(true)).toBe('是');
    expect(formatValue([])).toBe('无');
    expect(formatValue(['a', 'b'])).toBe('a、b');
    expect(formatValue({ a: 1 })).toBe('a 1');
  });

  it('renders any value readably, never as JSON', () => {
    expect(readable({ mean: 0.5, n: 3 })).toBe('平均分 0.5 · n 3');
    expect(readable({ outer: { mean: 0.5 } })).toBe('outer （平均分 0.5）');
    expect(readable([{ a: 1 }, { a: 2 }])).toBe('a 1；a 2');
    expect(readable([])).toBe('无');
    expect(readable(null)).toBe('—');
    for (const v of [{ a: { b: [1, { c: 2 }] } }, [{ x: null }]]) expect(readable(v)).not.toMatch(/[{}"]/);
  });

  it('digests readings and execution errors', () => {
    expect(detailsDigest({ smoothness: 0.86, completion_end: 0.12 })).toBe('平滑度 0.86 · completion_end 0.12');
    expect(detailsDigest({})).toBe('—');
    expect(recordError({ kind: 'execution', incidents: [{ step: 'arbitration', cause: 'timeout 60s', attempts: 3 }] })).toBe('arbitration：timeout 60s（3 次）');
    expect(recordError(null)).toBe('');
  });

  it('reads reason items leniently', () => {
    const reg = { registry_version: '1', stages: [], modules: [{ id: 'dedup', name_zh: '精确去重' }] } as never;
    expect(reasonLine({ module: 'dedup', text: '与 ep 43 重复' }, reg)).toBe('精确去重 · 与 ep 43 重复');
    expect(reasonLine({ foo: 1 }, reg)).toBe('foo 1');
  });

  it('adds up the runs behind a revision when the report has no duration', () => {
    const task = { started_at: 1_000, finished_at: 9_999_999 } as Task;
    const timeline: TimelineEntry[] = [
      { at: 1_000, kind: 'started', text: '', subtask_id: null, revision: null },
      { at: 1_017_000, kind: 'finished', text: '', subtask_id: null, revision: 1 },
    ];
    const subs = [
      { ...sub('a', 'retry'), started_at: 2_000_000, finished_at: 2_412_000, result_rev: 2 },
      { ...sub('b', 'reexport'), started_at: 3_000_000, finished_at: 3_060_000, result_rev: null },
    ];
    expect(runParts(task, timeline, subs, 2)).toEqual([
      { label: '主流程', seconds: 1016 },
      { label: '重试 #1', seconds: 412 },
    ]);
    expect(runParts(task, timeline, subs, 1)).toEqual([{ label: '主流程', seconds: 1016 }]);
    expect(runParts({ started_at: null } as Task, [], [], 1)).toEqual([]);
  });

  it('names subtasks by kind and ordinal', () => {
    const subs = [sub('a', 'retry'), sub('b', 'reexport'), sub('c', 'retry')];
    expect(subtaskName(subs, 'c')).toBe('重试 #2');
    expect(subtaskName(subs, 'b')).toBe('重新导出 #1');
    expect(subtaskName(subs, '')).toBe('主流程');
    expect(subtaskName(subs, 'zzz')).toBe('zzz');
  });

  it('lists revisions newest first from the timeline', () => {
    const at = new Date(2026, 8, 20, 13, 22).getTime();
    const entries: TimelineEntry[] = [
      { at, kind: 'revision', text: 'r1', revision: 1, subtask_id: null },
      { at: at + 60_000 * 47, kind: 'revision', text: 'r2', revision: 2, subtask_id: 'a' },
    ];
    expect(revisionOptions(entries, 2, [sub('a', 'retry')])).toEqual([
      { value: 2, label: 'r0002 · 当前 · 重试 #1 · 09-20 14:09' },
      { value: 1, label: 'r0001 · 主流程结束 · 09-20 13:22' },
    ]);
    expect(revisionOptions([], 2)).toEqual([
      { value: 2, label: 'r0002 · 当前' },
      { value: 1, label: 'r0001' },
    ]);
  });

  it('sums token usage by call kind within a perf scope', () => {
    const row = (subtask_id: string, call_kind: UsageRow['call_kind'], prompt: number): UsageRow => ({
      subtask_id,
      module_id: 'm',
      call_kind,
      model_name: 'x',
      prompt_tokens: prompt,
      completion_tokens: 1,
      reasoning_tokens: 0,
      cached_tokens: 0,
      requests: 1,
      requests_unknown_usage: 0,
    });
    const rows = [row('', 'probe', 10), row('', 'probe', 5), row('s1', 'probe', 7), row('s1', 'caption', 3)];
    expect(usageByCallKind(rows, 'all', null).map((r) => [r.call_kind, r.prompt, r.requests])).toEqual([
      ['probe', 22, 3],
      ['caption', 3, 1],
    ]);
    expect(usageByCallKind(rows, 'main', null).map((r) => r.prompt)).toEqual([15]);
    expect(usageByCallKind(rows, 'subtask', 's1').map((r) => r.call_kind)).toEqual(['probe', 'caption']);
  });

  it('keeps the v1 call kind labels verbatim', () => {
    expect(callKindLabel('arbitration')).toBe('取证仲裁 · arbitration');
    expect(callKindLabel('new_kind')).toBe('new_kind');
    expect(seconds(3.14)).toBe('3.1 秒');
    expect(seconds(402)).toBe('6 分 42 秒');
  });
});
