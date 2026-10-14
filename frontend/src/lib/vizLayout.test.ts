import { describe, expect, it } from 'vitest';
import type { VizCamera, VizStream } from '../api/types';
import { armStream, cameraOfScope, cellHeight, cellValid, fitCells, GRID_SIZES, gridKey, miniLayout, ownerOf, parseGridKey, playerChips, resizeCells, shapeFor, smartColumns, templateLayout, widthClass } from './vizLayout';

function cam(key: string, hidden = false): VizCamera {
  return { key, name: key, source: `observation.images.${key}`, kind: 'video', access: 'direct', codec: 'h264', codec_string: 'avc1.640028', width: 640, height: 480, fps: 30, transcoded: false, reason: null, hidden };
}

function stream(key: string, smart = true, kind: VizStream['kind'] = 'series', available = true): VizStream {
  return { key, kind, name: key, unit: null, lines: [], smart, available, reason: null, sources: [key] };
}

const k = (c: { kind: string; key?: string }) => c.key ?? c.kind;

const MODEL = {
  cameras: [cam('head'), cam('left_wrist'), cam('right_wrist')],
  streams: [stream('left_arm'), stream('right_arm'), stream('grippers'), stream('force', false), stream('depth', false, 'depth', false)],
};

describe('vizLayout', () => {
  it('reads and writes grid keys', () => {
    expect(parseGridKey('3x2')).toEqual({ cols: 3, rows: 2 });
    expect(parseGridKey('4x4')).toEqual({ cols: 4, rows: 4 });
    expect(parseGridKey('5x1')).toBeNull();
    expect(gridKey({ cols: 2, rows: 2 })).toBe('2x2');
    expect(GRID_SIZES.map(gridKey)).toEqual(['1x1', '2x1', '2x2', '3x2', '3x3', '4x3', '4x4']);
  });

  it('lays out the smart template by the grid width', () => {
    const wide = templateLayout('smart', MODEL, 1200)!;
    expect(wide.shape).toEqual({ cols: 3, rows: 2 });
    expect(wide.cells).toEqual([
      { kind: 'video', key: 'head' },
      { kind: 'video', key: 'left_wrist' },
      { kind: 'video', key: 'right_wrist' },
      { kind: 'curve', key: 'left_arm' },
      { kind: 'curve', key: 'right_arm' },
      { kind: 'curve', key: 'grippers' },                       // the cell left over takes the third group
    ]);
    // the info panel squeezes the grid below 1000 px: two columns, three rows, the cameras still first
    const narrow = templateLayout('smart', MODEL, 900)!;
    expect(narrow.shape).toEqual({ cols: 2, rows: 3 });
    expect(narrow.cells.map(k)).toEqual(['head', 'left_wrist', 'right_wrist', 'left_arm', 'right_arm', 'grippers']);
    expect(smartColumns(999)).toBe(2);
  });

  it('fills its empty cells, cameras first, picking the groups by turns among the robots (UMI with two hands, 2026-10-12)', () => {
    const umiCam = (r: number) => ({ ...cam(`robot${r}_sensor_camera0_compressed`), name: `robot${r} camera0`, source: `/robot${r}/sensor/camera0/compressed` });
    const umiGroup = (r: number, what: string, topic: string) => ({ ...stream(`robot${r}_${what}`), name: `robot${r} ${what}`, sources: [`/robot${r}/${topic}`] });
    const umi = {
      cameras: [umiCam(0), umiCam(1)],
      streams: [umiGroup(0, 'vio_eef_pose', 'vio/eef_pose'), umiGroup(0, 'sensor_magnetic_encoder', 'sensor/magnetic_encoder'), umiGroup(1, 'vio_eef_pose', 'vio/eef_pose'), umiGroup(1, 'sensor_magnetic_encoder', 'sensor/magnetic_encoder')],
    };
    // before: the two cameras, robot0's two groups and two empty cells
    const wide = templateLayout('smart', umi, 1200)!;
    expect(wide.shape).toEqual({ cols: 3, rows: 2 });
    expect(wide.cells.map(k)).toEqual([
      'robot0_sensor_camera0_compressed', 'robot1_sensor_camera0_compressed', 'robot0_vio_eef_pose',
      'robot0_sensor_magnetic_encoder', 'robot1_vio_eef_pose', 'robot1_sensor_magnetic_encoder',
    ]);
    // two columns hold two groups: each hand's first, not robot0's two
    const narrow = templateLayout('smart', umi, 900)!;
    expect(narrow.shape).toEqual({ cols: 2, rows: 2 });
    expect(narrow.cells.map(k)).toEqual(['robot0_sensor_camera0_compressed', 'robot1_sensor_camera0_compressed', 'robot0_vio_eef_pose', 'robot1_vio_eef_pose']);
    // a third camera: room for three groups, the third is robot0's second
    const top = { ...umi, cameras: [...umi.cameras, { ...cam('top'), source: '/camera/top' }] };
    expect(templateLayout('smart', top, 1200)!.cells.map(k)).toEqual([
      'robot0_sensor_camera0_compressed', 'robot1_sensor_camera0_compressed', 'top',
      'robot0_vio_eef_pose', 'robot0_sensor_magnetic_encoder', 'robot1_vio_eef_pose',
    ]);
  });

  it('tells the robot, arm or hand a name belongs to', () => {
    expect(ownerOf('/robot0/vio/eef_pose')).toBe('robot0');
    expect(ownerOf('robot1.observation.state')).toBe('robot1');
    expect(ownerOf('observation.images.cam_left_wrist')).toBe('left');
    expect(ownerOf('observation.state / action · kRightArm')).toBe('right');
    expect(ownerOf('left_cam', '/robot1/sensor/camera0')).toBe('robot1');   // a numbered robot before a side
    expect(ownerOf('observation.state / action · gripper', 'camera0')).toBeNull();
  });

  it('lays out more than nine cells in four columns on a wider grid, three otherwise (design doc 19 §2.2)', () => {
    const cams = (n: number) => Array.from({ length: n }, (_, i) => cam(`cam${i}`));
    // RH20T: ten cameras and two arm groups
    const rh20t = { cameras: cams(10), streams: [stream('left_arm'), stream('right_arm')] };
    const wide = templateLayout('smart', rh20t, 1400)!;
    expect(wide.shape).toEqual({ cols: 4, rows: 3 });
    expect(wide.cells.filter((c) => c.kind === 'video')).toHaveLength(10);
    expect(wide.cells.slice(10)).toEqual([
      { kind: 'curve', key: 'left_arm' },
      { kind: 'curve', key: 'right_arm' },
    ]);
    expect(wide.overflow).toBe(0);
    const narrow = templateLayout('smart', rh20t, 900)!;
    expect(narrow.shape).toEqual({ cols: 3, rows: 4 });
    expect(narrow.overflow).toBe(0);
    expect(smartColumns(1239, 12)).toBe(3);
    expect(smartColumns(1240, 12)).toBe(4);
    // more than the grid holds: the curves stay, the last cameras wait in 「更换」
    const sixteen = templateLayout('smart', { cameras: cams(16), streams: [stream('arm'), stream('grip')] }, 1400)!;
    expect(sixteen.shape).toEqual({ cols: 4, rows: 4 });
    expect(sixteen.cells.filter((c) => c.kind === 'video').map((c) => (c.kind === 'video' ? c.key : ''))).toEqual(cams(14).map((c) => c.key));
    expect(sixteen.cells.filter((c) => c.kind === 'curve')).toHaveLength(2);
    expect(sixteen.overflow).toBe(2);
    // nine cells or fewer keep the phase-one grid: a narrow 2 × 3 holds four cameras and the two groups
    const seven = templateLayout('smart', { cameras: cams(5), streams: [stream('arm'), stream('grip')] }, 900)!;
    expect(seven.shape).toEqual({ cols: 2, rows: 3 });
    expect(seven.cells.filter((c) => c.kind === 'curve')).toHaveLength(2);
    expect(seven.overflow).toBe(1);
    // video only: three columns up to nine cameras, four (or three) beyond
    expect(templateLayout('video', { cameras: cams(9), streams: [] }, 1400)!.shape).toEqual({ cols: 3, rows: 3 });
    expect(templateLayout('video', { cameras: cams(12), streams: [] }, 1400)!.shape).toEqual({ cols: 4, rows: 3 });
    const twenty = templateLayout('video', { cameras: cams(20), streams: [] }, 1000)!;
    expect(twenty.shape).toEqual({ cols: 3, rows: 4 });
    expect(twenty.overflow).toBe(8);
  });

  it('tells three grid widths apart', () => {
    expect([widthClass(800), widthClass(999), widthClass(1000), widthClass(1239), widthClass(1240), widthClass(2000)]).toEqual([900, 900, 1000, 1000, 1240, 1240]);
  });

  it('shrinks the grid for few contents', () => {
    const one = templateLayout('smart', { cameras: [cam('head')], streams: [] }, 1400)!;
    expect(one.shape).toEqual({ cols: 1, rows: 1 });
    const none = templateLayout('smart', { cameras: [], streams: [] }, 1400)!;
    expect(none.cells).toEqual([{ kind: 'empty' }]);
  });

  it('offers video-only and curve-only templates; custom keeps the arrangement', () => {
    expect(templateLayout('video', MODEL, 1200)!.shape).toEqual({ cols: 3, rows: 1 });
    const curves = templateLayout('curve', MODEL, 1200)!;
    expect(curves.shape).toEqual({ cols: 2, rows: 2 });
    // every drawable series, smart or not; the depth stream is not drawable in this phase
    expect(curves.cells.map((c) => (c.kind === 'curve' ? c.key : c.kind))).toEqual(['left_arm', 'right_arm', 'grippers', 'force']);
    expect(templateLayout('custom', MODEL, 1200)).toBeNull();
  });

  it('pads, cuts and resizes cells', () => {
    expect(shapeFor(5, 3)).toEqual({ cols: 3, rows: 2 });
    expect(shapeFor(2, 3)).toEqual({ cols: 2, rows: 1 });
    expect(fitCells([{ kind: 'video', key: 'a' }], { cols: 2, rows: 1 })).toEqual([{ kind: 'video', key: 'a' }, { kind: 'empty' }]);
    const cells = [{ kind: 'empty' as const }, { kind: 'video' as const, key: 'a' }, { kind: 'curve' as const, key: 'b' }];
    expect(resizeCells(cells, { cols: 1, rows: 1 })).toEqual([{ kind: 'video', key: 'a' }]);
  });

  it('sizes cells at two thirds of their width within 160–420 px', () => {
    expect(cellHeight(1200, 3)).toBe(Math.round(((1200 - 20) / 3) * 0.66));
    expect(cellHeight(300, 3)).toBe(160);
    expect(cellHeight(2400, 1)).toBe(420);
  });

  it('drops cells the model no longer has', () => {
    expect(cellValid({ kind: 'video', key: 'head' }, MODEL)).toBe(true);
    expect(cellValid({ kind: 'curve', key: 'gone' }, MODEL)).toBe(false);
    expect(cellValid({ kind: 'empty' }, MODEL)).toBe(true);
  });

  it('lays out the mini player by the finding (design doc 18 §4.6)', () => {
    const arms = { cameras: MODEL.cameras, streams: [stream('left_arm'), stream('right_arm'), stream('grippers')] };
    // a picture finding: the camera in scope and another one
    expect(miniLayout('visual_quality', { camera: 'left_wrist' }, arms)).toEqual({
      cells: [{ kind: 'video', key: 'left_wrist' }, { kind: 'video', key: 'head' }],
      shape: { cols: 2, rows: 1 },
    });
    // a motion finding: the camera and the arm's curves
    expect(miniLayout('kinematic_limits', { camera: 'right_wrist', arm: 'right' }, arms).cells).toEqual([
      { kind: 'video', key: 'right_wrist' },
      { kind: 'curve', key: 'right_arm' },
    ]);
    // no arm named: the first smart group; no camera named: the first camera
    expect(miniLayout('motion_quality', null, arms).cells).toEqual([{ kind: 'video', key: 'head' }, { kind: 'curve', key: 'left_arm' }]);
    // anything else (or no finding): every camera, three at most
    expect(miniLayout('task_success', null, arms)).toEqual({ cells: MODEL.cameras.map((c) => ({ kind: 'video', key: c.key })), shape: { cols: 3, rows: 1 } });
    expect(miniLayout(null, null, { cameras: [], streams: [stream('arm')] }).cells).toEqual([{ kind: 'curve', key: 'arm' }]);
  });

  it('lays out a sync finding over every camera, the named ones first, the arm after when there is room (2026-10-13)', () => {
    const two = { cameras: [cam('front'), cam('wrist')], streams: [stream('arm')] };
    // one camera named (a suspect, a single misaligned camera): still both cameras - an offset shows only against the other
    expect(miniLayout('video_action_sync', { camera: 'wrist' }, two).cells).toEqual([
      { kind: 'video', key: 'wrist' }, { kind: 'video', key: 'front' }, { kind: 'curve', key: 'arm' }]);
    expect(miniLayout('video_action_sync', { cameras: ['front', 'wrist'] }, two).cells).toEqual([
      { kind: 'video', key: 'front' }, { kind: 'video', key: 'wrist' }, { kind: 'curve', key: 'arm' }]);
    expect(miniLayout('video_action_sync', null, two).cells).toEqual([
      { kind: 'video', key: 'front' }, { kind: 'video', key: 'wrist' }, { kind: 'curve', key: 'arm' }]);
    // three cameras fill the row; the named one leads and the curve has no room
    const arms = { cameras: MODEL.cameras, streams: [stream('left_arm')] };
    expect(miniLayout('video_action_sync', { camera: 'right_wrist' }, arms).cells).toEqual([
      { kind: 'video', key: 'right_wrist' }, { kind: 'video', key: 'head' }, { kind: 'video', key: 'left_wrist' }]);
  });

  it('lays out an EEF finding over every camera with marks, the one in scope first (design doc 22 §3.3)', () => {
    const arms = { cameras: MODEL.cameras, streams: [stream('left_arm')] };
    expect(miniLayout('eef_video_consistency', { camera: 'right_wrist' }, arms, ['head', 'right_wrist']).cells).toEqual([
      { kind: 'video', key: 'right_wrist' },
      { kind: 'video', key: 'head' },
    ]);
    // a camera the model does not have is not laid out; none with marks: the motion rule as before
    expect(miniLayout('eef_video_consistency', null, arms, ['gone', 'head']).cells).toEqual([{ kind: 'video', key: 'head' }]);
    expect(miniLayout('eef_video_consistency', { camera: 'head' }, arms).cells).toEqual([{ kind: 'video', key: 'head' }, { kind: 'curve', key: 'left_arm' }]);
    // other modules ignore the marks
    expect(miniLayout('kinematic_limits', { camera: 'head' }, arms, ['head', 'right_wrist']).cells).toEqual([{ kind: 'video', key: 'head' }, { kind: 'curve', key: 'left_arm' }]);
  });

  it('finds the camera a scope names by its short name (an mcap topic too) and the group of an arm', () => {
    const cams = [cam('robot0_sensor_camera0_compressed'), cam('wrist')];
    cams[0].source = '/robot0/sensor/camera0/compressed';
    expect(cameraOfScope({ camera: 'wrist' }, cams)).toBe('wrist');
    expect(cameraOfScope({ cameras: ['compressed'] }, cams)).toBe('robot0_sensor_camera0_compressed');
    expect(cameraOfScope({ camera: 'nope' }, cams)).toBeNull();
    expect(armStream({ arm: 'LEFT' }, [stream('a'), stream('left_arm')])).toBe('left_arm');
    expect(armStream(null, [stream('x', false), stream('y')])).toBe('y');
    expect(armStream(null, [stream('d', false, 'depth', false)])).toBeNull();
  });

  it('leaves the cameras a display configuration hides out of the templates (design doc 21 §6)', () => {
    const model = { cameras: [cam('front'), cam('wrist', true), cam('top')], streams: [stream('arm')] };
    const keys = (cells: { kind: string; key?: string }[] | undefined) => (cells ?? []).filter((c) => c.kind === 'video').map((c) => c.key);
    expect(keys(templateLayout('smart', model, 1200)?.cells)).toEqual(['front', 'top']);
    expect(keys(templateLayout('video', model, 1200)?.cells)).toEqual(['front', 'top']);
    // the mini player still shows the hidden camera a finding is about; its other pick is not hidden
    expect(keys(miniLayout('visual_quality', { camera: 'wrist' }, model).cells)).toEqual(['wrist', 'front']);
    expect(keys(miniLayout(null, null, model).cells)).toEqual(['front', 'top']);
  });
});

