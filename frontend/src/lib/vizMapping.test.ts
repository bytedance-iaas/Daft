import { describe, expect, it } from 'vitest';
import type { McapTopic, VizMapping } from '../api/types';
import {
  canBeCamera,
  canBeDepth,
  displayName,
  emptyMapping,
  exportName,
  pairCamera,
  partnerTopic,
  roleByName,
  setDepthPair,
  setFields,
  setName,
  setPair,
  setRole,
  setSegments,
  setSmart,
  setTask,
  setTimeline,
  setUse,
  stemOf,
  summarize,
  usageOf,
  validateMapping,
  warningsOf,
} from './vizMapping';

function topic(name: string, schema: string, fields: [string, number][] | null, image: McapTopic['image'] = null): McapTopic {
  return { topic: name, schema, schema_encoding: 'protobuf', message_encoding: 'protobuf', count: 100, rate_hz: 30, start_s: 0, end_s: 3, image, fields: fields?.map(([path, size]) => ({ path, size })) ?? null, use: 'unmapped', role: null, name: '', notes: [] };
}

// the topics of an ABC-130k episode (a custom protobuf schema, state / action pairs)
const ABC = [
  topic('/left-arm-state', 'RobotState', [
    ['position', 6],
    ['velocity', 6],
    ['torque', 6],
  ]),
  topic('/left-arm-action', 'RobotState', [['position', 6]]),
  topic('/top-left-camera', 'foxglove.CompressedVideo', [], { codec: 'h265', width: 1920, height: 1200 }),
  topic('/instruction', 'Instructions', []),
  topic('/top-left-camera-info', 'foxglove.CameraCalibration', [['K', 9]]),
];

const UMI: VizMapping = {
  schema_version: 'viz-mapping/1.0',
  name: 'UMI 手持夹爪',
  base: 'builtin:umi',
  timeline: { source: 'log_time', frame_reference: null },
  cameras: [{ topic: '/robot0/sensor/camera0/compressed', name: 'robot0 camera0', schema: 'foxglove.CompressedImage' }],
  series: [
    { topic: '/robot0/vio/eef_pose', name: 'robot0 末端位姿', fields: ['pose'], labels: ['x', 'y', 'z', 'qx', 'qy', 'qz', 'qw'], unit: null, role: 'action' },
    { topic: '/robot0/sensor/magnetic_encoder', name: 'robot0 夹爪开度', fields: ['value'], labels: ['robot0_gripper'], unit: null, role: 'action' },
  ],
  task: null,
  segments: null,
  ignore: ['/robot0/sensor/imu'],
};

