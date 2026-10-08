// The mcap field mapping (C7 viz-mapping/1.1, design doc 18 §6; depth topics design doc 21 §5.4): what
// the mapping table shows for a probed topic and the edits it makes. Pure functions over the mapping; the drawer keeps it in state.
// The drafting rules mirror the Daemon's (curation/viz/mcap_mapping.py), so a topic turned into a
// curve by hand gets the fields, role and pairing the Daemon would have drafted.
import type { McapProbe, McapTopic, VizMapping } from '../api/types';
import { zh } from '../locales/zh';

export type MappingCamera = VizMapping['cameras'][number];
export type MappingDepth = NonNullable<VizMapping['depths']>[number];
export type MappingSeries = VizMapping['series'][number];
export type SeriesRole = MappingSeries['role'];
export type TopicUse = McapTopic['use'];
export type MappingTimeline = NonNullable<VizMapping['timeline']>;
export type TimelineSource = NonNullable<MappingTimeline['source']>;
export type MappingTask = VizMapping['task'];
export type MappingSegments = VizMapping['segments'];
/** The parts of a probed topic the edits read (a topic only the mapping names has just its name). */
export type TopicLike = Pick<McapTopic, 'topic'> & Partial<Pick<McapTopic, 'schema' | 'fields' | 'name' | 'image' | 'rate_hz'>>;

/** What a new mapping, or one given depth topics, says; a 1.0 mapping (no depths) stays valid as it is. */
export const SCHEMA_VERSION = 'viz-mapping/1.1';
export const SCHEMA_VERSIONS: readonly VizMapping['schema_version'][] = ['viz-mapping/1.0', 'viz-mapping/1.1'];
/** The picture codecs the probe gives a depth topic (rvl: recognised, not read). */
export const DEPTH_CODECS = ['png16', 'cdepth', 'cdepth32', 'raw16', 'raw32f', 'rvl'];
/** Above this rate an auxiliary curve (an IMU at 200 Hz) stays out of the smart layout, as the Daemon drafts it. */
export const HIGH_RATE_HZ = 150;
/** C7 `path`: field names, list indexes and `*`. */
export const PATH_RE = /^[A-Za-z_][A-Za-z0-9_]*(\.([A-Za-z_][A-Za-z0-9_]*|[0-9]+|\*))*$/;
export const ROLES: readonly SeriesRole[] = ['action', 'state', 'other'];
export const TIMELINE_SOURCES: readonly TimelineSource[] = ['log_time', 'publish_time', 'message_timestamp'];
export const TRANSFORMS = ['quat_xyzw_to_rpy', 'quat_wxyz_to_rpy', 'deg_to_rad', 'rad_to_deg'] as const;

function omit<T extends object, K extends keyof T>(obj: T, key: K): Omit<T, K> {
  const out = { ...obj };
  delete out[key];
  return out;
}

/** An empty mapping (a dataset whose probe drafted nothing). */
export function emptyMapping(name?: string): VizMapping {
  return { schema_version: SCHEMA_VERSION, ...(name ? { name } : {}), base: null, timeline: { source: 'log_time', frame_reference: null }, cameras: [], depths: [], series: [], task: null, segments: null, ignore: [] };
}

export const cameraOf = (m: VizMapping, topic: string): MappingCamera | undefined => m.cameras.find((c) => c.topic === topic);
export const depthOf = (m: VizMapping, topic: string): MappingDepth | undefined => (m.depths ?? []).find((d) => d.topic === topic);
export const seriesOf = (m: VizMapping, topic: string): MappingSeries | undefined => m.series.find((s) => s.topic === topic);
const taskTopic = (m: VizMapping): string | null => (m.task && 'topic' in m.task ? m.task.topic : null);
const segmentsTopic = (m: VizMapping): string | null => (m.segments && 'topic' in m.segments ? m.segments.topic : null);

