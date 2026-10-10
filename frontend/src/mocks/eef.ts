// A test fixture (F5.11 / F5.12): the EEF module's record of an episode it sent to a person. The mock
// world's tasks do not select the module; tests add it through db.extraRecords.
import type { EefOverlay, EefOverlayLayer, ResultRecord } from '../api/types';

const EEF = 'eef_video_consistency';

const ZERO = { translation_mm: [0, 0, 0], translation_norm_mm: 0, rotation_deg: 0, rotation_axis: [0, 0, 1] };

/**
 * The trajectory against the dataset's own record (design doc 12 §8.7, D-E16), as dataset2's episode 1
 * reads: the pose columns agree (the file came from them), the joints' kinematics differ from the declared
 * relation by a constant and by a drift; the dataset's own two records disagree. The one-frame time
 * offset is made up, to show one.
 */
export function eefDatasetRecordOf(ep: number): Record<string, unknown> {
  const dir = `checks/${EEF}/evidence/${String(ep).padStart(6, '0')}/record`;
  const curves = (raw: number[], after: number[]) => ({ frame: [0, 100, 200], t_s: [0, 6.67, 13.33], position_mm: raw, rotation_deg: [0, 3.6, 7.4], position_after_constant_mm: after, rotation_after_constant_deg: [2.7, 1.2, 4.7] });
  return {
    status: 'suspect',
    reasons: ['constant_mismatch', 'record_deviation'],
    mapping_sha256: '7fa8c7724adc242c406378e50c167abbef391c751e80bf1fb9ca895903fb09ec',
    sources: {
      pose: {
        source: { kind: 'pose', key: 'observation.state.cartesian_position', layout: 'xyz_rpy_xyz_extrinsic', frame_id: 'panda_link8', reference_frame: 'robot_base' },
        alignment: 'frame_index',
        frames_compared: 287,
        frames_with_pose: 287,
        frame_ids: { upload: 'panda_link8', record: 'panda_link8' },
        status: 'ok',
        reasons: [],
        notes: [],
        relation: { declared: true, expected: ZERO, fitted: ZERO, fitted_after_time_offset: null, deviation: ZERO },
        residual: { position_mm: { median: 0.005, p95: 0.007, max: 0.008 }, rotation_deg: { median: 0.001, p95: 0.001, max: 0.001 } },
        residual_after_constant: { position_mm: { median: 0.005, p95: 0.007, max: 0.008 }, rotation_deg: { median: 0.001, p95: 0.001, max: 0.001 } },
        segments: [],
        time_offset: { lag_s: 0, lag_frames: 0, improvement: 1, edge_at_limit: false },
        curves: curves([0.007, 0.006, 0.005], [0.007, 0.006, 0.005]),
      },
      joints: {
        source: { kind: 'joints', key: 'observation.state.joint_position', robot: 'franka_panda', frame_id: 'panda_link8', reference_frame: 'robot_base' },
        alignment: 'frame_index',
        frames_compared: 287,
        frames_with_pose: 287,
        frame_ids: { upload: 'panda_link8', record: 'panda_link8' },
        status: 'suspect',
        reasons: ['constant_mismatch', 'record_deviation'],
        notes: ['time_offset'],
        relation: {
          declared: true,
          expected: ZERO,
          fitted: { translation_mm: [-4.18, -15.0, 14.3], translation_norm_mm: 21.14, rotation_deg: 2.74, rotation_axis: [-0.096, -0.075, -0.993] },
          fitted_after_time_offset: { translation_mm: [-4.2, -15.1, 14.2], translation_norm_mm: 21.2, rotation_deg: 2.7, rotation_axis: [-0.096, -0.075, -0.993] },
          deviation: { translation_mm: [-4.2, -15.1, 14.2], translation_norm_mm: 21.2, rotation_deg: 2.7, rotation_axis: [-0.096, -0.075, -0.993] },
        },
        residual: { position_mm: { median: 26.88, p95: 38.273, max: 40.005 }, rotation_deg: { median: 3.624, p95: 7.414, max: 8.0 } },
        residual_after_constant: { position_mm: { median: 14.759, p95: 24.972, max: 28.594 }, rotation_deg: { median: 2.723, p95: 6.009, max: 6.607 } },
        segments: [
          { start_frame: 0, end_frame: 286, start_s: 0, end_s: 19.067, duration_s: 19.067, peak: 28.489, mean: 14.764, valid_frames: 287, reasons: ['record_deviation'], aspect: 'position', evidence_frames: [230, 231, 232] },
          { start_frame: 123, end_frame: 234, start_s: 8.2, end_s: 15.6, duration_s: 7.4, peak: 6.594, mean: 3.816, valid_frames: 112, reasons: ['record_deviation'], aspect: 'rotation', evidence_frames: [148, 149, 150] },
        ],
        time_offset: { lag_s: 0.0667, lag_frames: 1.0, improvement: 2.4, edge_at_limit: false },
        curves: curves([0.007, 26.9, 38.3], [21.1, 14.8, 25.0]),
      },
    },
    internal: { compared: true, consistent: false, relation_declared: true, position_mm: { median: 26.884, p95: 38.273, max: 40.0 }, rotation_deg: { median: 3.623, p95: 7.414, max: 8.0 } },
    evidence: [{ camera_id: 'ext', frame_index: 148, path: `${dir}/ext_frame_000148.jpg`, kind: 'record', legend: { upload: 'red', joints: 'orange' } }],
  };
}

