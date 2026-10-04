// The data visualizer's mock world (design doc 18, C4 2.4.0): the presentation model of every
// seeded dataset and task input, synthetic curves, annotation tracks and a JPEG frame pack.
// Pure generators - the handlers in viz.ts serve them and keep the mutable state in db.ts.
import type {
  DatasetDetail,
  DatasetMappingInfo,
  McapProbe,
  McapTopic,
  VizAnnotations,
  VizAnnotationSource,
  VizCamera,
  VizDataset,
  VizEpisode,
  VizEpisodeCamera,
  VizEpisodeItem,
  VizFieldNode,
  VizFrameIndex,
  VizMapping,
  VizSeries,
  VizStatus,
  VizStream,
  VizTemplate,
} from '../api/types';
import { DATASET_PROFILES, type DatasetProfile } from './world';

const DAY = 24 * 3600 * 1000;

// ------------------------------------------------------------------ small deterministic helpers

function hash(text: string): number {
  let h = 2166136261;
  for (let i = 0; i < text.length; i += 1) h = Math.imul(h ^ text.charCodeAt(i), 16777619);
  return h >>> 0;
}

function round(v: number, digits = 4): number {
  const f = 10 ** digits;
  return Math.round(v * f) / f;
}

// ------------------------------------------------------------------ mappings (mcap, C7)

/** The mapping warehouse_mcap was registered with: two cameras (JPEG, H.264) and the arm. */
export const WAREHOUSE_MAPPING: VizMapping = {
  schema_version: 'viz-mapping/1.0',
  name: 'warehouse（Foxglove 通用）',
  base: 'builtin:foxglove',
  timeline: { source: 'log_time', frame_reference: null },
  cameras: [
    { topic: '/observation.images.front', name: 'front', schema: 'foxglove.CompressedImage' },
    { topic: '/observation.images.wrist', name: 'wrist', schema: 'foxglove.CompressedVideo' },
  ],
  series: [
    { topic: '/observation.state', name: '关节', fields: ['position'], labels: JOINTS(), unit: 'rad', role: 'state', pair_with: '/action' },
    { topic: '/action', name: '关节', fields: ['position'], labels: JOINTS(), unit: 'rad', role: 'action', pair_with: '/observation.state' },
    { topic: '/imu', name: 'IMU', fields: ['linear_acceleration'], unit: 'm/s²', role: 'other', smart: false },
  ],
  task: { metadata_key: 'task' },
  segments: null,
  ignore: ['/tf', '/camera_info'],
};

function JOINTS(): string[] {
  return ['joint_0', 'joint_1', 'joint_2', 'joint_3', 'joint_4', 'joint_5', 'joint_6', 'gripper'];
}

export function seedVizMappings(now: number): Map<string, { mapping: VizMapping; version: number; updatedAt: number }> {
  return new Map([['ds_mcap', { mapping: WAREHOUSE_MAPPING, version: 1, updatedAt: now - 4 * DAY }]]);
}

const READERS: Record<string, 'lerobot' | 'mcap' | 'lance' | null> = { lerobot_v2: 'lerobot', lerobot_v3: 'lerobot', mcap: 'mcap', lance: 'lance' };

/**
 * The format as the visualizer sees it (the Daemon's ``viz_format``): episode_N.mcap files make an
 * mcap dataset even when the checks cannot read it with the site's default topics (ABC-130k), and a
 * LeRobot layout stays LeRobot when the check reader refuses it (Galaxea: no action column, F13.8).
 */
export function vizFormatOf(d: { format: DatasetDetail['format']; preflight?: { format?: { kind?: string; version?: string | null } } }): DatasetDetail['format'] {
  const f = d.preflight?.format;
  if (f?.kind === 'mcap') return 'mcap';
  if (f?.kind === 'lerobot' && (f.version === 'v2' || f.version === 'v3')) return f.version === 'v3' ? 'lerobot_v3' : 'lerobot_v2';
  return d.format;
}

/** C4 ``VizStatus`` of a registration (``DatasetItem.viz``). */
export function vizStatusOf(format: DatasetDetail['format'], mapped: boolean): VizStatus {
  if (!READERS[format]) {
    return { state: 'unsupported', reason: '这个格式没有可视化读取器' };
  }
  if (format === 'mcap' && !mapped) return { state: 'mapping_pending', reason: 'mcap 数据集要先确认字段映射（mcap 配置）' };
  return { state: 'ready', reason: null };
}

export function mappingInfoOf(format: DatasetDetail['format'], stored?: { mapping: VizMapping; version: number; updatedAt: number }): DatasetMappingInfo | null {
  if (format !== 'mcap') return null;
  if (!stored) return { state: 'none', version: 0, updated_at: null, name: null };
  return { state: 'confirmed', version: stored.version, updated_at: stored.updatedAt, name: stored.mapping.name ?? null };
}

// ------------------------------------------------------------------ the presentation model

interface Shape {
  cameras: VizCamera[];
  streams: VizStream[];
  sources: VizAnnotationSource[];
  fps: number | null;
}