/** What a mapping does with a topic (the probe table's 用途). */
export function usageOf(m: VizMapping, topic: string): TopicUse {
  if (cameraOf(m, topic)) return 'camera';
  if (depthOf(m, topic)) return 'depth';
  if (seriesOf(m, topic)) return 'series';
  if (taskTopic(m) === topic) return 'task';
  if (segmentsTopic(m) === topic) return 'segments';
  if ((m.ignore ?? []).includes(topic)) return 'ignore';
  return 'unmapped';
}

/** Every topic the mapping names (mapped or ignored), in its order. */
export function topicsOf(m: VizMapping): string[] {
  const out = [...m.cameras.map((c) => c.topic), ...(m.depths ?? []).map((d) => d.topic), ...m.series.map((s) => s.topic)];
  for (const t of [taskTopic(m), segmentsTopic(m), ...(m.ignore ?? [])]) if (t && !out.includes(t)) out.push(t);
  return out;
}

/** A display name as the Daemon drafts it: `/robot0/sensor/camera0/compressed` is `robot0 camera0`. */
export function displayName(topic: string): string {
  const parts = topic
    .replace(/^\/+|\/+$/g, '')
    .split('/')
    .filter((p) => p && !['compressed', 'image_raw', 'sensor', 'color'].includes(p));
  return parts.join(' ') || topic;
}

/** The role a topic's name suggests (`*-action`, `cmd` → action; `*-state`, `joint`, `pose` → state). */
export function roleByName(topic: string): SeriesRole {
  const t = topic.toLowerCase();
  if (['action', 'command', 'cmd', 'target', 'goal'].some((w) => t.includes(w))) return 'action';
  if (['state', 'feedback', 'obs', 'joint', 'pose', 'encoder', 'gripper'].some((w) => t.includes(w))) return 'state';
  return 'other';
}

/** The fields a topic turned into a curve starts with: a joint vector or a value, else its first six. */
export function defaultFields(t: Pick<TopicLike, 'fields'>): string[] | undefined {
  const paths = (t.fields ?? []).map((f) => f.path);
  for (const want of ['position', 'q', 'joint_positions', 'value', 'data']) if (paths.includes(want)) return [want];
  return paths.length ? paths.slice(0, 6) : undefined;
}

const PAIR = /^(.*?)([-_/])(state|action)$/;

/** `/left-arm-state` ↔ `/left-arm-action` (`_` and `/` too). */
export function partnerTopic(topic: string): string | null {
  const m = PAIR.exec(topic);
  return m ? `${m[1]}${m[2]}${m[3] === 'state' ? 'action' : 'state'}` : null;
}

const isDepthCodec = (codec: string | null | undefined): boolean => DEPTH_CODECS.includes(codec ?? '');

/** Whether a probed topic can be a camera (an image or video message that is not a depth picture). */
export function canBeCamera(t: TopicLike): boolean {
  return t.image ? !isDepthCodec(t.image.codec) : /image|video/i.test(t.schema ?? '');
}

/** Whether a probed topic can be a depth picture (the probe read one; a topic only the mapping names: an image schema). */
export function canBeDepth(t: TopicLike): boolean {
  return t.image ? isDepthCodec(t.image.codec) : /image|depth/i.test(t.schema ?? '');
}

/** The words that say which of a camera and its depth a topic is (the Daemon's ``_STEM_DROP``). */
const STEM_DROP = new Set(['depth', 'depths', 'camera', 'cam', 'color', 'colour', 'rgb', 'image', 'images', 'img', 'raw', 'compressed', 'compresseddepth', 'aligned', 'to', 'rect', 'sensor']);

/** A camera or depth topic without those words: `/front-depth` and `/front-camera` are both `front`. */
export function stemOf(topic: string): string {
  return topic
    .toLowerCase()
    .split(/[-/_.]+/)
    .filter((w) => w && !STEM_DROP.has(w))
    .join(' ');
}

/** The camera a depth topic belongs to, as the Daemon drafts it: the one camera of the same stem (none when several). */
export function pairCamera(m: VizMapping, topic: string): string | null {
  const hits = m.cameras.filter((c) => stemOf(c.topic) === stemOf(topic));
  return hits.length === 1 ? hits[0].topic : null;
}

