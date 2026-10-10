import { describe, expect, it } from 'vitest';
import { zh } from '../locales/zh';
import { eefConclusion, eefCpuEvidence, eefCpuRows, eefMerged, eefStateMotion, eefWindowRows } from './eefReadings';

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
    expect(a.frames).toBe('帧 41');                       // frame 40 of the data, counted from 1
    expect(a.target).toBe('工具中心点（TCP）');
    expect(a.votes.map((v) => `${v.label}：${v.value}:${v.tone}`)).toEqual(['位置：反驳:bad', '朝向：拿不准:none', '绿十字跟对了：支持:good']);
    expect(a.offset).toBe('偏移：偏左，一到两指宽');
    expect([a.explanation, a.cached, a.failure]).toEqual(['红圈在夹爪左边', true, null]);
    expect([b.answered, b.frames, b.failure, b.votes]).toEqual([false, '—', '答复引用了请求里没有的帧', []]);
    expect(c.failure).toBe('something_new');
    expect(eefCpuEvidence(['checks/e/evidence/000003/wrist/k_frame_000040.jpg', 'checks/e/overlays/3.jpg'], [a, b, c])).toEqual(['checks/e/overlays/3.jpg']);
  });
});

describe('eefOpinion (design doc 12 §10.5, D-E15)', () => {
  it('reads the opinion of each camera, the most confident stretch first; null for a judged record', async () => {
    const { eefOpinionRecord, eefRecord } = await import('../mocks/eef');
    const { eefOpinion } = await import('./eefReadings');
    const op = eefOpinion(eefOpinionRecord(3).details as Record<string, unknown>)!;
    expect(op).toMatchObject({ status: 'answered', flagged: true, maxConfidence: 0.85, failure: null, handheld: false, bridged: null });
    const [ext, wrist] = op.cameras;
    expect(ext).toMatchObject({ camera: 'ext', status: 'answered', point: 'tcp', axis: 'z', fingerAxis: 'y', summaries: ['前半段中心偏得明显'], failures: [], unseen: false });
    expect(ext.segments.map((g) => [g.startFrame, g.confidence, g.evidenceFrames.length])).toEqual([[40, 0.85, 2], [180, 0.4, 1]]);
    expect(wrist).toMatchObject({ camera: 'wrist', status: 'skipped', segments: [] });
    expect(eefOpinion(eefRecord(3, 'x').details as Record<string, unknown>)).toBeNull();
  });

  it('knows a handheld gripper\'s opinion and the pose gaps the checks bridged (design doc 22 §5.2)', async () => {
    const { eefOpinion } = await import('./eefReadings');
    const op = eefOpinion({
      assessment_mode: 'vlm_opinion',
      opinion: { status: 'answered', prompt_version: 'umi-action-prompt/7', interpolation: { max_gap_s: 0.100005, frames: { robot0: 2, robot1: 0 } }, cameras: {} },
    })!;
    expect(op).toMatchObject({ handheld: true, bridged: { maxGapMs: 100, hands: [['robot0', 2], ['robot1', 0]] } });
    expect(zh.eefDetail.opinion.bridged(100, op.bridged!.hands)).toBe('位姿缺测：前后相隔不超过 100 ms 的已插值补上（robot0 2 帧、robot1 0 帧），只用于画标记；更长的缺测不画。');
  });

  it('reads a wrist camera\'s own motion: the verdict, each camera\'s readings, stretches and unmatched parts (design doc 22 §5.3)', async () => {
    const { eefHandheldRecord, eefOpinionRecord } = await import('../mocks/eef');
    const { eefEgoMotion } = await import('./eefReadings');
    const ego = eefEgoMotion(eefHandheldRecord(3).details as Record<string, unknown>)!;
    expect(ego).toMatchObject({ status: 'suspect', verdict: 'bad', uncalibrated: true, windowS: 0.5 });
    expect(ego.assumed).toMatch(/^按假设值：/);
    const [ext, wrist] = ego.cameras;
    expect(ext).toMatchObject({ camera: 'ext', status: 'suspect', rotationMedian: 1.99, lagS: 0.467, lagFlagged: true, coverage: 0.969, pairs: 284 });
    expect(ext.segments).toEqual([expect.objectContaining({ startFrame: 50, endFrame: 250, reason: 'time_offset', band: 'moderate', evidenceFrames: [120, 135], lagS: 0.467 })]);
    expect(wrist).toMatchObject({ status: 'unknown', reason: 'pictures_unmatched', rotationMedian: null, lagS: null, segments: [] });
    expect(wrist.unmatched).toEqual([{ startFrame: 15, endFrame: 90, startS: 1.474, endS: 3.973 }]);
    expect(eefEgoMotion(eefOpinionRecord(3).details as Record<string, unknown>)).toBeNull();
  });

  it('reads how the platform had a handheld gripper\'s trajectory (design doc 22 §5.4)', async () => {
    const { eefHandheldRecord, eefOpinionRecord } = await import('../mocks/eef');
    const { eefTrajectorySource } = await import('./eefReadings');
    const src = eefTrajectorySource(eefHandheldRecord(3).details as Record<string, unknown>)!;
    expect(src).toMatchObject({ gripper: 'das_gripper', builtin: true, assumed: ['T_camera_tcp', 'body_to_optical', 'pose_frame'], reason: null });
    expect(src.cameras).toEqual([
      { hand: 'robot0', status: 'ok', reason: null, pairingRate: 0.9987, intrinsics: 'camera_info' },
      { hand: 'robot1', status: 'unsupported', reason: '这一路没有 camera_info，标定文件里也没有它的 intrinsics_fallback', pairingRate: null, intrinsics: null },
    ]);
    expect(src.suspects).toEqual(['有画面的帧里只有 88% 配上了位姿（容差 20 ms）']);
    expect(eefTrajectorySource(eefOpinionRecord(3).details as Record<string, unknown>)).toBeNull();     // an uploaded file
  });

  it('reads a trajectory generated from the declaration, and an opinion nobody was asked for (design doc 25 §3.3, D84)', async () => {
    const { eefOpinion, eefTrajectorySource } = await import('./eefReadings');
    const src = eefTrajectorySource({
      trajectory_source: { kind: 'generated', status: 'generated', declaration: { sha256: 'x', version: 3 }, assumed: ['calibration.tool'], declared_fixed: ['head'] },
    })!;
    expect(src).toMatchObject({ kind: 'generated', version: 3, assumed: ['calibration.tool'], declaredFixed: ['head'], reason: null });
    expect(zh.eefDetail.source.generated(src.version)).toBe('轨迹由平台按数据集声明（第 3 版）生成');
    const op = eefOpinion({ assessment_mode: 'vlm_opinion', opinion: { status: 'not_asked', missing: 'no_vlm_backend', cameras: {} } })!;
    expect([op.status, op.missing]).toEqual(['not_asked', 'no_vlm_backend']);
    expect(zh.eefDetail.opinion.notAsked(zh.eefDetail.output.missing[op.missing!])).toBe('没有问模型（没有模型后端）：这一条只有 CPU 的测量');
  });

  it('says which part of a clip got no answer and why', async () => {
    const { eefOpinion } = await import('./eefReadings');
    const op = eefOpinion({
      assessment_mode: 'vlm_opinion',
      opinion: { status: 'partial', cameras: { ext: { status: 'partial', clips: [{ start_frame: 0, end_frame: 899, status: 'answered', gripper_visible: false, summary: '看不清' }, { start_frame: 900, end_frame: 1000, status: 'failed', failure: { code: 'timeout' } }], segments: [] } } },
    })!;
    expect(op.cameras[0]).toMatchObject({ failures: ['帧 901–1001（101 帧）：模型超时'], unseen: true, summaries: ['看不清'] });
  });
});