/** The EEF module's record of an episode it sent to a person (before registry 5.0): a candidate the model finds consistent, a window that timed out. */
export function eefRecord(ep: number, why: string): ResultRecord {
  const dir = `checks/${EEF}/evidence/${String(ep).padStart(6, '0')}/ext`;
  const record = eefDatasetRecordOf(ep);
  return {
    episode_index: ep,
    module: EEF,
    verdict: 'abstain',
    passed: null,
    score: null,
    gate: 'hard',
    elapsed_s: 3.2,
    error: null,
    evidence: [
      `${dir}/aaaabbbbcccc_frame_000120.jpg`,
      `${dir}/aaaabbbbcccc_frame_000150.jpg`,
      `checks/${EEF}/overlays/${ep}_ext.jpg`,
      ...(record.evidence as { path: string }[]).map((e) => e.path),
    ],
    details: {
      reason: `需要人工裁决：${why}`,
      record,
      decision: { outcome: 'human', human: [{ code: 'conflict', text: why, subitem: 'position_2d', camera_id: 'ext' }], confirmed: [], unchecked: [] },
      cameras: { ext: { mount: 'fixed_external', subitems: { position_2d: { status: 'suspect' }, orientation_2d: { status: 'ok' }, temporal_alignment: { status: 'ok' }, camera_motion: { status: 'ok' }, input_consistency: { status: 'ok' } } } },
      state_motion: { status: 'ok' },
      review: {
        status: 'incomplete',
        cameras: {
          ext: {
            status: 'incomplete',
            windows: [
              {
                camera_id: 'ext',
                kind: 'candidate',
                subitem: 'position_2d',
                frames: [120, 135, 150],
                point_id: 'block_center',
                axis_id: 'gripper_x',
                status: 'answered',
                attempts: 1,
                cache_hit: false,
                answer: { review_status: 'support', target_visible: true, tracking_target_correct: 'support', position_support: 'support', orientation_support: 'not_observable', offset_direction: 'none', offset_magnitude_class: 'none', evidence_frame_ids: [135], reason_codes: [], explanation: '红圈压在夹爪指尖中间' },
                conflict: { subitem: 'position_2d', cpu: 'suspect', vlm: 'support' },
                evidence: [`${dir}/aaaabbbbcccc_frame_000120.jpg`, `${dir}/aaaabbbbcccc_frame_000150.jpg`],
              },
              { camera_id: 'ext', kind: 'uniform', frames: [300, 310, 320], point_id: 'block_center', axis_id: null, status: 'failed', attempts: 2, cache_hit: false, failure: { code: 'timeout', message: 'no answer in time' } },
            ],
          },
        },
      },
    },
  };
}

