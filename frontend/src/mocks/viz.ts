// MSW handlers of the data visualizer (design doc 18, C4 2.4.0): the dataset scope (the visualize
// page), the task scope (the mini player), mcap mappings, templates, and the cameras the Daemon
// serves (mp4 / JPEG frame pack, Range, 202 while a transcode runs).
import { HttpResponse, http } from 'msw';
import type { DatasetDetail, McapProbeRequest, Task, VizDisplayConfig, VizMapping, VizTemplate } from '../api/types';
import { apiBaseUrl } from '../base';
import { clock, db, findTask, nextId } from './db';
import { eefOverlay } from './eef';
import { API, body, cursorPage, err, idempotent } from './plumbing';
import { DATASET_PROFILES, datasetFormatOf, MCAP_URI, profileFor } from './world';
import {
  annotationsInfo,
  baseModel,
  BUILTIN_TEMPLATES,
  checkDisplay,
  checkMappingOf,
  depthPack,
  displayDefaults,
  episodeItems,
  fakeVideo,
  frameIndex,
  framePack,
  framesOf,
  fromTemplate,
  mappingInfoOf,
  mcapProbe,
  sampleIndex,
  vizDataset,
  vizEpisode,
  vizSeries,
  vizFormatOf,
  vizStatusOf,
} from './vizWorld';

const EEF = 'eef_video_consistency';

// ------------------------------------------------------------------ sources

interface Source {
  scope: 'dataset' | 'task';
  id: string;
  dataset: DatasetDetail;
  mapping: VizMapping | null;
  mappingVersion: number | null;
}

function datasetSource(id: string): Source | Response {
  const d = db.datasets.find((x) => x.id === id);
  if (!d) return err(404, 'not_found', '数据集登记不存在，可能已被删除');
  const stored = db.vizMappings.get(d.id);
  return { scope: 'dataset', id, dataset: d, mapping: stored?.mapping ?? null, mappingVersion: stored?.version ?? null };
}

/** The task's input as a registration-shaped record: its own, or one made from its address. */
function taskDataset(t: Task): DatasetDetail {
  const own = db.datasets.find((d) => d.id === t.dataset_id) ?? db.datasets.find((d) => d.uri === t.input.uri);
  if (own) return own;
  const p = profileFor(t.input.uri);
  const any = db.datasets[0];
  return { ...any, id: '', name: p.name, uri: p.uri, source: p.source, format: datasetFormatOf(p.format) };
}

function taskSource(id: string): Source | Response {
  const t = findTask(id);
  if (!t) return err(404, 'not_found', '任务不存在');
  const d = taskDataset(t);
  const stored = d.id ? db.vizMappings.get(d.id) : undefined;
  return { scope: 'task', id, dataset: d, mapping: stored?.mapping ?? null, mappingVersion: stored?.version ?? null };
}

function modelOf(s: Source) {
  const display = s.dataset.id ? (db.vizDisplays.get(s.dataset.id)?.config ?? null) : null;
  return vizDataset(s.scope, s.id, s.dataset, s.mapping, s.mappingVersion, display);
}

/** ``VizDisplay`` of a registration (design doc 21 §6). */
function displayDoc(d: DatasetDetail) {
  const stored = db.vizDisplays.get(d.id);
  const mapping = db.vizMappings.get(d.id);
  const base = baseModel('dataset', d.id, d, mapping?.mapping ?? null, mapping?.version ?? null);
  return { dataset_id: d.id, config: stored?.config ?? null, version: stored?.version ?? 0, updated_at: stored?.updatedAt ?? null, defaults: displayDefaults(base) };
}

function profileOf(d: DatasetDetail) {
  return DATASET_PROFILES.find((p) => p.uri === d.uri) ?? DATASET_PROFILES[1];
}

function episodeOf(s: Source, raw: unknown): number | Response {
  const index = Number(raw);
  const p = profileOf(s.dataset);
  if (!Number.isInteger(index) || index < 0 || index >= p.episodes) return err(404, 'not_found', `没有 episode ${String(raw)}`);
  return index;
}