/** Whether a probed topic has numbers to draw (unknown counts as yes). */
export function canBeSeries(t: TopicLike): boolean {
  return t.fields == null || t.fields.length > 0;
}

/** The mapping without the topic anywhere (a partner's pairing, a depth's camera and a frame reference to it go too). */
export function withoutTopic(m: VizMapping, topic: string): VizMapping {
  return {
    ...m,
    cameras: m.cameras.filter((c) => c.topic !== topic),
    ...(m.depths ? { depths: m.depths.filter((d) => d.topic !== topic).map((d) => (d.pair_with === topic ? { ...d, pair_with: null } : d)) } : {}),
    series: m.series.filter((s) => s.topic !== topic).map((s) => (s.pair_with === topic ? omit(s, 'pair_with') : s)),
    task: taskTopic(m) === topic ? null : m.task ?? null,
    segments: segmentsTopic(m) === topic ? null : m.segments ?? null,
    ignore: (m.ignore ?? []).filter((t) => t !== topic),
    ...(m.timeline?.frame_reference === topic ? { timeline: { ...m.timeline, frame_reference: null } } : {}),
  };
}

/** Pairs a curve with its `*-state` / `*-action` partner when that one is a curve and unpaired. */
function pairUp(m: VizMapping, topic: string): VizMapping {
  const other = partnerTopic(topic);
  const partner = other ? seriesOf(m, other) : undefined;
  if (!other || !partner || partner.pair_with) return m;
  const mine: SeriesRole = topic.endsWith('state') ? 'state' : 'action';
  const theirs: SeriesRole = mine === 'state' ? 'action' : 'state';
  return { ...m, series: m.series.map((s) => (s.topic === topic ? { ...s, role: mine, pair_with: other } : s.topic === other ? { ...s, role: theirs, pair_with: topic } : s)) };
}

function addIgnored(m: VizMapping, topic: string | null): VizMapping {
  if (!topic || usageOf(m, topic) !== 'unmapped') return m;
  return { ...m, ignore: [...(m.ignore ?? []), topic] };
}

/**
 * The mapping with `t` used as `use` (a curve with `role`). A curve keeps its fields and pairing when
 * only its role changes; a new one gets the Daemon's drafted fields and role. The topic a new task
 * text or segment topic replaces is ignored rather than left unmapped.
 */
export function setUse(m: VizMapping, t: TopicLike, use: TopicUse, role?: SeriesRole): VizMapping {
  const was = seriesOf(m, t.topic);
  if (use === 'series' && was) return role && role !== was.role ? setRole(m, t.topic, role) : m;
  if (use === 'depth' && depthOf(m, t.topic)) return m;
  const name = cameraOf(m, t.topic)?.name || depthOf(m, t.topic)?.name || was?.name || t.name || displayName(t.topic);
  const schema = t.schema ? { schema: t.schema } : {};
  const out = withoutTopic(m, t.topic);
  switch (use) {
    case 'camera':
      return { ...out, cameras: [...out.cameras, { topic: t.topic, name, ...schema }] };
    case 'depth':
      // a mapping with depths is a 1.1 one
      return { ...out, schema_version: SCHEMA_VERSION, depths: [...(out.depths ?? []), { topic: t.topic, name, ...schema, pair_with: pairCamera(out, t.topic) }] };
    case 'series': {
      const fields = defaultFields(t);
      const r = role ?? roleByName(t.topic);
      // a fast auxiliary sensor is offered in the + / 更换 menus only; arm states and actions stay in
      const aside = r === 'other' && ((t.rate_hz ?? 0) > HIGH_RATE_HZ || /imu/i.test(`${t.topic} ${t.schema ?? ''}`));
      const entry: MappingSeries = { topic: t.topic, name, ...schema, role: r, ...(fields ? { fields } : {}), ...(aside ? { smart: false } : {}) };
      const added = { ...out, series: [...out.series, entry] };
      // paired with its partner unless the role picked contradicts the name (`*-state` taken as action)
      return !role || role === PAIR.exec(t.topic)?.[3] ? pairUp(added, t.topic) : added;
    }
    case 'task':
      return addIgnored({ ...out, task: { topic: t.topic } }, taskTopic(out));
    case 'segments':
      return addIgnored({ ...out, segments: { topic: t.topic, ...segmentFields(t) } }, segmentsTopic(out));
    case 'ignore':
      return { ...out, ignore: [...(out.ignore ?? []), t.topic] };
    default:
      return out;
  }
}