describe('eefDatasetRecord (design doc 12 §8.7, D-E16)', () => {
  it('reads each source: where, pairing, residuals, the constant against the declaration, the time offset, the stretches', async () => {
    const { eefDatasetRecordOf } = await import('../mocks/eef');
    const { eefDatasetRecord } = await import('./eefReadings');
    const r = eefDatasetRecord({ record: eefDatasetRecordOf(1) })!;
    expect([r.status, r.statusText, r.reasons]).toEqual(['suspect', '可疑', []]);
    const [pose, joints] = r.sources;
    expect(pose).toMatchObject({ kind: 'pose', name: '位姿列', statusText: '一致', where: '列 observation.state.cartesian_position · xyz_rpy_xyz_extrinsic', frames: '两边都是 panda_link8' });
    expect(pose.lag).toBe('时间差 0.00 帧，不明显');
    expect(joints).toMatchObject({ name: '关节角正解', statusText: '可疑', where: '列 observation.state.joint_position · 机器人 franka_panda', alignment: '按帧号对齐，比了 287 / 287 帧' });
    expect(joints.reasons).toEqual(['恒定差与映射声明的关系不符', '有随时间变化的差']);
    expect(joints.notes).toEqual([]);
    expect(joints.lag).toBe('数据集记录比上传的轨迹晚 1 帧（0.067 秒）');
    expect(joints.residual.map((x) => [x.what, x.position])).toEqual([
      ['按声明关系的差', '中位 26.9 · P95 38.3 · 最大 40'],
      ['扣掉恒定差后', '中位 14.8 · P95 25 · 最大 28.6'],
    ]);
    expect(joints.relation).toEqual([
      { label: '声明的关系', value: '平移 0 mm，转 0°', bad: false },
      { label: '补偿时间差后拟合的恒定差', value: '平移 21.2 mm，转 2.7°', bad: false },
      { label: '与声明相差', value: '平移 21.2 mm，转 2.7°', bad: true },
    ]);
    expect(joints.segments).toEqual(['帧 1–287（287 帧） · 位置，峰值 28.5 mm', '帧 124–235（112 帧） · 姿态，峰值 6.59°']);
    expect(joints.curves?.frame).toEqual([0, 100, 200]);
    expect(r.internal).toEqual({ text: '位姿列与关节角正解对不上（P95 38.3 mm，7.41°）', bad: true });
    expect(r.overlays).toEqual([{ path: 'checks/eef_video_consistency/evidence/000001/record/ext_frame_000148.jpg', camera: 'ext', frame: 148 }]);
    expect(r.legend).toEqual(['红：上传的轨迹', '橙：关节角正解']);
  });

  it('says why nothing could be compared, reports an undeclared constant only, and is null for an older record', async () => {
    const { eefDatasetRecord } = await import('./eefReadings');
    const none = eefDatasetRecord({ record: { status: 'unsupported', reasons: ['record_columns_missing'], sources: {}, message: 'missing column: q' } })!;
    expect([none.statusText, none.reasons, none.message, none.sources]).toEqual(['不支持', ['数据集里找不到映射写的列或 topic'], 'missing column: q', []]);
    const undeclared = eefDatasetRecord({
      record: {
        status: 'ok',
        sources: {
          joints: {
            source: { kind: 'joints', topic: '/arm/joint_states', fields: 'position', robot: 'franka_panda' },
            status: 'ok',
            reasons: [],
            notes: ['constant_unchecked'],
            frame_ids: { upload: 'tcp', record: 'panda_link8' },
            relation: { declared: false, expected: null, fitted: { translation_norm_mm: 103.4, rotation_deg: 0 }, deviation: null },
          },
        },
      },
    })!;
    const [j] = undeclared.sources;
    expect(j.where).toBe('topic /arm/joint_states · 字段 position · 机器人 franka_panda');
    expect(j.frames).toBe('记录的帧 panda_link8 → 上传的帧 tcp');
    expect(j.notes).toEqual(['映射没有声明两边参考点的关系：拟合出的恒定差只报告，不判']);
    expect(j.relation).toEqual([{ label: '拟合的恒定差', value: '平移 103.4 mm，转 0°', bad: false }]);
    expect([j.lag, j.residual, j.curves]).toEqual([null, [], null]);
    // a drafted mapping (D-E17): the pose column's point is not declared
    const drafted = eefDatasetRecord({ record: { status: 'ok', sources: { pose: { source: { kind: 'pose', key: 'p', layout: 'xyz_rpy_xyz_extrinsic', reference_frame: '@upload' }, status: 'ok', frame_ids: { upload: 'panda_link8', record: null } } } } })!;
    expect(drafted.sources[0].frames).toBe('记录是哪个点未声明（映射写的 null）→ 上传的帧 panda_link8');
    expect(eefDatasetRecord({})).toBeNull();
  });

  it('keeps the overlay frames out of the CPU evidence', async () => {
    const { eefRecord } = await import('../mocks/eef');
    const { eefDatasetRecord } = await import('./eefReadings');
    const rec = eefRecord(3, 'x');
    const d = rec.details as Record<string, unknown>;
    const rest = eefCpuEvidence(rec.evidence as string[], eefWindowRows(d), eefDatasetRecord(d)!.overlays.map((o) => o.path));
    expect(rest).toEqual(['checks/eef_video_consistency/overlays/3_ext.jpg']);
  });
});