function camera(key: string, p: DatasetProfile, over: Partial<VizCamera> = {}): VizCamera {
  const big = p.name.startsWith('umi');
  return {
    key,
    name: key,
    source: `observation.images.${key}`,
    kind: 'video',
    // a Lance table keeps its mp4s in a blob column: the Daemon serves them (design doc 19 §4.3)
    access: p.format.kind === 'lance' ? 'blob' : p.source === 'local' ? 'local' : 'direct',
    codec: 'av1',
    codec_string: 'av01.0.05M.08',
    width: big ? 1280 : 320,
    height: big ? 720 : 180,
    fps: p.fps,
    pix_fmt: 'yuv420p',
    transcoded: false,
    reason: null,
    ...over,
  };
}

function seriesGroup(key: string, name: string, dims: string[], over: Partial<VizStream> = {}): VizStream {
  return {
    key,
    kind: 'series',
    name,
    unit: null,
    lines: dims.flatMap((d, i) => [
      { name: d, role: 'state' as const, source: 'observation.state', dim: i, unit: null },
      { name: d, role: 'action' as const, source: 'action', dim: i, unit: null },
    ]),
    smart: true,
    available: true,
    reason: null,
    sources: ['observation.state', 'action'],
    rate_hz: null,
    ...over,
  };
}

function shapeOf(p: DatasetProfile, mapping: VizMapping | null): Shape {
  if (p.format.kind === 'mcap') {
    if (!mapping) return { cameras: [], streams: [], sources: [], fps: null };
    const cams: VizCamera[] = mapping.cameras.map((c) => {
      const jpeg = (c.schema ?? '').includes('Image');
      return {
        key: c.topic.replace(/^\/+/, '').replace(/[^0-9A-Za-z_-]/g, '_'),
        name: c.name,
        source: c.topic,
        kind: jpeg ? 'frames' : 'video',
        access: jpeg ? 'frames' : 'remux',
        codec: jpeg ? 'jpeg' : 'h264',
        codec_string: jpeg ? null : 'avc1.64001f',
        width: 640,
        height: 480,
        fps: 30,
        pix_fmt: null,
        transcoded: false,
        reason: null,
      };
    });
    // the dataset's own names (2026-10-04: nothing translated)
    const arm = seriesGroup('observation_state', 'observation.state / action', JOINTS().slice(0, 7), { unit: 'rad', sources: ['/observation.state', '/action'], rate_hz: 30 });
    const gripper = seriesGroup('observation_state.gripper', 'observation.state / action · gripper', ['gripper'], { sources: ['/observation.state', '/action'], rate_hz: 30 });
    const imu: VizStream = {
      key: 'imu', kind: 'series', name: 'IMU', unit: 'm/s²', smart: false, available: true, reason: null, sources: ['/imu'], rate_hz: 200,
      lines: ['x', 'y', 'z'].map((n, i) => ({ name: n, role: 'other' as const, source: '/imu', dim: i, unit: 'm/s²' })),
    };
    return { cameras: cams, streams: [arm, gripper, imu], sources: [], fps: null };
  }
  const cams = p.cameras.map((c) =>
    p.name === 'droid-200' && c === 'wrist'
      ? camera(c, p, { codec: 'mpeg4', codec_string: 'mp4v.20.9', access: 'transcode', transcoded: true, reason: '原始编码 mpeg4（MPEG-4 Part 2），浏览器不能直接播放，由平台转为 H.264' })
      : camera(c, p),
  );
  const streams: VizStream[] = [];
  const sources: VizAnnotationSource[] = [];
  if (p.name.startsWith('umi')) {
    const pose = ['x', 'y', 'z', 'roll', 'pitch', 'yaw'];
    streams.push(seriesGroup('left', 'observation.state / action · left', pose.map((n) => `left_${n}`)));
    streams.push(seriesGroup('right', 'observation.state / action · right', pose.map((n) => `right_${n}`)));
    streams.push(seriesGroup('gripper', 'observation.state / action · gripper', ['left_gripper', 'right_gripper']));
  } else if (p.name === 'pusht') {
    streams.push(seriesGroup('observation_state', 'observation.state / action', ['x', 'y']));
  } else {
    streams.push(seriesGroup('observation_state', 'observation.state / action', JOINTS().slice(0, 7), { unit: 'rad' }));
    streams.push(seriesGroup('observation_state.gripper', 'observation.state / action · gripper', ['gripper']));
  }
  if (p.name === 'droid_100') {
    streams.push({
      key: 'observation_eef_pose', kind: 'series', name: 'observation.eef_pose', unit: null, smart: false, available: true, reason: null,
      sources: ['observation.eef_pose'], rate_hz: null,
      lines: ['x', 'y', 'z', 'roll', 'pitch', 'yaw'].map((n, i) => ({ name: n, role: 'other' as const, source: 'observation.eef_pose', dim: i, unit: null })),
    });
    streams.push({
      key: 'observation_images_wrist_depth', kind: 'depth', name: 'wrist 深度', unit: 'mm', lines: [], smart: false, available: false,
      reason: '深度图在第二期渲染（设计 18 §10）', sources: ['observation.images.wrist.depth'], rate_hz: null,
    });
    sources.push({ key: 'subtask_index', kind: 'segments', name: '子任务', format: 'subtask_index', source: 'subtask_index + meta/subtasks.parquet', supported: true, reason: null, primary: true });
  }
  if (p.name === 'droid-200') {
    sources.push({ key: 'subtask', kind: 'segments', name: 'subtask', format: 'string_column', source: 'subtask', supported: false, reason: '标注格式不支持：subtask 列全是占位文字 TODO', primary: false });
  }
  if (p.name === 'libero_10') {
    sources.push({ key: 'next.success', kind: 'labels', name: '成败', format: 'quality', source: 'next.success', supported: true, reason: null, primary: false });
  }
  return { cameras: cams, streams, sources, fps: p.fps };
}