/** Start / end / label fields of a segment topic, guessed from its numbers (the label is text). */
function segmentFields(t: TopicLike): { start_field: string; end_field: string; label_field: string } {
  const nums = new Set((t.fields ?? []).map((f) => f.path));
  for (const [a, b] of [
    ['start', 'end'],
    ['start_s', 'end_s'],
    ['start_time', 'end_time'],
    ['begin', 'end'],
  ]) {
    if (nums.has(a) && nums.has(b)) return { start_field: a, end_field: b, label_field: 'label' };
  }
  return { start_field: 'start', end_field: 'end', label_field: 'label' };
}

/** A curve's role; a pairing whose two sides would share a role is undone. */
export function setRole(m: VizMapping, topic: string, role: SeriesRole): VizMapping {
  const s = seriesOf(m, topic);
  if (!s || s.role === role) return m;
  const partner = s.pair_with ? seriesOf(m, s.pair_with) : undefined;
  const unpair = Boolean(partner && partner.role === role);
  return {
    ...m,
    series: m.series.map((x) => {
      if (x.topic === topic) return { ...(unpair ? omit(x, 'pair_with') : x), role };
      return unpair && x.topic === partner?.topic ? omit(x, 'pair_with') : x;
    }),
  };
}

export function setName(m: VizMapping, topic: string, name: string): VizMapping {
  return {
    ...m,
    cameras: m.cameras.map((c) => (c.topic === topic ? { ...c, name } : c)),
    ...(m.depths ? { depths: m.depths.map((d) => (d.topic === topic ? { ...d, name } : d)) } : {}),
    series: m.series.map((s) => (s.topic === topic ? { ...s, name } : s)),
  };
}

/**
 * A curve's fields; none = the whole message read by its shape. The labels (one per number) are
 * dropped once the fields change, and a transform stays only on a field still drawn.
 */
export function setFields(m: VizMapping, topic: string, fields: string[]): VizMapping {
  return {
    ...m,
    series: m.series.map((s) => {
      if (s.topic !== topic) return s;
      const same = JSON.stringify(s.fields ?? []) === JSON.stringify(fields);
      let next: MappingSeries = omit(s, 'fields');
      if (!same) next = omit(next, 'labels');
      if (fields.length) next = { ...next, fields };
      const kept = Object.entries(s.transforms ?? {}).filter(([k]) => fields.includes(k));
      next = omit(next, 'transforms');
      return kept.length ? { ...next, transforms: Object.fromEntries(kept) } : next;
    }),
  };
}

/** Whether a curve is in the smart layout (the default) or only offered in the + / 更换 menus. */
export function setSmart(m: VizMapping, topic: string, smart: boolean): VizMapping {
  return { ...m, series: m.series.map((s) => (s.topic !== topic ? s : smart ? omit(s, 'smart') : { ...s, smart: false })) };
}

/** Pairs two curves of opposite roles (drawn in one group), or undoes the curve's pairing. */
export function setPair(m: VizMapping, topic: string, other: string | null): VizMapping {
  const before = seriesOf(m, topic)?.pair_with ?? null;
  const theirsBefore = other ? seriesOf(m, other)?.pair_with ?? null : null;
  const loose = new Set([before, theirsBefore].filter((x): x is string => Boolean(x)));
  return {
    ...m,
    series: m.series.map((s) => {
      if (s.topic === topic) return other ? { ...s, pair_with: other } : omit(s, 'pair_with');
      if (other && s.topic === other) return { ...s, pair_with: topic };
      return loose.has(s.topic) && (s.pair_with === topic || s.pair_with === other) ? omit(s, 'pair_with') : s;
    }),
  };
}