/** A merged cell of registry 5.0 (design doc 25 §7). */
function cell(subitem: string, camera: string | null, p: number | null, sources: Record<string, Record<string, unknown>>, extra: Record<string, unknown> = {}) {
  const label = p === null ? 'cannot_tell' : p >= 0.7 ? 'inconsistent' : p >= 0.4 ? 'possibly_inconsistent' : 'consistent';
  return { subitem, camera, p, label, flags: [] as string[], sources, ...extra };
}

/**
 * {@link eefRecord} as registry 5.0 writes it (design doc 25 §7, D81): no verdict - the CPU finds position off on
 * `ext` (0.95), the model's candidate windows say it is fine (0.1): a conflict, the card a person answers; time
 * alignment is the CPU's alone.
 */
export function eefConflictRecord(ep: number): ResultRecord {
  const base = eefRecord(ep, 'x');
  const { decision: _decision, ...details } = base.details as Record<string, unknown>;
  const pos = cell('position_2d', 'ext', 0.95, { cpu: { verdict: 'issue', p: 0.95, confidence: 0.9 }, vlm_review: { verdict: 'ok', p: 0.1, confidence: 0.8 } }, {
    flags: ['conflict'],
    time_s: [8.0, 10.0],
  });
  const tem = cell('temporal_alignment', 'ext', 0.05, { cpu: { verdict: 'ok', p: 0.05, confidence: 0.9 } }, { flags: ['single_source'], missing: 'model_cannot_see' });
  const ori = cell('orientation_2d', 'ext', null, { cpu: { verdict: 'cannot_tell', p: null, why: 'axis_mapping_missing' }, vlm_review: { verdict: 'cannot_tell', p: null, why: 'no_vote' } }, { why: ['axis_mapping_missing', 'no_vote'] });
  return {
    ...base,
    verdict: 'abstain',
    passed: true,
    details: {
      ...details,
      reason: '不一致 · 0.95 · 冲突：位置（相机 ext），CPU认为不一致（0.95），模型复核认为一致（0.10）',
      merged: {
        cells: [pos, tem, ori],
        episode: { label: 'inconsistent', p: 0.95, subitem: 'position_2d', camera: 'ext', flags: ['conflict'], conflicts: 1, reason: '不一致 · 0.95 · 冲突：位置（相机 ext），CPU认为不一致（0.95），模型复核认为一致（0.10）' },
        profile: { calibrated: false, name: 'demo' },
      },
    },
  };
}