function fieldTree(p: DatasetProfile, shape: Shape): VizFieldNode[] {
  if (p.format.kind === 'mcap') {
    return [
      { id: 'topics', name: 'Topic', kind: 'group', children: [
        ...shape.cameras.map((c) => ({ id: `topic:${c.source}`, name: c.source, kind: 'topic' as const, camera: c.key, detail: { schema: c.access === 'frames' ? 'foxglove.CompressedImage' : 'foxglove.CompressedVideo', 编码: c.codec, 尺寸: `${c.width}×${c.height}` } })),
        ...shape.streams.map((s) => ({ id: `topic:${s.sources[0]}`, name: s.sources[0] ?? s.key, kind: 'topic' as const, stream: s.key, detail: { 频率: s.rate_hz ? `${s.rate_hz} Hz` : null } })),
      ] },
      { id: 'metadata', name: 'Metadata', kind: 'group', children: [{ id: 'metadata:episode', name: 'episode', kind: 'metadata', detail: { task: 'pick the box' } }] },
    ];
  }
  return [
    { id: 'cameras', name: '相机', kind: 'group', children: shape.cameras.map((c) => ({ id: `camera:${c.key}`, name: c.key, kind: 'camera' as const, camera: c.key, dtype: 'video', shape: [c.height ?? 0, c.width ?? 0, 3], detail: { 分辨率: `${c.width}×${c.height}`, 编码: c.codec, 帧率: c.fps, 读取方式: c.access } })) },
    { id: 'state_action', name: '状态与动作', kind: 'group', children: shape.streams.map((s) => ({ id: `stream:${s.key}`, name: s.name, kind: s.kind === 'depth' ? ('depth' as const) : ('series' as const), stream: s.key, dtype: s.kind === 'depth' ? 'uint16' : 'float32', shape: [Math.max(1, s.lines.filter((l) => l.role !== 'action').length)], names: s.lines.filter((l) => l.role !== 'action').map((l) => l.name), detail: { 来源: s.sources.join('、') } })) },
    { id: 'annotations', name: '任务与标注', kind: 'group', children: [
      { id: 'tasks', name: p.format.version === 'v3' ? 'meta/tasks.parquet' : 'meta/tasks.jsonl', kind: 'table', file: p.format.version === 'v3' ? undefined : 'meta/tasks.jsonl', detail: { 有任务描述: p.withTask, 无任务描述: p.episodes - p.withTask } },
      ...shape.sources.map((a) => ({ id: `annotation:${a.key}`, name: a.name, kind: 'table' as const, detail: { 来源: a.source, 支持: a.supported, 原因: a.reason } })),
    ] },
    { id: 'meta', name: '元数据', kind: 'group', children: [
      { id: 'meta:info', name: 'info.json', kind: 'file', file: 'meta/info.json', detail: { fps: p.fps, robot_type: p.robotType } },
      { id: 'meta:stats', name: p.format.version === 'v3' ? 'stats.json' : 'episodes_stats.jsonl', kind: 'file', file: p.format.version === 'v3' ? 'meta/stats.json' : 'meta/episodes_stats.jsonl' },
    ] },
  ];
}

export function vizDataset(scope: 'dataset' | 'task', id: string, d: DatasetDetail, mapping: VizMapping | null, mappingVersion: number | null): VizDataset {
  const p = DATASET_PROFILES.find((x) => x.uri === d.uri) ?? DATASET_PROFILES[1];
  const fmt = vizFormatOf(d);
  const reader = READERS[fmt] ?? null;
  const shape = reader ? shapeOf(p, mapping) : { cameras: [], streams: [], sources: [], fps: p.fps };
  const warnings = [];
  if (fmt === 'mcap' && !mapping) warnings.push({ code: 'mapping_pending', message: 'mcap 数据集还没有确认字段映射：到「mcap 配置」确认后才能看相机与曲线' });
  if (!reader) warnings.push({ code: 'unsupported', message: vizStatusOf(fmt, false).reason ?? '' });
  for (const s of shape.sources.filter((x) => !x.supported)) warnings.push({ code: 'annotation_unsupported', message: s.reason ?? '标注格式不支持' });
  return {
    scope,
    id,
    dataset_id: scope === 'dataset' ? id : d.id,
    name: d.name,
    format: { kind: p.format.kind, version: p.format.version, reader, layout: reader === 'lance' ? 'lance-0.3' : null },
    fps: shape.fps,
    episode_count: p.episodes,
    episode_indices: null,
    total_frames: reader === 'lerobot' ? episodeFrameSum(p) : null,
    robot_type: p.robotType,
    bytes: d.listing.bytes,
    cameras: shape.cameras,
    streams: shape.streams,
    annotation_sources: shape.sources,
    field_tree: reader ? fieldTree(p, shape) : [],
    mapping: fmt === 'mcap'
      ? { state: scope === 'task' && mapping ? 'frozen' : mapping ? 'confirmed' : 'none', version: mappingVersion, name: mapping?.name ?? null }
      : { state: 'not_needed', version: null, name: null },
    transcode: { enabled: true },
    warnings,
    fingerprint: d.meta_fingerprint,
  };
}

