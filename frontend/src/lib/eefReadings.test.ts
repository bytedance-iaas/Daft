import { describe, expect, it } from 'vitest';
import { eefConclusion, eefCpuEvidence, eefCpuRows, eefStateMotion, eefWindowRows } from './eefReadings';

const details = {
  reason: '「位置」（相机 wrist）CPU 与模型都认为不一致（模型反对 2、支持 0）',
  decision: {
    outcome: 'reject',
    confirmed: [{ code: 'confirmed', text: '「位置」（相机 wrist）CPU 与模型都认为不一致（模型反对 2、支持 0）', subitem: 'position_2d', camera_id: 'wrist' }],
    human: [{ code: 'model_cannot_see', text: '「时间对齐」（相机 wrist）CPU 判为可疑，模型看不了这一项', subitem: 'temporal_alignment', camera_id: 'wrist' }],
    unchecked: [{ subitem: 'orientation_2d', camera_id: 'wrist', status: 'unknown' }, { subitem: 'state_motion', status: 'unsupported' }],
  },
  cameras: { wrist: { mount: 'wrist', subitems: { position_2d: { status: 'suspect' }, temporal_alignment: { status: 'suspect' }, orientation_2d: { status: 'unknown' } } } },
  state_motion: { status: 'unsupported' },
  review: {
    cameras: {
      wrist: {
        windows: [
          {
            kind: 'candidate',
            subitem: 'position_2d',
            frames: [40],
            point_id: 'tcp',
            axis_id: null,
            status: 'answered',
            cache_hit: true,
            answer: { position_support: 'refute', orientation_support: 'uncertain', tracking_target_correct: 'support', offset_direction: 'left', offset_magnitude_class: 'one_to_two_finger_widths', explanation: '红圈在夹爪左边' },
            evidence: ['checks/e/evidence/000003/wrist/k_frame_000040.jpg'],
          },
          { kind: 'uniform', frames: [], status: 'failed', failure: { code: 'unknown_frame' } },
          { kind: 'uniform', frames: [1, 2], status: 'failed', failure: { code: 'something_new' } },
        ],
      },
    },
  },
};

describe('the EEF record for a person (F5.11, F5.12)', () => {
  it('reads the conclusion, the reasons and what was not checked', () => {
    const c = eefConclusion(details);
    expect(c.outcome).toBe('reject');
    expect(c.confirmed.map((x) => [x.code, x.camera])).toEqual([['confirmed', 'wrist']]);
    expect(c.human[0].text).toContain('模型看不了这一项');
    expect(c.unchecked).toEqual(['朝向（相机 wrist）：无法评估', '状态运动：不支持']);
    expect(eefConclusion({}).human).toEqual([]);
  });

  it('reads the CPU per camera and marks the suspect sub-items', () => {
    const [row] = eefCpuRows(details);
    expect(row.camera).toBe('wrist');
    expect(row.cells).toMatchObject({ position_2d: '可疑', orientation_2d: '无法评估', camera_motion: '—' });
    expect(row.suspect).toEqual(['position_2d', 'temporal_alignment']);
    expect(eefStateMotion(details)).toBe('不支持');
    expect(eefStateMotion({})).toBeNull();
  });

  it('reads every window: votes, offset, the model words, failures, crops', () => {
    const [a, b, c] = eefWindowRows(details);
    expect(a.title).toBe('候选段 · 位置');
    expect(a.frames).toBe('帧 40');
    expect(a.target).toBe('点 tcp');
    expect(a.votes.map((v) => `${v.label}：${v.value}:${v.tone}`)).toEqual(['位置：反驳:bad', '朝向：拿不准:none', '绿十字跟对了：支持:good']);
    expect(a.offset).toBe('偏移：偏左，一到两指宽');
    expect([a.explanation, a.cached, a.failure]).toEqual(['红圈在夹爪左边', true, null]);
    expect([b.answered, b.frames, b.failure, b.votes]).toEqual([false, '—', '答复引用了请求里没有的帧', []]);
    expect(c.failure).toBe('something_new');
    expect(eefCpuEvidence(['checks/e/evidence/000003/wrist/k_frame_000040.jpg', 'checks/e/overlays/3.jpg'], [a, b, c])).toEqual(['checks/e/overlays/3.jpg']);
  });
});