describe('vizMapping (design doc 18 §6)', () => {
  it('names and roles topics as the Daemon drafts them', () => {
    expect(displayName('/robot0/sensor/camera0/compressed')).toBe('robot0 camera0');
    expect(displayName('/')).toBe('/');
    expect(roleByName('/left-arm-action')).toBe('action');
    expect(roleByName('/robot0/vio/eef_pose')).toBe('state');
    expect(roleByName('/imu')).toBe('other');
    expect(partnerTopic('/left-arm-state')).toBe('/left-arm-action');
    expect(partnerTopic('/arm_action')).toBe('/arm_state');
    expect(partnerTopic('/imu')).toBeNull();
  });

  it('reads what the mapping does with every topic', () => {
    const m = { ...UMI, task: { topic: '/task' }, segments: { attachment: 'labels.json' } };
    expect(usageOf(m, '/robot0/sensor/camera0/compressed')).toBe('camera');
    expect(usageOf(m, '/robot0/vio/eef_pose')).toBe('series');
    expect(usageOf(m, '/task')).toBe('task');
    expect(usageOf(m, '/robot0/sensor/imu')).toBe('ignore');
    expect(usageOf(m, '/robot0/system_info')).toBe('unmapped');
  });

  it('turns a topic into a curve with the drafted fields and pairs it with its partner', () => {
    let m = setUse(emptyMapping(), ABC[0], 'series');
    expect(m.series).toEqual([{ topic: '/left-arm-state', name: 'left-arm-state', schema: 'RobotState', role: 'state', fields: ['position'] }]);
    m = setUse(m, ABC[1], 'series');
    expect(m.series.map((s) => [s.topic, s.role, s.pair_with])).toEqual([
      ['/left-arm-state', 'state', '/left-arm-action'],
      ['/left-arm-action', 'action', '/left-arm-state'],
    ]);
    // the role picked by hand wins over the name, and the pair it would break is undone
    m = setRole(m, '/left-arm-action', 'state');
    expect(m.series.map((s) => [s.role, s.pair_with ?? null])).toEqual([
      ['state', null],
      ['state', null],
    ]);
    // a curve stays where it is when only its role changes
    expect(setUse(m, ABC[1], 'series', 'other').series.map((s) => s.topic)).toEqual(['/left-arm-state', '/left-arm-action']);
  });

  it('keeps a fast auxiliary curve out of the smart layout, as the Daemon drafts it', () => {
    const imu = { ...topic('/robot0/sensor/imu', 'foxglove.IMUMeasurement', [['linear_acceleration.x', 1]]), rate_hz: 199 };
    expect(setUse(emptyMapping(), imu, 'series', 'other').series[0]).toMatchObject({ role: 'other', smart: false });
    // an arm state is as fast and stays in it
    expect(setUse(emptyMapping(), { ...ABC[0], rate_hz: 262 }, 'series').series[0]).not.toHaveProperty('smart');
  });

  it('moves a topic between uses, keeping its name; the topic a new task replaces is ignored', () => {
    let m = setUse(emptyMapping(), ABC[2], 'camera');
    m = setName(m, '/top-left-camera', '左上相机');
    m = setTimeline(m, { frame_reference: '/top-left-camera' });
    m = setUse(m, ABC[2], 'ignore');
    expect(m.cameras).toEqual([]);
    expect(m.ignore).toEqual(['/top-left-camera']);
    expect(m.timeline?.frame_reference).toBeNull();                     // the frame reference went with it
    m = setUse(m, ABC[2], 'camera');
    expect(m.cameras[0]).toEqual({ topic: '/top-left-camera', name: 'top-left-camera', schema: 'foxglove.CompressedVideo' });
    m = setUse(m, ABC[3], 'task');
    expect(m.task).toEqual({ topic: '/instruction' });
    m = setTask(m, { metadata_key: 'task_name' });
    expect(m.task).toEqual({ metadata_key: 'task_name' });
    expect(usageOf(m, '/instruction')).toBe('ignore');
    m = setSegments(m, { attachment: 'annotation.json' });
    expect(m.segments).toEqual({ attachment: 'annotation.json' });
    m = setUse(m, ABC[4], 'segments');
    expect(m.segments).toEqual({ topic: '/top-left-camera-info', start_field: 'start', end_field: 'end', label_field: 'label' });
    expect(setUse(m, ABC[4], 'unmapped').segments).toBeNull();
  });

  it('changes the fields of a curve: labels go once they change, transforms stay on fields still drawn', () => {
    const m = { ...UMI, series: [{ ...UMI.series[0], fields: ['pose.position', 'pose.orientation'], transforms: { 'pose.orientation': 'quat_xyzw_to_rpy' as const } }] };
    const same = setFields(m, '/robot0/vio/eef_pose', ['pose.position', 'pose.orientation']);
    expect(same.series[0].labels).toHaveLength(7);
    const fewer = setFields(m, '/robot0/vio/eef_pose', ['pose.position']);
    expect(fewer.series[0]).not.toHaveProperty('labels');
    expect(fewer.series[0]).not.toHaveProperty('transforms');
    expect(setFields(m, '/robot0/vio/eef_pose', []).series[0]).not.toHaveProperty('fields');  // the whole message
    expect(setSmart(m, '/robot0/vio/eef_pose', false).series[0].smart).toBe(false);
    expect(setSmart(setSmart(m, '/robot0/vio/eef_pose', false), '/robot0/vio/eef_pose', true).series[0]).not.toHaveProperty('smart');
  });

  it('pairs curves by hand and undoes the pairs they leave', () => {
    let m: VizMapping = { ...emptyMapping(), series: ['/a-state', '/a-action', '/b-action'].map((t) => ({ topic: t, name: t, role: t.endsWith('state') ? ('state' as const) : ('action' as const) })) };
    m = setPair(m, '/a-state', '/a-action');
    m = setPair(m, '/a-state', '/b-action');
    expect(m.series.map((s) => s.pair_with ?? null)).toEqual(['/b-action', null, '/a-state']);
    m = setPair(m, '/a-state', null);
    expect(m.series.map((s) => s.pair_with ?? null)).toEqual([null, null, null]);
  });

  it('sums up the mapping and warns about what the visualizer or the checks would miss', () => {
    const probe = { topics: [...ABC, topic('/robot0/sensor/camera0/compressed', 'foxglove.CompressedImage', [])] };
    const s = summarize(UMI, probe);
    expect([s.cameras, s.series, s.action, s.task, s.segments, s.ignored]).toEqual([1, 2, 2, false, false, 1]);
    expect(s.missing).toEqual(['/robot0/vio/eef_pose', '/robot0/sensor/magnetic_encoder', '/robot0/sensor/imu']);
    expect(s.unmapped).toEqual(ABC.map((t) => t.topic));
    expect(warningsOf(s)).toEqual(['missing', 'unmapped']);
    const others = { ...emptyMapping(), series: [{ topic: '/imu', name: 'imu', role: 'other' as const }] };
    expect(warningsOf(summarize(others, { topics: [] }))).toEqual(['no_camera', 'no_anchor', 'missing']);
    expect(warningsOf(summarize(emptyMapping(), { topics: [] }))).toEqual(['no_camera', 'no_series']);
  });

  it('validates an imported mapping like the Daemon: the Schema, then the rules it cannot say', () => {
    expect(validateMapping(UMI)).toEqual([]);
    expect(validateMapping([])).toEqual([{ field: '<root>', problem: '映射应为一个 JSON 对象' }]);
    const bad = validateMapping({
      schema_version: 'viz-mapping/2.0',
      cameras: [{ topic: '/cam' }],
      series: [{ topic: '/x', name: 'x', role: 'both', fields: ['a..b'], transforms: { q: 'to_euler' } }],
      task: { metadata_key: 'task', topic: '/task' },
      extra: 1,
    });
    expect(bad.map((p) => p.field)).toEqual(['extra', 'schema_version', 'cameras.0.name', 'series.0.role', 'series.0.fields.0', 'series.0.transforms.q', 'task']);
    // the cross rules run once the shape is right
    const twice: VizMapping = {
      ...UMI,
      cameras: [...UMI.cameras, { topic: '/robot0/vio/eef_pose', name: 'x' }],
      series: [{ ...UMI.series[0], pair_with: '/robot0/sensor/magnetic_encoder' }, UMI.series[1]],
      ignore: ['/robot0/sensor/camera0/compressed'],
      timeline: { frame_reference: '/nope' },
    };
    expect(validateMapping(twice).map((p) => [p.field, p.problem])).toEqual([
      ['series.0', 'topic /robot0/vio/eef_pose 同时出现在 cameras.1 与 series.0'],
      ['series.0.pair_with', '成对的两组要一组状态、一组动作'],
      ['ignore.0', 'topic /robot0/sensor/camera0/compressed 已经映射，不能同时忽略'],
      ['timeline.frame_reference', '帧号基准 /nope 不是映射里的相机或曲线'],
    ]);
    // against the dataset's topics
    const have = new Set(['/robot0/sensor/camera0/compressed', '/robot0/vio/eef_pose']);
    expect(validateMapping(UMI, have)).toEqual([
      { field: 'ignore', problem: '数据集里没有 topic /robot0/sensor/imu' },
      { field: 'series.1', problem: '数据集里没有 topic /robot0/sensor/magnetic_encoder' },
    ]);
    expect(validateMapping({ ...UMI, timeline: { source: 'message_timestamp' } }).map((p) => p.field)).toEqual(['timeline.timestamp_field']);
  });

  it('takes a depth topic as a depth picture paired with the camera of its stem, as the Daemon drafts it (design doc 21 §5.4)', () => {
    expect(['/top-depth', '/top-camera', '/camera/depth/image_raw', '/camera/color/image_raw', '/wrist_cam/aligned_depth_to_color/image_raw'].map(stemOf)).toEqual(['top', 'top', '', '', 'wrist']);
    const cam = topic('/top-camera', 'foxglove.CompressedImage', [], { codec: 'jpeg', width: 640, height: 480 });
    const depth = topic('/top-depth', 'foxglove.CompressedImage', [], { codec: 'png16', width: 640, height: 480 });
    expect([canBeCamera(cam), canBeDepth(cam), canBeCamera(depth), canBeDepth(depth)]).toEqual([true, false, false, true]);
    // a topic only the mapping names: by its schema
    expect(canBeDepth({ topic: '/x', schema: 'sensor_msgs/msg/Image' })).toBe(true);
    let m: VizMapping = { ...setUse(emptyMapping(), cam, 'camera'), schema_version: 'viz-mapping/1.0' };
    m = setUse(m, depth, 'depth');
    expect(m.schema_version).toBe('viz-mapping/1.1');
    expect(m.depths).toEqual([{ topic: '/top-depth', name: 'top-depth', schema: 'foxglove.CompressedImage', pair_with: '/top-camera' }]);
    expect(usageOf(m, '/top-depth')).toBe('depth');
    expect(summarize(m, { topics: [cam, depth] })).toMatchObject({ cameras: 1, depths: 1, unmapped: [] });
    // two cameras of one stem: no guess
    expect(pairCamera(setUse(m, topic('/top/camera/compressed', 'foxglove.CompressedImage', []), 'camera'), '/top-depth')).toBeNull();
    // by hand: drawn over no camera, renamed
    m = setName(setDepthPair(m, '/top-depth', null), '/top-depth', '顶部深度');
    expect(m.depths?.[0]).toMatchObject({ name: '顶部深度', pair_with: null });
    // its camera ignored: the depth no longer names it
    m = setUse(setDepthPair(m, '/top-depth', '/top-camera'), cam, 'ignore');
    expect(m.depths?.[0].pair_with).toBeNull();
    // made a camera, it keeps its name
    m = setUse(m, depth, 'camera');
    expect(m.depths).toEqual([]);
    expect(m.cameras).toEqual([{ topic: '/top-depth', name: '顶部深度', schema: 'foxglove.CompressedImage' }]);
  });

  it('validates depth topics like the Daemon: a mapped camera to draw over, mm or m, 1.0 and 1.1', () => {
    const cam = UMI.cameras[0].topic;
    const ok: VizMapping = { ...UMI, schema_version: 'viz-mapping/1.1', depths: [{ topic: '/robot0/sensor/depth0', name: 'robot0 depth0', pair_with: cam, unit: 'mm' }] };
    expect(validateMapping(ok)).toEqual([]);
    expect(validateMapping({ ...ok, depths: [{ ...ok.depths![0], unit: 'cm' }] })).toEqual([{ field: 'depths.0.unit', problem: '只能是 mm 或 m' }]);
    expect(validateMapping({ ...ok, schema_version: 'viz-mapping/2.0' })).toEqual([{ field: 'schema_version', problem: '应为 viz-mapping/1.0 或 viz-mapping/1.1' }]);
    const wrong: VizMapping = { ...ok, depths: [{ topic: '/robot0/sensor/depth0', name: 'd', pair_with: '/robot0/vio/eef_pose' }, { topic: cam, name: 'x' }] };
    expect(validateMapping(wrong).map((p) => [p.field, p.problem])).toEqual([
      ['depths.0.pair_with', '/robot0/vio/eef_pose 不是映射里的相机'],
      ['depths.1', `topic ${cam} 同时出现在 cameras.0 与 depths.1`],
    ]);
    expect(validateMapping(ok, new Set([cam, '/robot0/vio/eef_pose', '/robot0/sensor/magnetic_encoder', '/robot0/sensor/imu']))).toEqual([
      { field: 'depths.0', problem: '数据集里没有 topic /robot0/sensor/depth0' },
    ]);
  });

  it('names an exported file after the mapping', () => {
    expect(exportName(UMI)).toBe('UMI_手持夹爪.json');
    expect(exportName(emptyMapping())).toBe('viz-mapping.json');
  });
});