function episodeFrameSum(p: DatasetProfile): number {
  let n = 0;
  for (let i = 0; i < p.episodes; i += 1) n += framesOf(p, i);
  return n;
}

/** Frames of an episode: 180-379, the same for every reader of the mock world. */
export function framesOf(p: DatasetProfile, index: number): number {
  return 180 + ((index * 37 + hash(p.name)) % 200);
}

function rateOf(p: DatasetProfile): number {
  return p.fps ?? 30;
}

const STEP_NAMES = ['靠近目标', '抓取', '抬起', '移动', '放下'];

/** The subtask track of an episode (droid_100 only): 3-5 steps covering the episode. */
export function stepsOf(p: DatasetProfile, index: number): { start_s: number; end_s: number; label: string; quality: 'qualified' | 'unqualified' }[] {
  if (p.name !== 'droid_100') return [];
  const duration = framesOf(p, index) / rateOf(p);
  const n = 3 + (index % 3);
  const out = [];
  let t = round(0.4 + (index % 4) * 0.1, 2);
  for (let k = 0; k < n; k += 1) {
    const end = k === n - 1 ? round(duration - 0.3, 2) : round(t + (duration - 0.7) / n, 2);
    out.push({ start_s: t, end_s: end, label: STEP_NAMES[k % STEP_NAMES.length], quality: (index + k) % 7 === 3 ? ('unqualified' as const) : ('qualified' as const) });
    t = round(end + 0.05, 2);
  }
  return out;
}

export function taskOf(p: DatasetProfile, index: number): string {
  if (index % p.episodes >= p.withTask) return '';
  const tasks = ['Put the marker in the cup', 'Pick up the red block and place it in the bowl', 'Open the drawer', 'Fold the towel'];
  return tasks[(index + hash(p.name)) % tasks.length];
}

export function episodeItems(d: DatasetDetail): VizEpisodeItem[] {
  const p = DATASET_PROFILES.find((x) => x.uri === d.uri) ?? DATASET_PROFILES[1];
  const out: VizEpisodeItem[] = [];
  for (let i = 0; i < p.episodes; i += 1) {
    const frames = framesOf(p, i);
    const steps = stepsOf(p, i);
    out.push({
      index: i,
      duration_s: round(frames / rateOf(p), 3),
      frames,
      task: taskOf(p, i),
      steps: steps.length ? steps.map((s) => ({ start_s: s.start_s, end_s: s.end_s, unqualified: s.quality === 'unqualified' })) : null,
    });
  }
  return out;
}

export interface EpisodeUrls {
  /** a presigned TOS URL for a key relative to the dataset */
  direct: (key: string) => string;
  /** the Daemon's camera route (``.mp4`` / ``.frames`` / ``.json``) */
  daemon: (camera: string, suffix: 'mp4' | 'frames' | 'json', transcode?: boolean) => string;
}

