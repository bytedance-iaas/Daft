// A test fixture (F5.11 / F5.12): the EEF module's record of an episode it sent to a person. The mock
// world's tasks do not select the module; tests add it through db.extraRecords.
import type { ResultRecord } from '../api/types';

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

/** The EEF module's record of an episode it sent to a person: a candidate the model finds consistent, a window that timed out. */
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

/** No gripper reference (design doc 12 §10.5, D-E15): the model's opinion on the episode, one stretch flagged. */
export function eefOpinionRecord(ep: number): ResultRecord {
  const dir = `checks/${EEF}/opinion/ep_${String(ep).padStart(6, '0')}/ext`;
  return {
    episode_index: ep,
    module: EEF,
    verdict: 'pass',
    passed: true,
    score: null,
    gate: 'hard',
    elapsed_s: 21.4,
    error: null,
    evidence: [`${dir}/frame_000052.jpg`, `${dir}/frame_000071.jpg`],
    details: {
      assessment_mode: 'vlm_opinion',
      overall: 'opinion',
      reason: '',
      decision: { outcome: 'opinion', human: [], confirmed: [], unchecked: [] },
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
              { start_frame: 180, end_frame: 230, start_s: 12.0, end_s: 15.33, aspect: 'orientation', confidence: 0.4, evidence_frames: [205], observation: '红箭头略偏向桌面', evidence: [] },
              { start_frame: 40, end_frame: 95, start_s: 2.67, end_s: 6.33, aspect: 'position', confidence: 0.85, evidence_frames: [52, 71], observation: '红圈落在手指外侧', evidence: [`${dir}/frame_000052.jpg`, `${dir}/frame_000071.jpg`] },
            ],
          },
          wrist: { status: 'skipped', reason: 'mount wrist does not take part' },
        },
      },
    },
  };
}