describe('the player chips (2026-10-13)', () => {
  const f = (module: string, code: string, item: string, message_zh: string, scope: { camera?: string; cameras?: string[] } | null, level = 'info') => ({ module, level, finding: { code, item, message_zh, scope } });
  it('merges the sync check\'s findings into one chip naming every camera, other modules keep theirs', () => {
    const findings = [
      f('visual_quality', 'blur', 'IMG-2', '画面模糊', { camera: 'front' }),
      f('video_action_sync', 'camera_misaligned', 'AV-1', 'wrist 比动作早 0.97 秒', { camera: 'wrist' }, 'review'),
      f('video_action_sync', 'lag_inconsistent', 'MV-3', 'front 整条测不准，两路错开量不同', { cameras: ['front', 'wrist'] }, 'info'),
    ];
    const chips = playerChips(findings);
    expect(chips.map((c) => c.indices)).toEqual([[0], [1, 2]]);
    expect(chips[1].finding.level).toBe('review');
    expect(chips[1].finding.finding.item).toBe('AV-1 / MV-3');
    expect(chips[1].finding.finding.message_zh).toBe('front 整条测不准，两路错开量不同');   // the out-of-step text alone
    expect(chips[1].finding.finding.scope).toEqual({ camera: undefined, cameras: ['wrist', 'front'] });
    expect(playerChips([])).toEqual([]);
  });
});