export function vizEpisode(scope: 'dataset' | 'task', id: string, model: VizDataset, index: number, d: DatasetDetail, urls: EpisodeUrls, now: number): VizEpisode {
  const p = DATASET_PROFILES.find((x) => x.uri === d.uri) ?? DATASET_PROFILES[1];
  const frames = framesOf(p, index);
  const rate = rateOf(p);
  const duration = round(frames / rate, 3);
  const v3 = p.format.version === 'v3';
  const cameras: VizEpisodeCamera[] = model.cameras.map((c) => {
    const base = { key: c.key, kind: c.kind, access: c.access, transcoded: c.transcoded, reason: c.reason, offset_s: 0, samples_url: null };
    if (c.access === 'frames') {
      return { ...base, url: urls.daemon(c.key, 'frames'), index_url: urls.daemon(c.key, 'json'), transcode_url: null, from_ts: null, to_ts: null, expires_at: null };
    }
    if (c.access === 'remux') {
      // an mcap H.264 camera: the browser may decode its sample pack itself (design doc 19 §3)
      return { ...base, url: urls.daemon(c.key, 'mp4'), index_url: urls.daemon(c.key, 'json'), samples_url: urls.daemon(c.key, 'frames'), transcode_url: urls.daemon(c.key, 'mp4', true), from_ts: null, to_ts: null, expires_at: null };
    }
    if (c.access === 'blob') {
      const from = v3 ? round(index * 20.0, 3) : null;
      return { ...base, url: urls.daemon(c.key, 'mp4'), index_url: null, transcode_url: urls.daemon(c.key, 'mp4', true), from_ts: from, to_ts: from === null ? null : round(from + duration, 3), expires_at: null };
    }
    if (c.access === 'direct') {
      const file = v3 ? `videos/${c.source}/chunk-000/file-000.mp4` : `videos/chunk-000/${c.source}/episode_${String(index).padStart(6, '0')}.mp4`;
      const from = v3 ? round(index * 20.0, 3) : null;
      return { ...base, url: urls.direct(file), index_url: null, transcode_url: urls.daemon(c.key, 'mp4', true), from_ts: from, to_ts: from === null ? null : round(from + duration, 3), expires_at: now + 1800 * 1000 };
    }
    return { ...base, url: urls.daemon(c.key, 'mp4', c.access === 'transcode'), index_url: null, transcode_url: null, from_ts: null, to_ts: null, expires_at: null };
  });
  const steps = stepsOf(p, index);
  const annotations: VizAnnotations = {
    tracks: steps.length ? [{ key: 'subtask_index', name: '子任务', source: 'subtask_index + meta/subtasks.parquet', primary: true, segments: steps }] : [],
    events: steps.length ? [{ t_s: round(steps[1].start_s + 0.2, 2), label: '夹爪闭合', outcome: null, source: 'language_events' }] : [],
    labels: p.name === 'libero_10' ? [{ key: 'next.success', name: '成败', value: index % 5 === 4 ? '失败' : '成功', source: 'next.success' }] : [],
    warnings: model.annotation_sources.filter((s) => !s.supported).map((s) => ({ code: 'annotation_unsupported', message: s.reason ?? '标注格式不支持' })),
  };
  const task = taskOf(p, index);
  const mcap = p.format.kind === 'mcap';
  return {
    scope,
    id,
    index,
    duration_s: duration,
    frames,
    fps: mcap ? null : rate,
    task: task ? { text: task, source: '原始标注' } : null,
    timeline: mcap
      ? { kind: 'timestamp', fps: null, frame_reference: '/action', frame_times: Array.from({ length: frames }, (_, k) => round(k / rate + 0.012, 4)) }
      : { kind: 'frame', fps: rate, frame_reference: null, frame_times: null },
    cameras,
    annotations,
    check_clock: scope === 'task' ? { offset_s: mcap ? 0.012 : 0, fps: rate } : null,
    warnings: [],
  };
}

/** One curve group of an episode: smooth sine-like joints, the action leading the state by 0.15 s. */
export function vizSeries(model: VizDataset, d: DatasetDetail, index: number, key: string, from: number | null, to: number | null, points: number): VizSeries | null {
  const s = model.streams.find((x) => x.key === key);
  if (!s || s.kind !== 'series') return null;
  const p = DATASET_PROFILES.find((x) => x.uri === d.uri) ?? DATASET_PROFILES[1];
  const rate = s.rate_hz ?? rateOf(p);
  const duration = framesOf(p, index) / rateOf(p);
  const a = Math.max(0, from ?? 0);
  const b = Math.min(duration, to ?? duration);
  const total = Math.max(0, Math.floor((b - a) * rate) + 1);
  const n = Math.min(total, points);
  const step = n > 1 ? (b - a) / (n - 1) : 0;
  const t = Array.from({ length: n }, (_, k) => round(a + k * step, 4));
  const lines = s.lines.map((l, i) => {
    const seed = hash(`${d.id}:${index}:${l.name}`);
    const amp = 0.2 + (seed % 100) / 120;
    const freq = 0.08 + ((seed >> 8) % 50) / 400;
    const phase = ((seed >> 16) % 628) / 100;
    const lag = l.role === 'action' ? 0.15 : 0;
    const gripper = l.name.includes('gripper');
    return {
      name: l.name,
      role: l.role,
      values: t.map((x) => {
        const u = x + lag;
        if (gripper) return round(u % 6 < 3 ? 0.08 : 0.01, 4);
        return round(amp * Math.sin(2 * Math.PI * freq * u + phase) + 0.1 * i, 4);
      }),
    };
  });
  return { stream: key, unit: s.unit, from_s: round(a, 4), to_s: round(b, 4), t, lines, total_points: total, downsampled: total > n };
}

// ------------------------------------------------------------------ the JPEG frame pack

/** A 16×12 grey JPEG; every frame of the mock pack is this picture. */
const TINY_JPEG_B64 =
  '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERXRTc4UG1RV19iZ2hnPk1xeXBkeFxlZ2P/2wBDARESEhgVGC8aGi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2P/wAARCAAMABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwClRRRXKdJ//9k=';

let tinyJpeg: Uint8Array | null = null;

function jpegBytes(): Uint8Array {
  if (!tinyJpeg) tinyJpeg = Uint8Array.from(atob(TINY_JPEG_B64), (ch) => ch.charCodeAt(0));
  return tinyJpeg;
}

export function frameIndex(camera: string, frames: number, rate: number): VizFrameIndex {
  const size = jpegBytes().length;
  return {
    camera,
    codec: 'jpeg',
    width: 16,
    height: 12,
    count: frames,
    t: Array.from({ length: frames }, (_, k) => round(k / rate, 4)),
    offset: Array.from({ length: frames }, (_, k) => k * size),
    size: Array.from({ length: frames }, () => size),
    bytes: frames * size,
  };
}