/** The camera a depth topic is drawn over in the player (null: none). */
export function setDepthPair(m: VizMapping, topic: string, camera: string | null): VizMapping {
  return { ...m, depths: (m.depths ?? []).map((d) => (d.topic === topic ? { ...d, pair_with: camera } : d)) };
}

export function setTimeline(m: VizMapping, patch: Partial<MappingTimeline>): VizMapping {
  let t: MappingTimeline = { source: 'log_time', frame_reference: null, ...m.timeline, ...patch };
  if (t.source !== 'message_timestamp') t = omit(t, 'timestamp_field');
  return { ...m, timeline: t };
}

/** The task text's source; a topic taken for it leaves its old use, the topic it replaces is ignored. */
export function setTask(m: VizMapping, task: MappingTask): VizMapping {
  const old = taskTopic(m);
  const base = task && 'topic' in task ? withoutTopic(m, task.topic) : m;
  return addIgnored({ ...base, task: task ?? null }, old === (task && 'topic' in task ? task.topic : null) ? null : old);
}

export function setSegments(m: VizMapping, segments: MappingSegments): VizMapping {
  const old = segmentsTopic(m);
  const base = segments && 'topic' in segments ? withoutTopic(m, segments.topic) : m;
  return addIgnored({ ...base, segments: segments ?? null }, old === (segments && 'topic' in segments ? segments.topic : null) ? null : old);
}

// ---------------------------------------------------------------- summary and warnings

export interface MappingSummary {
  cameras: number;
  depths: number;
  series: number;
  action: number;
  state: number;
  other: number;
  task: boolean;
  segments: boolean;
  ignored: number;
  unmapped: string[];
  /** topics the mapping names that the probed file does not have */
  missing: string[];
}

export function summarize(m: VizMapping, probe: Pick<McapProbe, 'topics'>): MappingSummary {
  const have = new Set(probe.topics.map((t) => t.topic));
  return {
    cameras: m.cameras.length,
    depths: (m.depths ?? []).length,
    series: m.series.length,
    action: m.series.filter((s) => s.role === 'action').length,
    state: m.series.filter((s) => s.role === 'state').length,
    other: m.series.filter((s) => s.role === 'other').length,
    task: Boolean(m.task),
    segments: Boolean(m.segments),
    ignored: (m.ignore ?? []).length,
    unmapped: probe.topics.map((t) => t.topic).filter((t) => usageOf(m, t) === 'unmapped'),
    missing: topicsOf(m).filter((t) => !have.has(t)),
  };
}

export type MappingWarning = 'no_camera' | 'no_series' | 'no_anchor' | 'missing' | 'unmapped';

/** What the summary row warns about: the visualizer or the checks would miss something. */
export function warningsOf(s: MappingSummary): MappingWarning[] {
  const out: MappingWarning[] = [];
  if (!s.cameras) out.push('no_camera');
  if (!s.series) out.push('no_series');
  else if (!s.action && !s.state) out.push('no_anchor');
  if (s.missing.length) out.push('missing');
  if (s.unmapped.length) out.push('unmapped');
  return out;
}

// ---------------------------------------------------------------- validation (C7 + the rules it cannot say)

export interface MappingProblem {
  /** where: `series.2.role`, `timeline.frame_reference`, `<root>` */
  field: string;
  problem: string;
}

const P = zh.mcap.problem;
type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);

function checkKeys(v: Obj, allowed: readonly string[], at: string, out: MappingProblem[]): void {
  for (const k of Object.keys(v)) if (!allowed.includes(k)) out.push({ field: at ? `${at}.${k}` : k, problem: P.unknownKey });
}

