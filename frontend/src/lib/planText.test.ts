import { describe, expect, it } from 'vitest';
import { planGateText, planMergeText, planNoteText } from './planText';

const name = (id: string) => ({ eef_video_consistency: 'EEF–视频一致性', task_success: '任务成败判定' })[id] ?? id;

describe('plan texts in Chinese (fourth round)', () => {
  it('names the gates and the merge strategy', () => {
    expect(planGateText('probe', 64)).toBe('打分 64');
    expect(planGateText('guard_caption', 32)).toBe('护栏打标 32');
    expect(planGateText('someday', 4)).toBe('someday 4');
    expect(planMergeText('none')).toBe('请求合并：不合并');
  });

  it('translates the planner notes it knows and leaves the others alone', () => {
    // exactly as backend/curation/planner/estimates.py writes them
    expect(planNoteText('rough estimate: every selected episode is assumed to pass the hard gates; 22.7 s per request at 67% gate use (v1, 2026-09-07)', name)).toBe(
      '粗估：假设选中的条目都通过硬门，每个请求按 22.7 秒、闸门占用 67% 计（v1 在 2026-09-07 实测）',
    );
    expect(planNoteText('task_success arbitration and label-guard calls depend on the data and are not counted', name)).toBe('任务成败判定的取证仲裁和判废护栏调用视数据而定，没有计入');
    expect(planNoteText('skill_profile text calls (taxonomy, label audit) are per dataset and not counted', name)).toBe('技能画像的文本调用（技能归纳、标注核对）按数据集算，没有计入');
    expect(planNoteText("no request model for ['eef_video_consistency', 'task_success']; not counted", name)).toBe('EEF–视频一致性、任务成败判定没有请求数估算，没有计入');
    expect(planNoteText('something new', name)).toBe('something new');
  });
});
