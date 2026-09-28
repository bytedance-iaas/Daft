// A test fixture (F5.11 / F5.12): the EEF module's record of an episode it sent to a person. The mock
// world's tasks do not select the module; tests add it through db.extraRecords.
import type { ResultRecord } from '../api/types';

const EEF = 'eef_video_consistency';

/** The EEF module's record of an episode it sent to a person: a candidate the model finds consistent, a window that timed out. */
export function eefRecord(ep: number, why: string): ResultRecord {
  const dir = `checks/${EEF}/evidence/${String(ep).padStart(6, '0')}/ext`;
  return {
    episode_index: ep,
    module: EEF,
    verdict: 'abstain',
    passed: null,
    score: null,
    gate: 'hard',
    elapsed_s: 3.2,
    error: null,
    evidence: [`${dir}/aaaabbbbcccc_frame_000120.jpg`, `${dir}/aaaabbbbcccc_frame_000150.jpg`, `checks/${EEF}/overlays/${ep}_ext.jpg`],
    details: {
      reason: `需要人工裁决：${why}`,
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