function checkString(v: unknown, at: string, out: MappingProblem[], { min = 1, max }: { min?: number; max: number }): boolean {
  if (typeof v !== 'string') {
    out.push({ field: at, problem: P.notString });
    return false;
  }
  if (v.length < min) out.push({ field: at, problem: P.empty });
  else if (v.length > max) out.push({ field: at, problem: P.tooLong(max) });
  return true;
}

function checkPath(v: unknown, at: string, out: MappingProblem[]): void {
  if (checkString(v, at, out, { min: 1, max: 256 }) && !PATH_RE.test(v as string)) out.push({ field: at, problem: P.badPath(String(v)) });
}

function checkTopic(v: unknown, at: string, out: MappingProblem[]): void {
  checkString(v, at, out, { min: 1, max: 512 });
}

function checkRequired(v: Obj, keys: readonly string[], at: string, out: MappingProblem[]): void {
  for (const k of keys) if (!(k in v)) out.push({ field: at ? `${at}.${k}` : k, problem: P.missingKey });
}

function checkCamera(c: unknown, at: string, out: MappingProblem[]): void {
  if (!isObj(c)) return void out.push({ field: at, problem: P.notObject });
  checkKeys(c, ['topic', 'name', 'schema'], at, out);
  checkRequired(c, ['topic', 'name'], at, out);
  if ('topic' in c) checkTopic(c.topic, `${at}.topic`, out);
  if ('name' in c) checkString(c.name, `${at}.name`, out, { max: 64 });
  if ('schema' in c) checkString(c.schema, `${at}.schema`, out, { min: 0, max: 256 });
}

function checkDepth(d: unknown, at: string, out: MappingProblem[]): void {
  if (!isObj(d)) return void out.push({ field: at, problem: P.notObject });
  checkKeys(d, ['topic', 'name', 'schema', 'pair_with', 'unit'], at, out);
  checkRequired(d, ['topic', 'name'], at, out);
  if ('topic' in d) checkTopic(d.topic, `${at}.topic`, out);
  if ('name' in d) checkString(d.name, `${at}.name`, out, { max: 64 });
  if ('schema' in d) checkString(d.schema, `${at}.schema`, out, { min: 0, max: 256 });
  if ('pair_with' in d && d.pair_with !== null) checkTopic(d.pair_with, `${at}.pair_with`, out);
  if ('unit' in d && !['mm', 'm'].includes(d.unit as string)) out.push({ field: `${at}.unit`, problem: P.depthUnit });
}

function checkSeries(s: unknown, at: string, out: MappingProblem[]): void {
  if (!isObj(s)) return void out.push({ field: at, problem: P.notObject });
  checkKeys(s, ['topic', 'name', 'schema', 'fields', 'labels', 'names_field', 'transforms', 'unit', 'role', 'pair_with', 'smart'], at, out);
  checkRequired(s, ['topic', 'name', 'role'], at, out);
  if ('topic' in s) checkTopic(s.topic, `${at}.topic`, out);
  if ('name' in s) checkString(s.name, `${at}.name`, out, { max: 64 });
  if ('schema' in s) checkString(s.schema, `${at}.schema`, out, { min: 0, max: 256 });
  if ('role' in s && !ROLES.includes(s.role as SeriesRole)) out.push({ field: `${at}.role`, problem: P.role });
  if ('fields' in s) {
    if (!Array.isArray(s.fields)) out.push({ field: `${at}.fields`, problem: P.notPathList });
    else if (s.fields.length > 64) out.push({ field: `${at}.fields`, problem: P.tooManyFields });
    else s.fields.forEach((f, i) => checkPath(f, `${at}.fields.${i}`, out));
  }
  if ('labels' in s) {
    if (!Array.isArray(s.labels)) out.push({ field: `${at}.labels`, problem: P.notNameList });
    else if (s.labels.length > 512) out.push({ field: `${at}.labels`, problem: P.tooManyNames });
    else s.labels.forEach((l, i) => checkString(l, `${at}.labels.${i}`, out, { max: 64 }));
  }
  if ('names_field' in s) checkPath(s.names_field, `${at}.names_field`, out);
  if ('transforms' in s) {
    if (!isObj(s.transforms)) out.push({ field: `${at}.transforms`, problem: P.notTransforms });
    else
      for (const [k, v] of Object.entries(s.transforms)) {
        checkPath(k, `${at}.transforms`, out);
        if (!(TRANSFORMS as readonly string[]).includes(v as string)) out.push({ field: `${at}.transforms.${k}`, problem: P.transform(TRANSFORMS) });
      }
  }
  if ('unit' in s && s.unit !== null) checkString(s.unit, `${at}.unit`, out, { min: 0, max: 32 });
  if ('pair_with' in s && s.pair_with !== null) checkTopic(s.pair_with, `${at}.pair_with`, out);
  if ('smart' in s && typeof s.smart !== 'boolean') out.push({ field: `${at}.smart`, problem: P.notBool });
}

