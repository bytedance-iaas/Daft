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