/** No gripper reference (design doc 12 §10.5, D-E15): the model's opinion on the episode, one stretch flagged. */
export function eefOpinionRecord(ep: number): ResultRecord {
  return {
    episode_index: ep,
    module: EEF,
    verdict: 'pass',
    passed: true,
    score: null,
    gate: 'hard',
    elapsed_s: 21.4,
    error: null,
    evidence: [],
    details: {
      assessment_mode: 'vlm_opinion',
      overall: 'opinion',
      reason: '不一致 · 0.80：位置（相机 ext），模型意见认为不一致（0.85），只有一个渠道：没给夹爪参考',
      merged: {
        cells: [
          cell('position_2d', 'ext', 0.8, { vlm_opinion: { verdict: 'issue', p: 0.85, confidence: 0.7 } }, { flags: ['single_source'], missing: 'no_gripper_reference', time_s: [2.67, 6.33] }),
          cell('orientation_2d', 'ext', 0.4, { vlm_opinion: { verdict: 'ok', p: 0.4, confidence: 0.2 } }, { flags: ['single_source'], missing: 'no_gripper_reference' }),
          cell('temporal_alignment', 'ext', 0.0, { vlm_opinion: { verdict: 'ok', p: 0.0, confidence: 1.0 } }, { flags: ['single_source'], missing: 'no_gripper_reference' }),
        ],
        episode: { label: 'inconsistent', p: 0.8, subitem: 'position_2d', camera: 'ext', flags: ['single_source'], conflicts: 0, reason: '不一致 · 0.80：位置（相机 ext），模型意见认为不一致（0.85），只有一个渠道：没给夹爪参考' },
        profile: { calibrated: false, name: 'demo' },
      },
      record: { status: 'unsupported', reasons: ['record_mapping_missing'], sources: {} },
      opinion: {
        protocol: 'eef-opinion/1',
        status: 'answered',
        segments: 2,
        flagged: true,
        max_confidence: 0.85,
        cameras: {
          ext: {
            status: 'answered',
            point_id: 'tcp',
            axis_id: 'z',
            finger_axis_id: 'y',
            clips: [{ start_frame: 0, end_frame: 286, status: 'answered', attempts: 1, cache_hit: false, gripper_visible: true, summary: '前半段中心偏得明显' }],
            segments: [
              { start_frame: 180, end_frame: 230, start_s: 12.0, end_s: 15.33, aspect: 'orientation', confidence: 0.4, evidence_frames: [205], observation: '红箭头略偏向桌面' },
              { start_frame: 40, end_frame: 95, start_s: 2.67, end_s: 6.33, aspect: 'position', confidence: 0.85, evidence_frames: [52, 71], observation: '红圈落在手指外侧' },
            ],
          },
          wrist: { status: 'skipped', reason: 'mount wrist does not take part' },
        },
      },
    },
  };
}

/**
 * {@link eefOpinionRecord} with a wrist camera's own motion against its poses (design doc 22 §5.3): camera `ext`
 * (the one with marks in the mock) has its poses 0.47 s late, `wrist` matched too few pictures to say.
 */
export function eefHandheldRecord(ep: number): ResultRecord {
  const base = eefOpinionRecord(ep);
  const seg = { start_frame: 50, end_frame: 250, start_s: 1.683, end_s: 8.35, reason: 'time_offset', magnitude: 0.467, unit: 's', band: 'moderate', band_level: 2, evidence_frames: [120, 135], lag_s: 0.467 };
  const merged = (base.details as { merged: { cells: unknown[]; episode: unknown; profile: unknown } }).merged;
  return {
    ...base,
    details: {
      ...(base.details as Record<string, unknown>),
      // the wrist camera's own motion is its own cell, the ego-motion channel's alone (design doc 25 §6)
      merged: {
        ...merged,
        cells: [
          ...merged.cells,
          cell('ego_motion', 'ext', 0.8, { ego: { verdict: 'issue', p: 0.97, confidence: 0.94 } }, { flags: ['single_source'], missing: 'model_cannot_see', time_s: [1.683, 8.35] }),
        ],
      },
      // no trajectory.json: the platform derived it from the recording (design doc 22 §5.4)
      trajectory_source: {
        kind: 'derived',
        calibration: { gripper: 'das_gripper', builtin: true, sha256: 'a'.repeat(64), assumed: ['T_camera_tcp', 'body_to_optical', 'pose_frame'], intrinsics_fallback: [] },
        status: 'ok',
        rows: 287,
        cameras: {
          robot0: { status: 'ok', reason: null, pairing_rate: 0.9987, intrinsics: 'camera_info', first_decodable: 0 },
          robot1: { status: 'unsupported', reason: '这一路没有 camera_info，标定文件里也没有它的 intrinsics_fallback', pairing_rate: null, intrinsics: null, first_decodable: 0 },
        },
        suspects: [{ code: 'timing_suspect', row: 7, camera: 'robot0', detail: '有画面的帧里只有 88% 配上了位姿（容差 20 ms）' }],
      },
      ego_motion: {
        status: 'suspect',
        verdict: 'bad',
        explanation_zh: '腕部相机的运动与记录的位姿不一致：ext：位姿约晚 0.47 s（第 51–251 帧，中）',
        uncalibrated: true,
        window_s: 0.5,
        assumed: '按假设值：T_camera_tcp、body_to_optical、pose_frame 是假设，不是夹爪厂商的声明（设计 22 §7）。',
        worst: { camera: 'ext', ...seg },
        cameras: {
          ext: {
            status: 'suspect',
            reason: null,
            metrics: { pairs: 284, attempted: 293, coverage: 0.969, rotation_median_deg: 1.99, rotation_p95_deg: 8.0, direction_median_deg: 39.2, lag_s: 0.467, lag_confidence: 0.8 },
            lag: { lag_s: 0.467, confidence: 0.8, flagged: true, median_at_zero_deg: 1.99, median_at_lag_deg: 0.41, searched_s: 1.0 },
            segments: [seg],
            unmatched: [],
          },
          wrist: {
            status: 'unknown',
            reason: 'pictures_unmatched',
            metrics: { pairs: 53, attempted: 124, coverage: 0.427 },
            lag: null,
            segments: [],
            unmatched: [{ start_frame: 15, end_frame: 90, start_s: 1.474, end_s: 3.973, reason: 'pictures_unmatched' }],
          },
        },
      },
    },
  };
}