function checkShape(v: unknown, out: MappingProblem[]): v is VizMapping {
  if (!isObj(v)) {
    out.push({ field: '<root>', problem: P.notMapping });
    return false;
  }
  checkKeys(v, ['schema_version', 'name', 'base', 'timeline', 'cameras', 'depths', 'series', 'task', 'segments', 'ignore'], '', out);
  checkRequired(v, ['schema_version', 'cameras', 'series'], '', out);
  if ('schema_version' in v && !SCHEMA_VERSIONS.includes(v.schema_version as VizMapping['schema_version'])) out.push({ field: 'schema_version', problem: P.version(SCHEMA_VERSIONS) });
  if ('name' in v) checkString(v.name, 'name', out, { max: 128 });
  if ('base' in v && ![null, 'builtin:foxglove', 'builtin:ros2', 'builtin:umi'].includes(v.base as string | null)) out.push({ field: 'base', problem: P.base });
  if ('timeline' in v) {
    const t = v.timeline;
    if (!isObj(t)) out.push({ field: 'timeline', problem: P.notObject });
    else {
      checkKeys(t, ['source', 'timestamp_field', 'frame_reference'], 'timeline', out);
      if ('source' in t && !TIMELINE_SOURCES.includes(t.source as TimelineSource)) out.push({ field: 'timeline.source', problem: P.timelineSource });
      if ('timestamp_field' in t) checkPath(t.timestamp_field, 'timeline.timestamp_field', out);
      if (t.source === 'message_timestamp' && !('timestamp_field' in t)) out.push({ field: 'timeline.timestamp_field', problem: P.needTimestamp });
      if ('frame_reference' in t && t.frame_reference !== null) checkTopic(t.frame_reference, 'timeline.frame_reference', out);
    }
  }
  for (const [key, max, check] of [
    ['cameras', 32, checkCamera],
    ['depths', 32, checkDepth],
    ['series', 128, checkSeries],
  ] as const) {
    if (!(key in v)) continue;
    const list = v[key];
    if (!Array.isArray(list)) out.push({ field: key, problem: P.notList });
    else if (list.length > max) out.push({ field: key, problem: P.tooMany(max) });
    else list.forEach((x, i) => check(x, `${key}.${i}`, out));
  }
  if ('task' in v && v.task !== null) {
    const t = v.task;
    if (!isObj(t) || ('metadata_key' in t) === ('topic' in t)) out.push({ field: 'task', problem: P.task });
    else if ('metadata_key' in t) {
      checkKeys(t, ['metadata_key'], 'task', out);
      checkString(t.metadata_key, 'task.metadata_key', out, { max: 128 });
    } else {
      checkKeys(t, ['topic', 'field'], 'task', out);
      checkTopic(t.topic, 'task.topic', out);
      if ('field' in t) checkPath(t.field, 'task.field', out);
    }
  }
  if ('segments' in v && v.segments !== null) {
    const s = v.segments;
    if (!isObj(s) || ('attachment' in s) === ('topic' in s)) out.push({ field: 'segments', problem: P.segments });
    else if ('attachment' in s) {
      checkKeys(s, ['attachment'], 'segments', out);
      checkString(s.attachment, 'segments.attachment', out, { max: 256 });
    } else {
      checkKeys(s, ['topic', 'start_field', 'end_field', 'label_field'], 'segments', out);
      checkRequired(s, ['start_field', 'end_field', 'label_field'], 'segments', out);
      checkTopic(s.topic, 'segments.topic', out);
      for (const k of ['start_field', 'end_field', 'label_field']) if (k in s) checkPath(s[k], `segments.${k}`, out);
    }
  }
  if ('ignore' in v) {
    const list = v.ignore;
    if (!Array.isArray(list)) out.push({ field: 'ignore', problem: P.notTopicList });
    else {
      list.forEach((t, i) => checkTopic(t, `ignore.${i}`, out));
      if (new Set(list).size !== list.length) out.push({ field: 'ignore', problem: P.duplicateTopics });
    }
  }
  return !out.length;
}

