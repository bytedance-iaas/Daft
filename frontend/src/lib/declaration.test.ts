import { describe, expect, it } from 'vitest';
import type { Declaration } from '../api/types';
import { applyTemplate, canGenerate, cameraReason, fxcxfycy, missingOf, parseDeclaration, setCamera, shortName, withFour } from './declaration';

const FRONT = 'observation.images.front';
const base: Declaration = {
  schema_version: 'dataset-declaration/1.0',
  semantics: {
    pose: { key: 'observation.state.cartesian_position', layout: 'xyz_rpy_xyz_extrinsic', units: { position: 'm' }, frame_id: 'panda_link8', reference_frame: 'robot_base' },
  },
  calibration: { cameras: {}, tool: { tcp_offset_m: [0, 0, 0.16] } },
};

describe('the dataset declaration form (design doc 25 §3)', () => {
  it('names cameras by their short name', () => {
    expect(shortName('observation.images.exterior_1_left')).toBe('exterior_1_left');
    expect(shortName('/robot0/sensor/camera0/compressed')).toBe('robot0_camera0');
  });

  it('says what generating the trajectory still lacks, as the Daemon does', () => {
    expect(missingOf(null, [FRONT])).toEqual([{ field: 'semantics.pose', code: 'pose_missing' }]);
    expect(missingOf(base, [FRONT]).map((m) => m.code)).toEqual(['mount_unknown', 'no_drawable_camera']);
    const withCam = setCamera(base, FRONT, { mount: 'fixed_external', intrinsics: null, extrinsics: { mode: 'column', key: 'camera_extrinsics.front', layout: 'xyz_rpy' } });
    expect(cameraReason(withCam.calibration!.cameras![FRONT])).toBe('intrinsics_missing');
    expect(canGenerate(withCam, [FRONT])).toBe(false);
    const done = setCamera(withCam, FRONT, { intrinsics: withFour(null, [500, 320, 500, 240]) });
    expect(fxcxfycy(done.calibration!.cameras![FRONT].intrinsics)).toEqual([500, 320, 500, 240]);
    expect(canGenerate(done, [FRONT])).toBe(true);
    expect(base.calibration!.cameras).toEqual({});                                    // edits copy
    expect(withFour(null, [0, 1, 1, 1])).toBeNull();
    const wrist = setCamera(done, 'observation.images.wrist', { mount: 'wrist', intrinsics: withFour(null, [1, 1, 1, 1]) });
    expect(cameraReason(wrist.calibration!.cameras!['observation.images.wrist'])).toBe('camera_tcp_missing');
  });

  it('reads an mcap mapping as the first layer and lays a template on', () => {
    const m = parseDeclaration(JSON.stringify({ schema_version: 'viz-mapping/1.1', cameras: [], series: [] }));
    expect(m.schema_version).toBe('dataset-declaration/1.0');
    expect(() => parseDeclaration('{"schema_version": "x"}')).toThrow();
    const tpl: Declaration = { schema_version: 'dataset-declaration/1.0', calibration: { tool: { tcp_offset_m: [0, 0, 0.1034], max_opening_m: 0.08, finger_axis: 'local_y' } } };
    const out = applyTemplate({ ...base, suspects: [{ code: 'opening_range', field: 'x', message: 'm' }] }, tpl);
    expect(out.calibration!.tool!.tcp_offset_m).toEqual([0, 0, 0.1034]);
    expect(out.semantics!.pose!.frame_id).toBe('panda_link8') ;
    expect(out.suspects).toBeUndefined();
  });
});