/** The index of an mcap H.264 camera's sample pack: the frame pack's, with a keyframe every second. */
export function sampleIndex(camera: string, frames: number, rate: number): VizFrameIndex {
  const base = frameIndex(camera, frames, rate);
  const every = Math.max(1, Math.round(rate));
  return { ...base, codec: 'h264', width: 640, height: 480, key: base.t.map((_, k) => k % every === 0), codec_string: 'avc1.64001f' };
}

export function framePack(frames: number): Uint8Array {
  const one = jpegBytes();
  const out = new Uint8Array(one.length * frames);
  for (let k = 0; k < frames; k += 1) out.set(one, k * one.length);
  return out;
}

/** Bytes standing in for an mp4 (the mock world has no real video; the page shows the error state). */
export function fakeVideo(): Uint8Array {
  const out = new Uint8Array(4096);
  out.set([0, 0, 0, 0x18, 0x66, 0x74, 0x79, 0x70, 0x69, 0x73, 0x6f, 0x6d]);
  return out;
}

// ------------------------------------------------------------------ probing and templates (§6)

export const BUILTIN_TEMPLATES: VizTemplate[] = [
  { id: 'builtin:umi', name: 'UMI 手持夹爪（内置）', description: '/robotN/sensor/cameraN/compressed、/robotN/vio/eef_pose、/robotN/sensor/magnetic_encoder', builtin: true, mapping: null, created_at: null, updated_at: null },
  { id: 'builtin:foxglove', name: 'Foxglove 通用（内置）', description: '按 schema 归类：CompressedImage / CompressedVideo → 相机，PoseInFrame、JointState 等 → 曲线', builtin: true, mapping: null, created_at: null, updated_at: null },
  { id: 'builtin:ros2', name: 'ROS 2 通用（内置）', description: 'sensor_msgs/Image、CompressedImage → 相机，JointState、PoseStamped 等 → 曲线', builtin: true, mapping: null, created_at: null, updated_at: null },
];

const UMI_TOPICS: [string, string, string, number, number][] = [
  ['/robot0/sensor/camera0/compressed', 'foxglove.CompressedImage', 'camera', 1541, 29.9],
  ['/robot0/vio/eef_pose', 'foxglove.PoseInFrame', 'series', 1535, 29.8],
  ['/robot0/sensor/magnetic_encoder', 'foxglove.MagneticEncoderMeasurement', 'series', 2570, 49.9],
  ['/robot0/sensor/imu', 'foxglove.IMUMeasurement', 'ignore', 10212, 198],
  ['/robot0/sensor/camera0/camera_info', 'foxglove.CameraCalibration', 'ignore', 1, 0],
  ['/robot0/system_info', 'foxglove.SystemInfo', 'ignore', 43, 0.8],
];

type AbcTopic = [string, string, [string, number][], number | null, number, boolean];

/** An ABC-130k episode (real probe of abc130k_arrange_flowers_zedx, trimmed to the left side). */
const ABC_TOPICS: AbcTopic[] = [
  ['/instruction', 'Instructions', [], null, 1, false],
  ['/left-arm-action', 'RobotState', [['position', 6]], 199.96, 1990, false],
  ['/left-arm-state', 'RobotState', [['position', 6], ['velocity', 6], ['torque', 6]], 262.02, 2601, false],
  ['/left-ee-action', 'GripperState', [['position', 1]], 199.96, 1990, false],
  ['/left-ee-state', 'GripperState', [['position', 1], ['velocity', 1], ['torque', 1]], 262.02, 2601, false],
  ['/left-wrist-camera', 'foxglove.CompressedVideo', [], 30, 301, true],
  ['/left-wrist-camera-info', 'foxglove.CameraCalibration', [['width', 1], ['height', 1], ['D', 5], ['K', 9], ['P', 12]], null, 1, false],
  ['/top-left-camera', 'foxglove.CompressedVideo', [], 30, 301, true],
  ['/top-left-camera-info', 'foxglove.CameraCalibration', [['width', 1], ['height', 1], ['D', 5], ['K', 9], ['P', 12]], null, 1, false],
];