/**
 * A handheld gripper's episode the platform could not derive a trajectory for (design doc 22 §5.4): a recording
 * without camera_info under the built-in calibration. The opinion did not look, asks nobody and passes.
 */
export function eefUnderivedRecord(ep: number): ResultRecord {
  const why = '这一条推不出轨迹；robot0：这一路没有 camera_info，标定文件里也没有它的 intrinsics_fallback';
  return {
    ...eefOpinionRecord(ep),
    verdict: 'abstain',
    passed: null,
    elapsed_s: 0.2,
    details: {
      episode_index: ep,
      assessment_mode: 'vlm_opinion',
      overall: 'opinion',
      reason: '',
      merged: { cells: [], episode: { label: 'cannot_tell', p: null, flags: [], why: [], conflicts: 0, reason: `判断不了：${why}` }, profile: { calibrated: false, name: 'demo' } },
      opinion: { protocol: 'eef-opinion/1', status: 'not_assessable', cameras: {}, segments: 0, flagged: false, max_confidence: null, requests: 0, failure: why },
      trajectory_source: {
        kind: 'derived',
        calibration: { gripper: 'das_gripper', builtin: true, sha256: 'a'.repeat(64), assumed: ['T_camera_tcp', 'body_to_optical', 'pose_frame'], intrinsics_fallback: [] },
        status: 'unsupported',
        reason: 'trajectory_not_derived',
        rows: 0,
        cameras: { robot0: { status: 'unsupported', reason: '这一路没有 camera_info，标定文件里也没有它的 intrinsics_fallback', pairing_rate: null, intrinsics: null, first_decodable: null } },
        suspects: [],
      },
    },
  };
}

/**
 * The marks of {@link eefOpinionRecord}'s camera `ext` (design docs 20, 22 §3.2): P circling slowly with
 * its past and future trails, A pointing down (off by default), B across the fingers, the tool's three
 * axes, over a 640×480 clip of 287 frames at 15 fps shown at frame / 15 s. ``observed`` adds the measured
 * episode's group: the observed P a few pixels off, its trail and the residual line.
 */
