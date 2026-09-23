import { describe, expect, it } from 'vitest';
import { judgementName, motionFacts, motionRows, syncBadge, syncRows, taskTrail, timestampFacts, violationRows, visualRows } from './episodeReadings';

describe("one episode's readings in Chinese (F6.2)", () => {
  it('timestamps: duration, frames, intervals, where it jumped', () => {
    const { facts, gaps } = timestampFacts({ n: 75, duration_s: 5.533, dt_nominal: 0.066667, max_dt: 0.666667, gap_frames: [{ frame: 36, dt: 0.6667 }], reason: '第 37 帧后间隔突增' });
    expect(facts.map((f) => `${f.label}${f.value}`)).toEqual(['时长5.53 秒', '帧数75', '名义帧间隔0.07 秒', '最大帧间隔0.67 秒']);
    expect(facts[3].warn).toBe(true);
    expect(gaps).toEqual(['第 37 帧后隔了 0.67 秒']);
  });

  it('kinematics: violations with Chinese types and joints, the rest counted', () => {
    const d = { n_violations: 25, violations: Array.from({ length: 22 }, (_, i) => ({ type: i ? 'velocity_limit' : 'joint_limit', joint: i % 2 ? 3 : 'xyz', frame: i, value: 2.51234, limit: i ? 2 : [-2, 2] })) };
    const { rows, more } = violationRows(d);
    expect(rows).toHaveLength(20);
    expect(rows[0]).toMatchObject({ type: '关节超限', joint: '末端位置', frame: '0', value: '2.5123', limit: '-2 ~ 2' });
    expect(rows[1]).toMatchObject({ type: '关节超速', joint: '关节 3', limit: '2' });
    expect(more).toBe(5);
  });

  it('motion: sub-scores with their role, not-applicable ones with the reason; stuck and idle facts', () => {
    const d = { smoothness: 0.9491, spike: 1, actuator_saturation: null, saturation_reason: '指令与读数同源', path_efficiency: 0.09, stuck: null, stuck_reason: '指令与读数同源', idle_head_s: 1.2, idle_tail_s: 0, idle_mid_count: 1, idle_mid_total_s: 0.8, active_ratio: 0.9 };
    expect(motionRows(d).map((r) => [r.name, r.value, r.role, r.note])).toEqual([
      ['平滑度', '0.949', '计入总分', ''],
      ['尖刺', '1', '计入总分', ''],
      ['执行器饱和', '不适用', '—', '指令与读数同源'],
      ['路径效率', '0.09', '只报不罚', ''],
    ]);
    expect(motionFacts(d).map((f) => `${f.label}：${f.value}`)).toEqual(['执行器卡死：不适用：指令与读数同源', '空闲：开头 1.2 秒 · 结尾 0 秒 · 中途停顿 1 次（共 0.8 秒）', '运动占比：90%']);
    expect(motionFacts({ stuck: 0, stuck_joints: [{ axis: 'z', freeze_start_frame: 144, freeze_end_frame: 170 }] })[0]).toMatchObject({ value: '卡死：z（第 144–170 帧）', warn: true });
  });

  it('visual: per camera, short names, placeholders and low scores marked', () => {
    const rows = visualRows({
      per_camera_detail: { 'observation.images.wrist': { score: 0.55, sharpness: 0.5, exposure: 1, integrity: 1, frozen_ratio: 0.01 }, 'observation.images.front': { score: 0.9 } },
      padded_channels: [],
      camera_liveness: { live: [], dead_or_padded: ['observation.images.front'] },
    });
    expect(rows.map((r) => [r.camera, r.score, r.status, r.low])).toEqual([
      ['front', '0.9', '无画面', false],
      ['wrist', '0.55', '正常', true],
    ]);
    expect(rows[1].frozen).toBe('1%');
  });

  it('sync: the badge (疑似错位 for suspects only) and the diagnosis per camera', () => {
    expect(syncBadge({ verdict: 'annotated', suspect_cameras: ['a'], flagged_cameras: [] })).toMatchObject({ text: '疑似错位（证据不足）', color: 'orange' });
    expect(syncBadge({ verdict: 'misaligned_all' }).text).toBe('整体错位（判废）');
    const rows = syncRows({ flagged_cameras: ['b'], per_camera: { a: { lag_s: 0.02, corr_peak: 0.9, trusted: true, diagnosis: { label: '对齐', text: '对得上' } }, b: { lag_s: -0.38, corr_peak: 0.7, corr_at_zero: 0.2, trusted: true, diagnosis: { label: '错位', text: '晚了' } } } });
    expect(rows.map((r) => [r.camera, r.lag, r.trusted, r.label, r.flagged])).toEqual([
      ['a', '+0.02', '是', '对齐', false],
      ['b', '−0.38', '是', '错位', true],
    ]);
  });

  it('task_success: the trail through the layers, unreached ones marked', () => {
    const trail = taskTrail({ verdict: 'arbitration_failure', init_verdict: 'success', strong_score: false, review: 'split', cam_votes: { a: 'no', b: 'no', c: 'yes' }, arbitration: { final: 'no', n_effective: 2 } }, 'fail');
    expect(trail.map((s) => `${s.layer}:${s.text}`)).toEqual(['打分层:成功候选（弱）', '逐机位复核:复核分歧（完成 1 · 未完成 2 · 看不清 0）', '判废护栏:未触发', '取证仲裁:2 路取证，一致判未完成', '结论:判废']);
    expect(trail[2].reached).toBe(false);
    expect(trail[4].tone).toBe('bad');
    expect(taskTrail({}, 'pass').filter((s) => !s.reached)).toHaveLength(4);
    expect(judgementName({ verdict: 'label_conflict_suspect' })).toBe('疑似标注错（护栏拦下）');
  });
});