/** The probe of an ABC-130k-like dataset: no template fits, builtin:foxglove drafts it by schema. */
function abcProbe(file: string, files: number, template: string | null): McapProbe {
  const topics: McapTopic[] = ABC_TOPICS.map(([topic, schema, fields, rate, count, video]) => ({
    topic,
    schema,
    schema_encoding: 'protobuf',
    message_encoding: 'protobuf',
    count,
    rate_hz: rate,
    start_s: 0,
    end_s: 9.95,
    image: video ? { codec: 'h265', width: 1920, height: 1200 } : null,
    fields: fields.map(([path, size]) => ({ path, size })),
    use: video ? 'camera' : schema.endsWith('Calibration') ? 'ignore' : topic === '/instruction' ? 'task' : 'series',
    role: video || schema.endsWith('Calibration') || topic === '/instruction' ? null : topic.endsWith('state') ? 'state' : 'action',
    name: video ? topic.slice(1) : topic === '/instruction' ? '任务描述' : schema.endsWith('Calibration') ? '' : topic.slice(1),
    notes: [...(rate && rate > 150 ? ['高频：作曲线时下采样到 ≤ 2000 点'] : []), ...(video ? ['H.265：浏览器放不了时由平台转码'] : [])],
  }));
  const series = topics.filter((t) => t.use === 'series');
  const draft: VizMapping = {
    schema_version: 'viz-mapping/1.0',
    name: 'Foxglove 通用',
    base: 'builtin:foxglove',
    timeline: { source: 'log_time', frame_reference: null },
    cameras: topics.filter((t) => t.use === 'camera').map((t) => ({ topic: t.topic, name: t.name, schema: t.schema ?? undefined })),
    series: series.map((t) => ({ topic: t.topic, name: t.name, schema: t.schema ?? undefined, role: t.role ?? 'other', fields: ['position'], pair_with: t.topic.replace(/(state|action)$/, (w) => (w === 'state' ? 'action' : 'state')) })),
    task: { topic: '/instruction' },
    segments: null,
    ignore: topics.filter((t) => t.use === 'ignore').map((t) => t.topic),
  };
  return {
    file,
    files,
    topics,
    metadata: { 'episode-metadata': { task_name: 'arrange the flowers into the vase', top_camera_type: 'ZED_X' } },
    attachments: [],
    draft,
    matched: template ? { template_id: 'builtin:foxglove', name: 'Foxglove 通用（内置）', coverage: 1 } : null,
    warnings: [
      { code: 'checks_gap', message: '质检读取器读不出 /left-arm-action 的 position、/left-arm-state 的 position 等的数值（protobuf 的 repeated 字段、ROS 2 的嵌套消息）：质检用不了这份映射，可视化不受影响' },
      { code: 'checks_gap', message: '质检读取器只读 JPEG 与 H.264 相机，读不了 H.265（/left-wrist-camera、/top-left-camera）：质检用不了这份映射，可视化不受影响' },
    ],
  };
}

/** A site template applied to a probe (the Daemon's ``from_template``): entries the file lacks go. */
export function fromTemplate(t: VizTemplate, probe: McapProbe): McapProbe {
  const have = new Set(probe.topics.map((x) => x.topic));
  const m = t.mapping!;
  const series = m.series.filter((s) => have.has(s.topic));
  const kept = new Set(series.map((s) => s.topic));
  const named = [...m.cameras.map((c) => c.topic), ...m.series.map((s) => s.topic)];
  const draft: VizMapping = {
    ...m,
    cameras: m.cameras.filter((c) => have.has(c.topic)),
    series: series.map((s) => (s.pair_with && !kept.has(s.pair_with) ? { ...s, pair_with: null } : s)),
    task: m.task && 'topic' in m.task && !have.has(m.task.topic) ? null : m.task ?? null,
    ignore: (m.ignore ?? []).filter((x) => have.has(x)),
  };
  const coverage = named.length ? named.filter((x) => have.has(x)).length / named.length : 0;
  return { ...probe, draft, matched: { template_id: t.id, name: t.name, coverage: round(coverage, 3) } };
}

/** warehouse_mcap's episode: the check reader's default topics, drafted by builtin:foxglove. */
function warehouseProbe(file: string, files: number, template: string | null): McapProbe {
  const rows: [string, string, [string, number][], number | null, McapTopic['image']][] = [
    ['/action', 'RobotJointState', [['position', 8]], 30, null],
    ['/camera_info', 'foxglove.CameraCalibration', [['K', 9], ['P', 12]], null, null],
    ['/imu', 'foxglove.IMUMeasurement', [['linear_acceleration.x', 1], ['linear_acceleration.y', 1], ['linear_acceleration.z', 1]], 200, null],
    ['/observation.images.front', 'foxglove.CompressedImage', [], 30, { codec: 'jpeg', width: 640, height: 480 }],
    ['/observation.images.wrist', 'foxglove.CompressedVideo', [], 30, { codec: 'h264', width: 640, height: 480 }],
    ['/observation.state', 'RobotJointState', [['position', 8], ['velocity', 8]], 30, null],
    ['/tf', 'foxglove.FrameTransforms', [], 10, null],
  ];
  const draft: VizMapping = { ...WAREHOUSE_MAPPING, name: 'Foxglove 通用' };
  const topics: McapTopic[] = rows.map(([topic, schema, fields, rate, image]) => {
    const series = draft.series.find((x) => x.topic === topic);
    const camera = draft.cameras.find((x) => x.topic === topic);
    return {
      topic,
      schema,
      schema_encoding: 'protobuf',
      message_encoding: 'protobuf',
      count: rate ? rate * 20 : 1,
      rate_hz: rate,
      start_s: 0,
      end_s: 20,
      image,
      fields: fields.map(([path, size]) => ({ path, size })),
      use: camera ? 'camera' : series ? 'series' : 'ignore',
      role: series?.role ?? null,
      name: camera?.name ?? series?.name ?? '',
      notes: rate && rate > 150 ? ['高频：作曲线时下采样到 ≤ 2000 点'] : [],
    };
  });
  return {
    file,
    files,
    topics,
    metadata: { episode: { task: 'pick the box onto the shelf' } },
    attachments: [],
    draft,
    matched: template ? { template_id: 'builtin:foxglove', name: 'Foxglove 通用（内置）', coverage: 1 } : null,
    warnings: [],
  };
}