function urlsFor(s: Source, index: number) {
  const scopePath = s.scope === 'dataset' ? `datasets/${s.id}` : `tasks/${s.id}`;
  const host = s.dataset.uri.replace(/^tos:\/\//, '').split('/')[0];
  const prefix = s.dataset.uri.replace(/^tos:\/\/[^/]+\/?/, '');
  return {
    direct: (key: string) => `https://${host}.tos-cn-beijing.volces.com/${prefix}/${key}?X-Tos-Expires=1800&X-Tos-Signature=mock`,
    daemon: (camera: string, suffix: 'mp4' | 'frames' | 'json', transcode = false) =>
      `${apiBaseUrl()}/${scopePath}/episodes/${index}/cameras/${camera}.${suffix}${transcode ? '?transcode=1' : ''}`,
    stream: (key: string, suffix: 'frames' | 'json') => `${apiBaseUrl()}/${scopePath}/episodes/${index}/streams/${key}.${suffix}`,
  };
}

function needsMapping(s: Source): Response | null {
  if (vizFormatOf(s.dataset) === 'mcap' && !s.mapping) {
    return err(400, 'validation_failed', 'mcap 数据集还没有确认字段映射：到「mcap 配置」确认后才能看', { reason: 'mapping_pending' });
  }
  return null;
}

// ------------------------------------------------------------------ bytes with Range

function ranged(request: Request, data: Uint8Array, type: string): Response {
  const total = data.length;
  const m = /^bytes=(\d*)-(\d*)$/.exec((request.headers.get('range') ?? '').trim());
  const headers: Record<string, string> = { 'Accept-Ranges': 'bytes', 'Content-Type': type };
  if (!m || (!m[1] && !m[2])) {
    return new HttpResponse(data, { status: 200, headers: { ...headers, 'Content-Length': String(total) } });
  }
  let start: number;
  let end: number;
  if (m[1]) {
    start = Number(m[1]);
    end = m[2] ? Math.min(Number(m[2]), total - 1) : total - 1;
  } else {
    const n = Math.min(Number(m[2]), total);
    start = total - n;
    end = total - 1;
  }
  if (start >= total || start > end) return new HttpResponse(null, { status: 416, headers: { ...headers, 'Content-Range': `bytes */${total}` } });
  const part = data.slice(start, end + 1);
  return new HttpResponse(part, { status: 206, headers: { ...headers, 'Content-Range': `bytes ${start}-${end}/${total}`, 'Content-Length': String(part.length) } });
}

function cameraBytes(s: Source, request: Request, index: number, file: string): Response {
  const m = /^([0-9A-Za-z_-]+)\.(mp4|frames|json)$/.exec(file);
  if (!m) return err(404, 'not_found', `没有 ${file}`);
  const [, key, suffix] = m;
  const model = modelOf(s);
  const cam = model.cameras.find((c) => c.key === key);
  if (!cam) return err(404, 'not_found', `episode ${index} 没有相机 ${key}`);
  const frames = framesOf(profileOf(s.dataset), index);
  if (suffix === 'json' || suffix === 'frames') {
    if (cam.access === 'remux') {
      // an mcap H.264 camera's sample pack (design doc 19 §3): fake bytes, a keyframe every second
      return suffix === 'json' ? HttpResponse.json(sampleIndex(key, frames, cam.fps ?? 30)) : ranged(request, framePack(frames), 'application/octet-stream');
    }
    if (cam.kind !== 'frames') return err(404, 'not_found', `相机 ${key} 不是帧包`, { reason: 'not_frames' });
    return suffix === 'json' ? HttpResponse.json(frameIndex(key, frames, cam.fps ?? 30)) : ranged(request, framePack(frames), 'application/octet-stream');
  }
  const transcode = new URL(request.url).searchParams.get('transcode') === 'true' || new URL(request.url).searchParams.get('transcode') === '1';
  if (transcode || cam.access === 'transcode') {
    // the first ask starts the transcode; the next one finds it ready (design doc 18 §4.2)
    const job = `${s.scope}:${s.id}:${index}:${key}`;
    if (!db.vizTranscodes.has(job)) {
      db.vizTranscodes.add(job);
      return HttpResponse.json({ state: 'pending', progress: 0.4, message: '平台转码中' }, { status: 202 });
    }
  } else if (cam.access === 'direct') {
    return err(404, 'not_found', `相机 ${key} 直连 TOS，不经 Daemon`, { reason: 'direct' });
  }
  return ranged(request, fakeVideo(), 'video/mp4');
}

/** A depth stream's pack (design doc 21 §5.3): the first ask of its index answers 202, as the Daemon does while it makes it. */
async function streamBytes(s: Source, request: Request, index: number, file: string): Promise<Response> {
  const m = /^([0-9A-Za-z_-]+)\.(frames|json)$/.exec(file);
  if (!m) return err(404, 'not_found', `没有 ${file}`);
  const [, key, suffix] = m;
  const stream = modelOf(s).streams.find((x) => x.key === key && x.kind === 'depth');
  if (!stream) return err(404, 'not_found', `没有深度流 ${key}`, { reason: 'unknown_stream' });
  const job = `${s.scope}:${s.id}:${index}:${key}`;
  if (!db.vizDepthPacks.has(job)) {
    db.vizDepthPacks.add(job);
    return HttpResponse.json({ state: 'pending', progress: 0.5, message: '深度图生成中' }, { status: 202 });
  }
  const p = profileOf(s.dataset);
  const pack = await depthPack(key, framesOf(p, index), p.fps ?? 30);
  return suffix === 'json' ? HttpResponse.json(pack.index) : ranged(request, pack.bytes, 'application/octet-stream');
}

// ------------------------------------------------------------------ handlers

function withSource(make: (id: string) => Source | Response, run: (s: Source, request: Request, params: Record<string, unknown>) => Response | Promise<Response>) {
  return ({ request, params }: { request: Request; params: Record<string, unknown> }) => {
    const s = make(String(params.id));
    if (s instanceof Response) return s;
    return run(s, request, params);
  };
}

function scoped(prefix: string, make: (id: string) => Source | Response) {
  return [
    http.get(`${API}/${prefix}/:id/viz`, withSource(make, (s) => HttpResponse.json(modelOf(s)))),
    http.get(`${API}/${prefix}/:id/episodes/:index/viz`, withSource(make, (s, _request, params) => {
      const index = episodeOf(s, params.index);
      if (index instanceof Response) return index;
      const blocked = needsMapping(s);
      if (blocked) return blocked;
      const model = modelOf(s);
      const ep = vizEpisode(s.scope, s.id, model, index, s.dataset, urlsFor(s, index), clock());
      // the configured track is the primary one (the Daemon's apply_episode)
      const track = model.display?.track;
      if (track && ep.annotations.tracks.some((t) => t.key === track)) ep.annotations.tracks = ep.annotations.tracks.map((t) => ({ ...t, primary: t.key === track }));
      return HttpResponse.json(ep);
    })),
    http.get(`${API}/${prefix}/:id/episodes/:index/series`, withSource(make, (s, request, params) => {
      const index = episodeOf(s, params.index);
      if (index instanceof Response) return index;
      const blocked = needsMapping(s);
      if (blocked) return blocked;
      const q = new URL(request.url).searchParams;
      const stream = q.get('stream') ?? '';
      const num = (k: string) => (q.get(k) === null ? null : Number(q.get(k)));
      const points = num('points') ?? 2000;
      if (!Number.isInteger(points) || points < 100 || points > 20000) return err(400, 'validation_failed', 'points 在 100 到 20000 之间');
      const out = vizSeries(modelOf(s), s.dataset, index, stream, num('from'), num('to'), points);
      return out ? HttpResponse.json(out) : err(404, 'not_found', `没有曲线组 ${stream}`, { reason: 'unknown_stream' });
    })),
    http.get(`${API}/${prefix}/:id/episodes/:index/cameras/:file`, withSource(make, (s, request, params) => {
      const index = episodeOf(s, params.index);
      if (index instanceof Response) return index;
      return cameraBytes(s, request, index, String(params.file));
    })),
    http.get(`${API}/${prefix}/:id/episodes/:index/streams/:file`, withSource(make, (s, request, params) => {
      const index = episodeOf(s, params.index);
      if (index instanceof Response) return index;
      return streamBytes(s, request, index, String(params.file));
    })),
  ];
}

export const vizHandlers = [
  ...scoped('datasets', datasetSource),
  ...scoped('tasks', taskSource),
  // the EEF marks (design docs 20, 22): the opinion record's camera `ext` over the input's first camera, for a
  // task that ran the module (selected it, or an EEF record was put in); 404 no_eef_module otherwise
  http.get(`${API}/tasks/:id/episodes/:index/eef-overlay`, withSource(taskSource, (s, _request, params) => {
    const index = episodeOf(s, params.index);
    if (index instanceof Response) return index;
    const records = [...(db.extraRecords.get(s.id)?.values() ?? [])];
    const ran = !!findTask(s.id)?.modules.some((m) => m.id === EEF && m.selected) || records.some((r) => EEF in r);
    if (!ran) return err(404, 'not_found', '这个任务没有勾选「EEF–视频一致性」，没有可叠加的投影', { reason: 'no_eef_module' });
    const measured = records.some((r) => r[EEF]?.details && (r[EEF].details as Record<string, unknown>).assessment_mode !== 'vlm_opinion');
    return HttpResponse.json(eefOverlay(s.id, index, modelOf(s).cameras[0]?.key ?? null, measured));
  })),
  http.get(`${API}/datasets/:id/viz/episodes`, ({ request, params }) => {
    const s = datasetSource(String(params.id));
    if (s instanceof Response) return s;
    const url = new URL(request.url);
    const q = (url.searchParams.get('q') ?? '').trim().toLowerCase();
    const sort = url.searchParams.get('sort') ?? 'index';
    const desc = url.searchParams.get('order') === 'desc';
    const num = /^(?:ep\s*)?(\d+)$/.exec(q);
    let items = episodeItems(s.dataset).filter((e) => !q || (num ? e.index === Number(num[1]) : e.task.toLowerCase().includes(q)));
    const key = (e: (typeof items)[number]) => (sort === 'duration' ? (e.duration_s ?? 0) : sort === 'steps' ? (e.steps ? 1 : 0) : e.index);
    items = items.sort((a, b) => (key(a) - key(b) || a.index - b.index) * (desc ? -1 : 1));
    return HttpResponse.json({ ...cursorPage(items, url, 50), total: items.length });
  }),
  http.get(`${API}/datasets/:id/viz/meta`, ({ request, params }) => {
    const s = datasetSource(String(params.id));
    if (s instanceof Response) return s;
    const path = new URL(request.url).searchParams.get('path') ?? '';
    if (!/^(meta\/[^/.][^/]*|README\.md)$/.test(path)) return err(400, 'validation_failed', `只能看数据集信息树里列出的元数据文件，不能看 ${path}`);
    const p = profileOf(s.dataset);
    const text = path.endsWith('info.json')
      ? JSON.stringify({ codebase_version: p.format.version === 'v3' ? 'v3.0' : 'v2.1', robot_type: p.robotType, fps: p.fps, total_episodes: p.episodes }, null, 2)
      : path.endsWith('.jsonl')
        ? '{"task_index": 0, "task": "Put the marker in the cup"}\n'
        : '{}';
    return HttpResponse.json({ path, size: text.length, truncated: false, kind: path.endsWith('.jsonl') ? 'jsonl' : path.endsWith('.json') ? 'json' : 'markdown', text });
  }),
  http.get(`${API}/datasets/:id/mapping`, ({ params }) => {
    const d = db.datasets.find((x) => x.id === params.id);
    if (!d) return err(404, 'not_found', '数据集登记不存在，可能已被删除');
    if (vizFormatOf(d) !== 'mcap') return err(400, 'validation_failed', '只有 mcap 数据集有字段映射');
    const stored = db.vizMappings.get(d.id);
    return HttpResponse.json({
      dataset_id: d.id,
      state: stored ? 'confirmed' : 'none',
      mapping: stored?.mapping ?? null,
      version: stored?.version ?? 0,
      updated_at: stored?.updatedAt ?? null,
      check_mapping: stored ? checkMappingOf(stored.mapping) : null,
      warnings: [],
    });
  }),
  http.put(`${API}/datasets/:id/mapping`, async ({ request, params }) =>
    idempotent(request, async () => {
      const d = db.datasets.find((x) => x.id === params.id);
      if (!d) return err(404, 'not_found', '数据集登记不存在，可能已被删除');
      const b = await body<{ mapping: VizMapping }>(request, 'putDatasetMapping');
      if (vizFormatOf(d) !== 'mcap') return err(400, 'validation_failed', '只有 mcap 数据集有字段映射');
      const known = new Set(['/observation.images.front', '/observation.images.wrist', '/observation.state', '/action', '/imu', '/tf', '/camera_info', ...(['umi', 'abc', 'robomind'] as const).flatMap((f) => mcapProbe('x', 1, null, f).topics.map((t) => t.topic))]);
      const unknown = [...b.mapping.cameras, ...(b.mapping.depths ?? []), ...b.mapping.series].map((x) => x.topic).filter((t) => !known.has(t));
      if (unknown.length) {
        return err(400, 'validation_failed', `映射里有数据集没有的 topic：${unknown.join('、')}`, { errors: unknown.map((t) => ({ field: 'mapping', problem: `unknown topic ${t}` })) });
      }
      const prev = db.vizMappings.get(d.id);
      const now = clock();
      const next = { mapping: b.mapping, version: (prev?.version ?? 0) + 1, updatedAt: now };
      db.vizMappings.set(d.id, next);
      d.viz = vizStatusOf('mcap', true);
      d.viz_mapping = mappingInfoOf('mcap', next);
      return HttpResponse.json({ dataset_id: d.id, state: 'confirmed', mapping: next.mapping, version: next.version, updated_at: now, check_mapping: checkMappingOf(next.mapping), warnings: [] });
    }),
  ),
  http.get(`${API}/datasets/:id/viz/display`, ({ params }) => {
    const d = db.datasets.find((x) => x.id === params.id);
    if (!d) return err(404, 'not_found', '数据集登记不存在，可能已被删除');
    return HttpResponse.json(displayDoc(d));
  }),
  http.put(`${API}/datasets/:id/viz/display`, async ({ request, params }) =>
    idempotent(request, async () => {
      const d = db.datasets.find((x) => x.id === params.id);
      if (!d) return err(404, 'not_found', '数据集登记不存在，可能已被删除');
      const b = await body<{ config: VizDisplayConfig }>(request, 'putDatasetVizDisplay');
      const mapping = db.vizMappings.get(d.id);
      const problems = checkDisplay(b.config, baseModel('dataset', d.id, d, mapping?.mapping ?? null, mapping?.version ?? null));
      if (problems.length) return err(400, 'validation_failed', `展示配置有 ${problems.length} 处对不上这个数据集`, { errors: problems });
      db.vizDisplays.set(d.id, { config: b.config, version: (db.vizDisplays.get(d.id)?.version ?? 0) + 1, updatedAt: clock() });
      return HttpResponse.json(displayDoc(d));
    }),
  ),
  http.delete(`${API}/datasets/:id/viz/display`, async ({ request, params }) =>
    idempotent(request, () => {
      const d = db.datasets.find((x) => x.id === params.id);
      if (!d) return err(404, 'not_found', '数据集登记不存在，可能已被删除');
      db.vizDisplays.set(d.id, { config: null, version: (db.vizDisplays.get(d.id)?.version ?? 0) + 1, updatedAt: clock() });
      return HttpResponse.json(displayDoc(d));
    }),
  ),
  http.put(`${API}/datasets/:id/annotations`, async ({ request, params }) =>
    idempotent(request, async () => {
      const d = db.datasets.find((x) => x.id === params.id);
      if (!d) return err(404, 'not_found', '数据集登记不存在，可能已被删除');
      const b = await body<{ upload_id: string | null }>(request, 'putDatasetAnnotations');
      const up = b.upload_id ? db.uploads.get(b.upload_id) : null;
      if (b.upload_id && !up) return err(404, 'not_found', '没有这个上传件');
      if (up && up.kind !== 'viz_annotations') return err(400, 'validation_failed', '这个上传件不是外部标注文件（kind 应为 viz_annotations）', { errors: [{ field: 'upload_id', problem: 'wrong kind' }] });
      d.annotations = up ? annotationsInfo(up) : null;
      return HttpResponse.json(d);
    }),
  ),
  http.post(`${API}/viz/mcap-probe`, async ({ request }) =>
    idempotent(request, async () => {
      const b = await body<McapProbeRequest>(request, 'probeMcap');
      const input = b.input;
      const ds = 'dataset_id' in input ? db.datasets.find((d) => d.id === input.dataset_id) : null;
      if ('dataset_id' in input && !ds) return err(404, 'not_found', '数据集登记不存在，可能已被删除');
      const uri = ds ? ds.uri : 'uri' in input ? input.uri : '';
      if (ds ? vizFormatOf(ds) !== 'mcap' : !MCAP_URI.test(uri)) return err(400, 'validation_failed', '这不是 mcap 数据集：没有 episode_N.mcap', { reason: 'not_mcap' });
      const flavor = /abc/i.test(uri) ? 'abc' : /warehouse/i.test(uri) ? 'warehouse' : /robomind/i.test(uri) ? 'robomind' : 'umi';
      const team = b.template && !b.template.startsWith('builtin:') ? db.vizTemplates.find((t) => t.id === b.template) : undefined;
      if (b.template && !b.template.startsWith('builtin:') && !team) return err(404, 'not_found', '没有这个模版');
      const first = flavor === 'umi' ? 'episode_100110.mcap' : 'episode_0.mcap';
      const probe = mcapProbe(b.file ?? first, flavor === 'umi' ? 12 : 4, team ? null : b.template ?? null, flavor);
      if (team) return HttpResponse.json(fromTemplate(team, probe));
      // without a template, a team template that names exactly these topics wins (design doc 18 §6.3)
      const fits = b.template ? undefined : db.vizTemplates.find((t) => fromTemplate(t, probe).matched?.coverage === 1);
      return HttpResponse.json(fits ? fromTemplate(fits, probe) : probe);
    }),
  ),
  http.get(`${API}/viz/templates`, () => HttpResponse.json({ items: [...BUILTIN_TEMPLATES, ...[...db.vizTemplates].sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))] })),
  http.post(`${API}/viz/templates`, async ({ request }) =>
    idempotent(request, async () => {
      const b = await body<{ name: string; description?: string; mapping: VizMapping }>(request, 'createVizTemplate');
      if (db.vizTemplates.some((t) => t.name === b.name) || BUILTIN_TEMPLATES.some((t) => t.name === b.name)) return err(409, 'name_taken', `已经有叫「${b.name}」的模版`);
      const now = clock();
      const t: VizTemplate = { id: nextId('vt'), name: b.name, description: b.description ?? '', builtin: false, mapping: b.mapping, created_at: now, updated_at: now };
      db.vizTemplates.push(t);
      return HttpResponse.json(t, { status: 201 });
    }),
  ),
  http.delete(`${API}/viz/templates/:tid`, async ({ request, params }) =>
    idempotent(request, () => {
      const id = String(params.tid);
      if (id.startsWith('builtin:')) return err(400, 'validation_failed', '内置模版不能删除');
      const i = db.vizTemplates.findIndex((t) => t.id === id);
      if (i < 0) return err(404, 'not_found', '没有这个模版');
      db.vizTemplates.splice(i, 1);
      return new HttpResponse(null, { status: 204 });
    }),
  ),
];