/**
 * Every problem of a mapping, located (empty = fine): the C7 Schema, then what it cannot say - a
 * topic used twice, a depth's camera that is not a mapped camera, pairs of one role or a partner that
 * is not a curve, an ignored topic that is also mapped, a frame reference that is not mapped, and
 * (given `topics`) topics the dataset lacks.
 * The same rules as the Daemon's, so an import fails here the way a save would.
 */
export function validateMapping(raw: unknown, topics?: ReadonlySet<string>): MappingProblem[] {
  const out: MappingProblem[] = [];
  if (!checkShape(raw, out)) return out;
  const m = raw;
  const uses = new Map<string, string>();
  const use = (topic: string, where: string) => {
    const prev = uses.get(topic);
    if (prev) out.push({ field: where, problem: P.usedTwice(topic, prev, where) });
    else uses.set(topic, where);
  };
  m.cameras.forEach((c, i) => use(c.topic, `cameras.${i}`));
  const cameras = new Set(m.cameras.map((c) => c.topic));
  (m.depths ?? []).forEach((d, i) => {
    use(d.topic, `depths.${i}`);
    if (d.pair_with && !cameras.has(d.pair_with)) out.push({ field: `depths.${i}.pair_with`, problem: P.notCamera(d.pair_with) });
  });
  const byTopic = new Map(m.series.map((s) => [s.topic, s]));
  m.series.forEach((s, i) => {
    use(s.topic, `series.${i}`);
    if (!s.pair_with) return;
    const partner = byTopic.get(s.pair_with);
    if (!partner) out.push({ field: `series.${i}.pair_with`, problem: P.notSeries(s.pair_with) });
    else if (partner.role === s.role) out.push({ field: `series.${i}.pair_with`, problem: P.pairRoles });
    else if (partner.pair_with && partner.pair_with !== s.topic) out.push({ field: `series.${i}.pair_with`, problem: P.pairTaken(s.pair_with) });
  });
  (m.ignore ?? []).forEach((t, i) => {
    if (uses.has(t)) out.push({ field: `ignore.${i}`, problem: P.ignoredMapped(t) });
  });
  const ref = m.timeline?.frame_reference;
  if (ref && !uses.has(ref)) out.push({ field: 'timeline.frame_reference', problem: P.frameRef(ref) });
  if (topics) {
    const named = new Set([...uses.keys(), taskTopic(m), segmentsTopic(m), ...(m.ignore ?? [])].filter((t): t is string => Boolean(t)));
    for (const t of [...named].sort()) if (!topics.has(t)) out.push({ field: uses.get(t) ?? 'ignore', problem: P.missingTopic(t) });
  }
  return out;
}

/** The mapping as the JSON a person exports, imports or saves as a template. */
export function mappingJson(m: VizMapping): string {
  return `${JSON.stringify(m, null, 2)}\n`;
}

/** A file name for an exported mapping. */
export function exportName(m: VizMapping): string {
  const stem = (m.name ?? '').replace(/[\\/:*?"<>|\s]+/g, '_').replace(/^_+|_+$/g, '');
  return `${stem || 'viz-mapping'}.json`;
}