describe('the opinion with its confidence (registry 5.0, design doc 25 §7)', () => {
  it('reads the episode and every cell, worst first, each side with its verdict and p', async () => {
    const { eefConflictRecord, eefRecord, eefUnderivedRecord } = await import('../mocks/eef');
    const m = eefMerged(eefConflictRecord(3).details as Record<string, unknown>)!;
    expect([m.label, m.p, m.flags, m.conflicts, m.uncalibrated]).toEqual(['inconsistent', 0.95, ['conflict'], 1, true]);
    expect(m.cells.map((c) => [c.subitem, c.p])).toEqual([['position_2d', 0.95], ['temporal_alignment', 0.05], ['orientation_2d', null]]);
    const pos = m.cells[0];
    expect(pos.sources.map((x) => [x.channel, x.verdict, x.p])).toEqual([['cpu', 'issue', 0.95], ['vlm_review', 'ok', 0.1]]);
    expect(pos.timeS).toEqual([8, 10]);
    expect(m.cells[1].missing).toBe('model_cannot_see');
    // a record made before 5.0 has none; an episode nothing could be said about has no p and no cell
    expect(eefMerged(eefRecord(3, 'x').details as Record<string, unknown>)).toBeNull();
    const none = eefMerged(eefUnderivedRecord(3).details as Record<string, unknown>)!;
    expect([none.label, none.p, none.cells]).toEqual(['cannot_tell', null, []]);
  });
});