/** The probe of a UMI-like mcap dataset (GenRobot), drafted with builtin:umi or another template; `abc` an ABC-130k-like one. */
export function mcapProbe(file: string, files: number, template: string | null, flavor: 'umi' | 'abc' | 'warehouse' = 'umi'): McapProbe {
  if (flavor === 'abc') return abcProbe(file, files, template);
  if (flavor === 'warehouse') return warehouseProbe(file, files, template);
  const umi = !template || template === 'builtin:umi';
  const topics: McapTopic[] = UMI_TOPICS.map(([topic, schema, use, count, rate]) => {
    const asCamera = use === 'camera';
    const asSeries = use === 'series' && umi;
    const draftUse = asCamera ? 'camera' : asSeries ? 'series' : template && template !== 'builtin:umi' && use === 'series' ? 'series' : 'ignore';
    return {
      topic,
      schema,
      schema_encoding: 'protobuf',
      message_encoding: 'protobuf',
      count,
      rate_hz: rate || null,
      start_s: 0,
      end_s: round(count / (rate || 1), 2),
      image: asCamera ? { codec: 'jpeg', width: 1280, height: 720 } : null,
      fields: topic.includes('eef_pose') ? [{ path: 'pose.position', size: 3 }, { path: 'pose.orientation', size: 4 }] : topic.includes('encoder') ? [{ path: 'value', size: 1 }] : null,
      use: draftUse as McapTopic['use'],
      role: draftUse === 'series' ? 'action' : null,
      name: asCamera ? 'robot0 camera0' : topic.includes('eef_pose') ? 'robot0 vio eef_pose' : topic.includes('encoder') ? 'robot0 magnetic_encoder' : '',
      notes: rate > 150 ? ['高频：作曲线时下采样到 ≤ 2000 点'] : [],
    };
  });
  const draft: VizMapping = {
    schema_version: 'viz-mapping/1.0',
    name: umi ? 'UMI 手持夹爪' : '按模版起草',
    base: umi ? 'builtin:umi' : 'builtin:foxglove',
    timeline: { source: 'log_time', frame_reference: null },
    cameras: topics.filter((t) => t.use === 'camera').map((t) => ({ topic: t.topic, name: t.name, schema: t.schema ?? undefined })),
    series: topics.filter((t) => t.use === 'series').map((t) => ({
      topic: t.topic,
      name: t.name || t.topic,
      schema: t.schema ?? undefined,
      fields: t.topic.includes('eef_pose') ? ['pose'] : ['value'],
      labels: t.topic.includes('eef_pose') ? ['x', 'y', 'z', 'qx', 'qy', 'qz', 'qw'].map((n) => `robot0_${n}`) : ['robot0_gripper'],
      unit: null,
      role: 'action' as const,
    })),
    task: null,
    segments: null,
    ignore: topics.filter((t) => t.use === 'ignore').map((t) => t.topic),
  };
  return {
    file,
    files,
    topics,
    metadata: { episode: { robot: 'das_gripper' } },
    attachments: [],
    draft,
    matched: umi
      ? { template_id: 'builtin:umi', name: 'UMI 手持夹爪（内置）', coverage: 0.86 }
      : { template_id: template!, name: BUILTIN_TEMPLATES.find((t) => t.id === template)?.name ?? template!, coverage: 1 },
    warnings: [],
  };
}

/** C4 ``DatasetAnnotationsInfo`` of a viz_annotations upload, as the Daemon makes it. */
export function annotationsInfo(up: Record<string, unknown>): { upload_id: string; name: string; format: string; episodes: number; uploaded_at: number } {
  const summary = ((up.validation as { summary?: Record<string, unknown> } | undefined)?.summary ?? {}) as { format?: string; episodes?: number };
  return { upload_id: String(up.upload_id), name: String(up.name), format: summary.format ?? 'argus', episodes: summary.episodes ?? 0, uploaded_at: Number(up.created_at) };
}

/** The check reader's mapping derived from a C7 mapping (design doc 18 §6.2), as the Daemon shows it. */
export function checkMappingOf(m: VizMapping): Record<string, unknown> {
  const pick = (role: 'state' | 'action'): { topic: string; fields: string | null }[] =>
    m.series.filter((s) => s.role === role).flatMap((s) => (s.fields?.length ? s.fields.map((f) => ({ topic: s.topic, fields: f as string | null })) : [{ topic: s.topic, fields: null }]));
  const action = pick('action');
  const state = pick('state');
  return {
    action: action.length ? action : state,
    state: action.length ? (state.length ? state : null) : null,
    task: m.task && 'topic' in m.task ? m.task.topic : '/task',
    video_prefix: '/observation.images.',
    video_topics: m.cameras.map((c) => c.topic),
    ...(m.base === 'builtin:umi' ? { profile: 'umi_das' } : {}),
  };
}