export function eefOverlay(taskId: string, episode: number, vizCamera: string | null, observed = false): EefOverlay {
  const frames = 287;
  const at = (f: number): [number, number] => [320 + 120 * Math.cos(f / 40), 260 + 60 * Math.sin(f / 40)];
  const round = (v: number) => Math.round(v * 10) / 10;
  const span = (f: number, from: number, to: number) =>
    Array.from({ length: to - from + 1 }, (_, i) => at(f + from + i)).flat().map(round);
  const trail = Array.from({ length: frames }, (_, f) => (f < 1 ? null : span(f, -Math.min(f, 15), 0)));
  const future = Array.from({ length: frames }, (_, f) => (f > frames - 2 ? null : span(f, 0, Math.min(frames - 1 - f, 15))));
  const each = (fn: (p: [number, number]) => number[]) => Array.from({ length: frames }, (_, f) => fn(at(f)).map(round));
  const layer = (id: string, group: EefOverlayLayer['group'], kind: EefOverlayLayer['kind'], title: string, color: string, fr: EefOverlayLayer['frames'], more: Partial<EefOverlayLayer> = {}): EefOverlayLayer => ({
    id, group, title, kind, label: null, color, width: 2, frames: fr, default_on: true, hand: 'eef', in_model: false, model_color: null, ...more,
  });
  const seen = ([x, y]: [number, number]) => [x + 6, y - 3];
  const layers: EefOverlayLayer[] = [
    layer('trail_past', 'trail_past', 'polyline', '过去轨迹', '#00dcff', trail, { in_model: !observed }),
    layer('trail_future', 'trail_future', 'polyline', '未来轨迹', '#a5f3fc', future, { default_on: false }),
    ...(observed ? [layer('observed_trail', 'observed', 'polyline', '观测轨迹', '#00ff00', Array.from({ length: frames }, (_, f) => (f < 1 ? null : span(f, -Math.min(f, 15), 0).map((v, i) => (i % 2 ? v - 3 : v + 6)))))] : []),
    layer('finger_axis', 'declared', 'segment', '两指连线 B（y）', '#ffa500', each(([x, y]) => [x - 30, y, x + 30, y]), { label: 'B', in_model: !observed }),
    layer('axis_x', 'axes', 'arrow', '坐标轴 x', '#ff3b30', each(([x, y]) => [x, y, x + 40, y + 8])),
    layer('axis_y', 'axes', 'arrow', '坐标轴 y', '#34c759', each(([x, y]) => [x, y, x - 12, y - 36])),
    layer('axis_z', 'axes', 'arrow', '坐标轴 z', '#2f7bff', each(([x, y]) => [x, y, x, y + 50])),
    layer('axis', 'declared', 'arrow', '朝向 A（z）', '#b26bff', each(([x, y]) => [x, y, x, y + 50]), { label: 'A', default_on: false, in_model: true, model_color: '#ff0000' }),
    ...(observed
      ? [layer('residual', 'residual', 'segment', '残差线', '#ffd400', each((p) => [...p, ...seen(p)]), { default_on: false }),
         layer('observed_point', 'observed', 'cross', '观测点（tcp）', '#00ff00', each(seen), { in_model: true })]
      : []),
    layer('point', 'declared', 'point', '中心点 P（tcp）', '#ff0000', each(([x, y]) => [x, y]), { label: 'P', in_model: true }),
  ];
  return {
    task_id: taskId,
    dataset_id: null,
    episode_index: episode,
    cameras: [
      {
        camera_id: 'ext',
        viz_camera: vizCamera,
        image_size_wh: [640, 480],
        fps: 15,
        mount: 'fixed_external',
        media_frames: Array.from({ length: frames }, (_, f) => f),
        times_s: Array.from({ length: frames }, (_, f) => (vizCamera ? Math.round((f / 15) * 1e6) / 1e6 : null)),
        hands: [{ id: 'eef', title: 'panda_link8', color: '#ff0000', opening_m: Array.from({ length: frames }, (_, f) => Math.round(0.085 * (0.5 + 0.5 * Math.cos(f / 30)) * 1e4) / 1e4) }],
        skipped: null,
        layers,
      },
      { camera_id: 'wrist', viz_camera: null, image_size_wh: [640, 480], fps: 15, mount: 'wrist', media_frames: [], times_s: [], hands: [], skipped: 'mount wrist does not take part', layers: [] },
    ],
    interpolation: null,
  };
}
