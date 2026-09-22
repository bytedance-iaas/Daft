// MSW handlers for every C4 operation the console uses. They run the mock world in db.ts so
// `npm run dev` is fully interactive, and the same handlers serve the tests. Response bodies are
// validated against the OpenAPI schemas in contract.test.ts; request bodies are validated by the
// hook the test setup installs (so the UI cannot send what the contract does not allow).
import { HttpResponse, http } from 'msw';
import type {
  AdjudicationCard,
  Credential,
  DatasetCheck,
  DatasetDetail,
  DecisionInput,
  InputRef,
  ModuleChoice,
  Overview,
  PreflightResult,
  ReasoningLevel,
  Report,
  SourceChange,
  Subtask,
  Task,
  TaskCreate,
  TaskPatch,
  TaskState,
  TimelineEntry,
  UsageRow,
  VlmBackend,
  VlmModel,
} from '../api/types';
import {
  EMBODIMENTS,
  MAIN_TASK,
  PUBLIC_BUCKET,
  ZERO_USAGE,
  DATASET_PROFILES,
  episodePreviews,
  episodeView,
  mainPerf,
  mainPlan,
  mainReport,
  mainUsage,
  preflightFor,
  profileFor,
  registry,
  SO101_SKIPPED,
  SO101_TASK,
  tableRows,
} from './world';
import { cardsOf, clock, countsOf, datasetName, db, decisionsOf, findTask, nextId, reviewCatalog, toListItem } from './db';

// ------------------------------------------------------------------ plumbing

type Validator = (operationId: string, body: unknown) => void;
let requestValidator: Validator | null = null;

/** Tests install a validator that checks request bodies against the contract. */
export function setRequestValidator(v: Validator | null): void {
  requestValidator = v;
}

const API = '*/api/v1';

function err(status: number, code: string, message: string, details?: Record<string, unknown>) {
  return HttpResponse.json({ error: details ? { code, message, details } : { code, message } }, { status });
}

async function body<T>(request: Request, operationId: string): Promise<T> {
  const text = await request.text();
  const parsed = text ? (JSON.parse(text) as T) : ({} as T);
  requestValidator?.(operationId, parsed);
  return parsed;
}

/** Idempotency-Key (doc 03 §8): the same key returns the first response. */
async function idempotent(request: Request, run: () => Promise<Response> | Response): Promise<Response> {
  const key = request.headers.get('Idempotency-Key');
  if (key && db.idempotency.has(key)) {
    const first = db.idempotency.get(key)!;
    return HttpResponse.json(first.body as never, { status: first.status });
  }
  const res = await run();
  if (key) {
    const clone = res.clone();
    const text = await clone.text();
    db.idempotency.set(key, { status: res.status, body: text ? JSON.parse(text) : null });
  }
  return res;
}

function page<T>(items: T[], url: URL): { items: T[]; page: number; page_size: number; total: number } {
  const size = Number(url.searchParams.get('page_size') ?? 20);
  const pageNo = Math.max(1, Number(url.searchParams.get('page') ?? 1));
  const start = (pageNo - 1) * size;
  return { items: items.slice(start, start + size), page: pageNo, page_size: size, total: items.length };
}

function encodeCursor(obj: unknown): string {
  return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));
}

function decodeCursor<T>(c: string | null): T | null {
  if (!c) return null;
  try {
    return JSON.parse(decodeURIComponent(escape(atob(c)))) as T;
  } catch {
    return null;
  }
}

function cursorPage<T>(items: T[], url: URL, defLimit = 50): { items: T[]; next_cursor: string | null; has_more: boolean } {
  const limit = Math.min(Number(url.searchParams.get('limit') ?? defLimit), 500);
  const offset = decodeCursor<{ o: number }>(url.searchParams.get('cursor'))?.o ?? 0;
  const slice = items.slice(offset, offset + limit);
  const more = offset + limit < items.length;
  return { items: slice, next_cursor: more ? encodeCursor({ o: offset + limit }) : null, has_more: more };
}

const NON_TERMINAL: TaskState[] = ['created', 'queued', 'running', 'pausing', 'paused', 'stopping'];
const TERMINAL: TaskState[] = ['stopped', 'succeeded', 'completed_with_errors', 'failed'];

function touch(t: Task): Task {
  t.updated_at = Math.max(clock(), t.updated_at + 1);
  return t;
}

function timeline(taskId: string): TimelineEntry[] {
  if (!db.timelines.has(taskId)) {
    const t = findTask(taskId);
    const entries: TimelineEntry[] = [];
    if (t) {
      entries.push({ at: t.created_at, kind: 'created', text: t.state === 'created' ? '保存为待启动' : '创建并开始', state: 'created', subtask_id: null, revision: null });
      if (t.started_at) entries.push({ at: t.started_at, kind: 'started', text: '开始前检查通过，开始运行', state: 'running', subtask_id: null, revision: null });
      if (t.pause_reason === 'system') entries.push({ at: t.updated_at, kind: 'system_pause', text: 'Daemon 重启，任务在 episode 边界暂停，新进程起来后自动续跑', state: 'paused', subtask_id: null, revision: null });
      if (t.pause_reason === 'user') entries.push({ at: t.updated_at, kind: 'user_pause', text: '用户暂停', state: 'paused', subtask_id: null, revision: null });
      if (t.state === 'failed') entries.push({ at: t.finished_at ?? t.updated_at, kind: 'failed', text: t.state_reason ?? '任务失败', state: 'failed', subtask_id: null, revision: null });
      if (t.state === 'stopped') entries.push({ at: t.finished_at ?? t.updated_at, kind: 'stopped', text: '用户停止', state: 'stopped', subtask_id: null, revision: null });
      if (t.state === 'succeeded' || t.state === 'completed_with_errors') {
        entries.push({ at: t.finished_at ?? t.updated_at, kind: 'finished', text: '主流程结束', state: t.state, subtask_id: null, revision: t.result_rev || null });
        for (let r = 1; r <= t.result_rev; r += 1) entries.push({ at: (t.finished_at ?? t.updated_at) + r, kind: 'revision', text: `结果版本 r${String(r).padStart(4, '0')}`, state: null, subtask_id: null, revision: r });
      }
    }
    db.timelines.set(taskId, entries);
  }
  return db.timelines.get(taskId)!;
}

// ------------------------------------------------------------------ credentials

function verifyCredential(c: Credential, akid: string | undefined, testBucket: string | undefined): void {
  c.last_verified_at = clock();
  if (akid?.toLowerCase().startsWith('bad')) {
    c.verify_state = 'failed';
    c.last_verify_error = 'SignatureDoesNotMatch：Secret Access Key 不对';
  } else if (akid?.toLowerCase().startsWith('nolist') && !testBucket) {
    c.verify_state = 'unverified';
    c.last_verify_error = '这对密钥没有列存储桶的权限，也没填测试用存储桶';
    c.last_verified_at = null;
  } else {
    c.verify_state = 'ok';
    c.last_verify_error = null;
  }
}

const credentials = [
  http.get(`${API}/credentials`, () => HttpResponse.json({ items: db.credentials })),
  http.post(`${API}/credentials`, async ({ request }) => {
    const b = await body<{ name: string; access_key_id: string; secret_access_key: string; region: string; endpoint?: string; test_bucket?: string }>(request, 'createCredential');
    if (!b.name || !b.access_key_id || !b.secret_access_key || !b.region) return err(400, 'validation_failed', '名称、地域、Access Key ID、Secret Access Key 都要填');
    if (db.credentials.some((c) => c.name === b.name)) return err(409, 'name_taken', `名称「${b.name}」已被占用，换一个`);
    const c: Credential = {
      id: nextId('cred'),
      name: b.name,
      kind: 'tos',
      meta: { region: b.region, access_key_id_hint: b.access_key_id.slice(-4), ...(b.endpoint ? { endpoint: b.endpoint } : {}), ...(b.test_bucket ? { test_bucket: b.test_bucket } : {}) },
      verify_state: 'unverified',
      last_verified_at: null,
      last_verify_error: null,
      references: { active_tasks: 0, historical_tasks: 0 },
      created_at: clock(),
      updated_at: clock(),
    };
    verifyCredential(c, b.access_key_id, b.test_bucket);
    db.credentials.push(c);
    return HttpResponse.json(c, { status: 201 });
  }),
  http.put(`${API}/credentials/:id`, async ({ request, params }) => {
    const c = db.credentials.find((x) => x.id === params.id);
    if (!c) return err(404, 'not_found', '访问密钥不存在');
    const b = await body<{ name?: string; access_key_id?: string; secret_access_key?: string; region?: string; endpoint?: string; test_bucket?: string }>(request, 'updateCredential');
    if (b.name && b.name !== c.name && db.credentials.some((x) => x.name === b.name)) return err(409, 'name_taken', `名称「${b.name}」已被占用`);
    if (b.name) c.name = b.name;
    if (b.region) c.meta.region = b.region;
    if (b.test_bucket !== undefined) c.meta.test_bucket = b.test_bucket || undefined;
    if (b.access_key_id) c.meta.access_key_id_hint = b.access_key_id.slice(-4);
    if (!c.meta.test_bucket) delete c.meta.test_bucket;
    verifyCredential(c, b.access_key_id || (c.verify_state === 'failed' ? 'bad' : 'ok'), b.test_bucket ?? c.meta.test_bucket);
    c.updated_at = clock();
    return HttpResponse.json(c);
  }),
  http.delete(`${API}/credentials/:id`, ({ request, params }) => {
    const c = db.credentials.find((x) => x.id === params.id);
    if (!c) return err(404, 'not_found', '访问密钥不存在');
    const active = c.references?.active_tasks ?? 0;
    const hist = c.references?.historical_tasks ?? 0;
    if (active > 0) return err(409, 'credential_in_use', `访问密钥「${c.name}」正被 ${active} 个未结束的任务使用，不能删除`, { active_tasks: active });
    const confirm = new URL(request.url).searchParams.get('confirm') === 'true';
    if (hist > 0 && !confirm) return err(409, 'credential_in_use', `访问密钥「${c.name}」被 ${hist} 个已结束的任务引用，删除要确认`, { historical_tasks: hist, confirm_required: true });
    db.credentials = db.credentials.filter((x) => x !== c);
    return new HttpResponse(null, { status: 204 });
  }),
  http.post(`${API}/credentials/:id/verify`, ({ params }) => {
    const c = db.credentials.find((x) => x.id === params.id);
    if (!c) return err(404, 'not_found', '访问密钥不存在');
    verifyCredential(c, c.verify_state === 'failed' ? 'bad' : c.verify_state === 'unverified' && !c.meta.test_bucket ? 'nolist' : 'ok', c.meta.test_bucket);
    return HttpResponse.json({ verify_state: c.verify_state, last_verified_at: c.last_verified_at ?? clock(), error: c.last_verify_error ?? null });
  }),
];

// ------------------------------------------------------------------ VLM backends

const DOUBAO: ReasoningLevel[] = ['minimal', 'low', 'medium', 'high'];
const ALL7: ReasoningLevel[] = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];

function levelsFor(model: string): ReasoningLevel[] {
  return model.startsWith('doubao-seed') ? DOUBAO : ALL7;
}

function listModels(b: Pick<VlmBackend, 'endpoint' | 'kind'>): { listed: boolean; models: VlmModel[] } {
  if (b.endpoint.includes('unreachable') || b.endpoint.includes('nolist')) return { listed: false, models: [] };
  if (b.kind === 'ark') {
    return {
      listed: true,
      models: ['doubao-seed-2-0-pro-260215', 'doubao-seed-2-0-lite-260215', 'doubao-seed-1-6-251015'].map((name) => ({
        id: nextId('vm'),
        model_name: name,
        reasoning_effort: null,
        max_concurrency: null,
        capabilities: { vision: true, reasoning_effort_levels: levelsFor(name) },
        source: 'listed' as const,
      })),
    };
  }
  return { listed: true, models: [{ id: nextId('vm'), model_name: 'Qwen2.5-VL-72B-Instruct', reasoning_effort: null, max_concurrency: null, capabilities: { vision: true, reasoning_effort_levels: ALL7 }, source: 'listed' }] };
}

function backendInUse(b: VlmBackend, model?: string): number {
  return db.tasks.filter((t) => !t.deleted_at && NON_TERMINAL.includes(t.state) && t.vlm?.backend === b.name && (!model || t.vlm.model === model)).length;
}

function backendHistorical(b: VlmBackend): number {
  return db.tasks.filter((t) => TERMINAL.includes(t.state) && t.vlm?.backend === b.name).length;
}

const backends = [
  http.get(`${API}/vlm-backends`, () => HttpResponse.json({ items: db.backends })),
  http.post(`${API}/vlm-backends`, async ({ request }) => {
    const b = await body<{ name: string; kind: 'ark' | 'custom'; endpoint: string; api_key?: string; max_concurrency?: number }>(request, 'createVlmBackend');
    if (b.kind === 'ark' && !b.api_key) return err(400, 'validation_failed', '方舟后端要填 API Key');
    if (db.backends.some((x) => x.name === b.name)) return err(409, 'name_taken', `名称「${b.name}」已被占用，换一个`);
    const listing = listModels(b);
    const vb: VlmBackend = {
      id: nextId('vb'),
      name: b.name,
      kind: b.kind,
      endpoint: b.endpoint,
      max_concurrency: b.max_concurrency ?? 64,
      has_api_key: Boolean(b.api_key),
      verify_state: b.endpoint.includes('unreachable') ? 'failed' : 'ok',
      last_verified_at: clock(),
      last_verify_error: b.endpoint.includes('unreachable') ? '连接超时：10 秒内没有响应' : null,
      models_listed: listing.listed,
      models: listing.models,
      created_at: clock(),
      updated_at: clock(),
    };
    db.backends.push(vb);
    return HttpResponse.json(vb, { status: 201 });
  }),
  http.put(`${API}/vlm-backends/:id`, async ({ request, params }) => {
    const vb = db.backends.find((x) => x.id === params.id);
    if (!vb) return err(404, 'not_found', 'VLM 后端不存在');
    const b = await body<{ name?: string; endpoint?: string; api_key?: string; max_concurrency?: number }>(request, 'updateVlmBackend');
    if (b.name && b.name !== vb.name && db.backends.some((x) => x.name === b.name)) return err(409, 'name_taken', `名称「${b.name}」已被占用`);
    if (b.name) vb.name = b.name;
    if (b.endpoint) vb.endpoint = b.endpoint;
    if (b.api_key) vb.has_api_key = true;
    if (b.max_concurrency) vb.max_concurrency = b.max_concurrency;
    vb.updated_at = clock();
    return HttpResponse.json(vb);
  }),
  http.delete(`${API}/vlm-backends/:id`, ({ request, params }) => {
    const vb = db.backends.find((x) => x.id === params.id);
    if (!vb) return err(404, 'not_found', 'VLM 后端不存在');
    // W8: an unfinished task is a plain 409; finished ones only need confirm=true.
    const n = backendInUse(vb);
    if (n) return err(409, 'backend_in_use', `VLM 后端「${vb.name}」正被 ${n} 个未结束的任务使用，不能删除`);
    const hist = backendHistorical(vb);
    const confirm = new URL(request.url).searchParams.get('confirm') === 'true';
    if (hist && !confirm) return err(409, 'backend_in_use', `VLM 后端「${vb.name}」被 ${hist} 个已结束的任务引用，删除要确认`, { historical_tasks: hist, confirm_required: true });
    db.backends = db.backends.filter((x) => x !== vb);
    return new HttpResponse(null, { status: 204 });
  }),
  http.post(`${API}/vlm-backends/:id/verify`, ({ params }) => {
    const vb = db.backends.find((x) => x.id === params.id);
    if (!vb) return err(404, 'not_found', 'VLM 后端不存在');
    vb.last_verified_at = clock();
    return HttpResponse.json({ verify_state: vb.verify_state, last_verified_at: vb.last_verified_at, error: vb.last_verify_error ?? null });
  }),
  http.post(`${API}/vlm-backends/:id/refresh-models`, ({ params }) => {
    const vb = db.backends.find((x) => x.id === params.id);
    if (!vb) return err(404, 'not_found', 'VLM 后端不存在');
    const listing = listModels(vb);
    if (listing.listed) {
      const manual = vb.models.filter((m) => m.source === 'manual');
      const kept = listing.models.map((m) => vb.models.find((x) => x.model_name === m.model_name) ?? m);
      vb.models = [...kept, ...manual];
    }
    vb.models_listed = listing.listed;
    return HttpResponse.json({ listed: listing.listed, models: vb.models, ...(listing.listed ? {} : { note: 'GET /models 返回 404，请手填 Model ID 或推理接入点 ID' }) });
  }),
  http.post(`${API}/vlm-backends/:id/models`, async ({ request, params }) => {
    const vb = db.backends.find((x) => x.id === params.id);
    if (!vb) return err(404, 'not_found', 'VLM 后端不存在');
    const b = await body<{ model_name: string; reasoning_effort?: ReasoningLevel | null; max_concurrency?: number | null }>(request, 'addVlmModel');
    const name = b.model_name.trim();
    if (name.toLowerCase().startsWith('bad') || vb.endpoint.includes('unreachable')) {
      return err(422, 'model_check_failed', `用「${name}」发了一次最小请求，没调通：InvalidEndpointOrModel.NotFound`);
    }
    if (vb.models.some((m) => m.model_name === name)) return err(409, 'name_taken', `模型「${name}」已经在列表里了`);
    const m: VlmModel = { id: nextId('vm'), model_name: name, reasoning_effort: b.reasoning_effort ?? null, max_concurrency: b.max_concurrency ?? null, capabilities: { vision: null, reasoning_effort_levels: levelsFor(name) }, source: 'manual' };
    vb.models.push(m);
    return HttpResponse.json(m, { status: 201 });
  }),
  http.patch(`${API}/vlm-backends/:id/models/:modelId`, async ({ request, params }) => {
    const vb = db.backends.find((x) => x.id === params.id);
    const m = vb?.models.find((x) => x.id === params.modelId);
    if (!vb || !m) return err(404, 'not_found', '模型不存在');
    const b = await body<{ reasoning_effort?: ReasoningLevel | null; max_concurrency?: number | null }>(request, 'updateVlmModel');
    if ('reasoning_effort' in b) m.reasoning_effort = b.reasoning_effort ?? null;
    if ('max_concurrency' in b) m.max_concurrency = b.max_concurrency ?? null;
    return HttpResponse.json(m);
  }),
  http.delete(`${API}/vlm-backends/:id/models/:modelId`, ({ params }) => {
    const vb = db.backends.find((x) => x.id === params.id);
    const m = vb?.models.find((x) => x.id === params.modelId);
    if (!vb || !m) return err(404, 'not_found', '模型不存在');
    if (backendInUse(vb, m.model_name)) return err(409, 'backend_in_use', `模型「${m.model_name}」正被未结束的任务使用，不能移除`);
    vb.models = vb.models.filter((x) => x !== m);
    return new HttpResponse(null, { status: 204 });
  }),
];

// ------------------------------------------------------------------ datasets and preflight

function resolveInput(input: { dataset_id?: string } & Partial<InputRef>): { ref: InputRef; dataset?: DatasetDetail } | Response {
  if (input.dataset_id) {
    const d = db.datasets.find((x) => x.id === input.dataset_id);
    if (!d) return err(404, 'not_found', '数据集登记不存在，可能已被删除');
    return { ref: { source: d.source, uri: d.uri, ...(d.region ? { region: d.region } : {}), ...(d.credential ? { credential: d.credential } : {}) }, dataset: d };
  }
  if (!input.source || !input.uri) return err(400, 'validation_failed', '要给数据来源和地址，或者给已登记的数据集');
  if (input.source === 'tos' && !input.credential) return err(400, 'validation_failed', '私有 TOS 要选访问密钥');
  return { ref: input as InputRef };
}

function readCheck(ref: InputRef): Response | null {
  if (/nope|missing/.test(ref.uri)) return err(404, 'not_found', `读不到 ${ref.uri.replace(/\/+$/, '')}/meta/info.json：对象不存在。检查地址和地域`);
  if (ref.credential === 'old-ci') return err(400, 'validation_failed', '用访问密钥 old-ci 读 meta/info.json 被拒绝：SignatureDoesNotMatch（Secret Access Key 不对）');
  if (ref.source === 'tos' && ref.region && ref.region !== 'cn-beijing' && ref.uri.includes('pai-kit-datasets')) {
    return err(400, 'validation_failed', `存储桶 pai-kit-datasets 不在 ${ref.region}，它在 cn-beijing —— 把地域改成 cn-beijing 再试`);
  }
  return null;
}

function formatOf(r: PreflightResult): DatasetDetail['format'] {
  if (!r.format.supported) return 'unsupported';
  return r.format.version === 'v3' ? 'lerobot_v3' : 'lerobot_v2';
}

function datasetKey(ref: InputRef): string {
  return `${ref.source}|${ref.uri.replace(/\/+$/, '').toLowerCase()}|${ref.region ?? ''}`;
}

const DROID200_CHANGE: SourceChange = { meta_changed: true, added: 12, removed: 0, modified: 1, sample_keys: ['data/chunk-000/episode_000200.parquet', 'meta/info.json'], preflighted_at: 0 };

const datasets = [
  // Tests may add review lines to the catalog (D43); the mock world itself serves the contract file.
  http.get(`${API}/modules`, () => HttpResponse.json({ ...registry, review_lines: reviewCatalog() })),
  http.get(`${API}/overview`, () => HttpResponse.json(overview())),
  http.get(`${API}/datasets`, ({ request }) => {
    const url = new URL(request.url);
    const q = (url.searchParams.get('q') ?? '').toLowerCase();
    const format = url.searchParams.get('format');
    const check = url.searchParams.get('check_state');
    const items = db.datasets
      .filter((d) => !q || d.name.toLowerCase().includes(q) || d.uri.toLowerCase().includes(q))
      .filter((d) => !format || d.format === format)
      .filter((d) => !check || d.check_state === check)
      .sort((a, b) => b.created_at - a.created_at)
      .map(toDatasetItem);
    return HttpResponse.json(page(items, url));
  }),
  http.post(`${API}/datasets`, async ({ request }) =>
    idempotent(request, async () => {
      const b = await body<{ input: InputRef; name?: string; note?: string }>(request, 'createDataset');
      const r = resolveInput(b.input);
      if (r instanceof Response) return r;
      const bad = readCheck(r.ref);
      if (bad) return bad;
      const existing = db.datasets.find((d) => datasetKey({ source: d.source, uri: d.uri, region: d.region ?? undefined, credential: d.credential ?? undefined }) === datasetKey(r.ref));
      if (existing) return HttpResponse.json(existing, { status: 200 });
      const p = profileFor(r.ref.uri);
      const result = preflightFor(p, {});
      const now = clock();
      const d: DatasetDetail = {
        id: nextId('ds'),
        name: b.name || r.ref.uri.replace(/\/+$/, '').split('/').pop() || 'dataset',
        source: r.ref.source,
        uri: r.ref.uri.replace(/\/+$/, ''),
        region: r.ref.region ?? null,
        format: formatOf(result),
        episode_count: result.dataset?.episode_count ?? null,
        robot_type: result.dataset?.robot_type ?? null,
        check_state: 'ok',
        checked_at: null,
        preflighted_at: now,
        created_at: now,
        last_task: null,
        note: b.note ?? null,
        credential: r.ref.source === 'public' ? null : r.ref.credential ?? null,
        preflight: result,
        meta_fingerprint: result.meta_fingerprint,
        listing: { objects: (result.dataset?.episode_count ?? 0) * 2 + 4, bytes: (result.dataset?.episode_count ?? 0) * 30_000_000, digest: result.meta_fingerprint },
        checks: [{ at: now, trigger: 'add', result: 'same', change: null }],
        tasks: [],
        links: [],
      };
      db.datasets.push(d);
      return HttpResponse.json(d, { status: 201 });
    }),
  ),
  http.get(`${API}/datasets/browse`, ({ request }) => {
    const url = new URL(request.url);
    const source = url.searchParams.get('source');
    if (source === 'public') {
      const items = DATASET_PROFILES.filter((p) => p.source === 'public').map((p) => ({ name: p.name, uri: p.uri, format_hint: (p.format.version === 'v3' ? 'lerobot_v3' : 'lerobot_v2') as 'lerobot_v2' | 'lerobot_v3', episodes: p.episodes }));
      items.push({ name: 'aloha_sim_insertion_human', uri: `tos://${PUBLIC_BUCKET}/lerobot/aloha_sim_insertion_human`, format_hint: 'lerobot_v2', episodes: 50 });
      return HttpResponse.json(cursorPage(items, url));
    }
    const prefix = (url.searchParams.get('uri') ?? '').replace(/\/+$/, '');
    if (!prefix.startsWith('tos://')) return err(400, 'validation_failed', '要给 tos:// 前缀');
    if (!url.searchParams.get('credential')) return err(400, 'validation_failed', '私有 TOS 要选访问密钥');
    const items = DATASET_PROFILES.filter((p) => p.source === 'tos' && p.uri.startsWith(`${prefix}/`)).map((p) => ({ name: p.name, uri: p.uri, format_hint: (p.format.supported ? (p.format.version === 'v3' ? 'lerobot_v3' : 'lerobot_v2') : 'unknown') as 'lerobot_v2' | 'lerobot_v3' | 'unknown', episodes: p.format.supported ? p.episodes : null }));
    return HttpResponse.json(cursorPage(items, url));
  }),
  http.get(`${API}/datasets/episodes`, ({ request }) => {
    const url = new URL(request.url);
    const id = url.searchParams.get('dataset_id');
    let uri = url.searchParams.get('uri') ?? '';
    if (id) {
      const d = db.datasets.find((x) => x.id === id);
      if (!d) return err(404, 'not_found', '数据集登记不存在');
      uri = d.uri;
    }
    if (!uri) return err(400, 'validation_failed', '要给 dataset_id，或者来源和地址');
    const p = profileFor(uri);
    if (!p.format.supported) return err(400, 'validation_failed', '这个数据集的格式不支持，没有 episode 可预览');
    return HttpResponse.json(cursorPage(episodePreviews(uri, p.episodes), url, 48));
  }),
  http.get(`${API}/datasets/:id`, ({ params }) => {
    const d = db.datasets.find((x) => x.id === params.id);
    return d ? HttpResponse.json(d) : err(404, 'not_found', '数据集登记不存在，可能已被删除');
  }),
  http.patch(`${API}/datasets/:id`, async ({ request, params }) => {
    const d = db.datasets.find((x) => x.id === params.id);
    if (!d) return err(404, 'not_found', '数据集登记不存在');
    const b = await body<{ name?: string; note?: string | null }>(request, 'updateDataset');
    if (b.name) d.name = b.name;
    if ('note' in b) d.note = b.note ?? null;
    return HttpResponse.json(d);
  }),
  http.delete(`${API}/datasets/:id`, ({ params }) => {
    const d = db.datasets.find((x) => x.id === params.id);
    if (!d) return err(404, 'not_found', '数据集登记不存在');
    const busy = db.tasks.filter((t) => t.dataset_id === d.id && !t.deleted_at && NON_TERMINAL.includes(t.state));
    if (busy.length) return err(409, 'dataset_in_use', `还有 ${busy.length} 个未结束的任务在用这个数据集，等它们结束后再删`, { tasks: busy.map((t) => t.id) });
    db.datasets = db.datasets.filter((x) => x !== d);
    return new HttpResponse(null, { status: 204 });
  }),
  http.post(`${API}/datasets/:id/recheck`, ({ params }) => {
    const d = db.datasets.find((x) => x.id === params.id);
    if (!d) return err(404, 'not_found', '数据集登记不存在');
    const changed = d.id === 'ds_droid200' || d.check_state === 'changed';
    const check: DatasetCheck = { at: clock(), trigger: 'recheck', result: changed ? 'changed' : 'same', change: changed ? { ...DROID200_CHANGE, preflighted_at: d.preflighted_at } : null };
    d.checks = [check, ...d.checks].slice(0, 20);
    d.checked_at = check.at;
    d.check_state = changed ? 'changed' : 'ok';
    return HttpResponse.json(check);
  }),
  http.post(`${API}/datasets/:id/repreflight`, ({ params }) => {
    const d = db.datasets.find((x) => x.id === params.id);
    if (!d) return err(404, 'not_found', '数据集登记不存在');
    repreflightDataset(d);
    return HttpResponse.json(d);
  }),
  http.post(`${API}/preflight`, async ({ request }) => {
    const b = await body<{ input: { dataset_id?: string } & Partial<InputRef>; vlm_backend?: string; embodiment_id?: string }>(request, 'preflight');
    const r = resolveInput(b.input);
    if (r instanceof Response) return r;
    const bad = readCheck(r.ref);
    if (bad) return bad;
    if (b.vlm_backend && !db.backends.some((x) => x.name === b.vlm_backend)) return err(400, 'validation_failed', `VLM 后端「${b.vlm_backend}」不存在`);
    const result = preflightFor(profileFor(r.ref.uri), { vlmBackend: b.vlm_backend, embodiment: b.embodiment_id });
    const id = nextId('pf');
    const expiresAt = clock() + 30 * 60_000;
    db.preflights.set(id, { id, expiresAt, input: { ...r.ref, datasetId: r.dataset?.id }, result });
    return HttpResponse.json({ preflight_id: id, expires_at: expiresAt, result });
  }),
  http.post(`${API}/deliveries/probe`, async ({ request }) => {
    const b = await body<{ uri: string; region?: string; credential: string }>(request, 'probeDelivery');
    return HttpResponse.json(probe(b.uri, b.credential));
  }),
];

/**
 * The delivery write probe as the W8 Daemon answers it: error.code is one of forbidden,
 * not_found, auth_failed, unreachable, server_error, failed; `leftover` comes with ok: true
 * (written, but the probe object could not be removed).
 */
function probe(uri: string, credential: string): { ok: boolean; error?: { code: string; message: string } } {
  const bucket = uri.replace(/^tos:\/\//, '').split('/')[0];
  if (bucket === PUBLIC_BUCKET) return { ok: false, error: { code: 'forbidden', message: 'HuggingFace 缓存桶是只读的，不能当交付目录' } };
  if (credential === 'readonly-tos' || bucket === 'pai-kit-datasets') {
    return { ok: false, error: { code: 'forbidden', message: `存储桶 ${bucket} 对访问密钥 ${credential} 只读，写不进去 —— 换一个可写的存储桶或访问密钥` } };
  }
  if (credential === 'old-ci') return { ok: false, error: { code: 'auth_failed', message: '访问密钥 old-ci 签名不对（SignatureDoesNotMatch）' } };
  if (!db.credentials.some((c) => c.name === credential)) return { ok: false, error: { code: 'failed', message: `访问密钥「${credential}」不存在` } };
  if (bucket.includes('nosuch')) return { ok: false, error: { code: 'not_found', message: `存储桶 ${bucket} 不存在（NoSuchBucket）` } };
  if (bucket.includes('offline')) return { ok: false, error: { code: 'unreachable', message: '连不上 TOS 端点：10 秒内没有响应' } };
  if (bucket.includes('scratch')) return { ok: true, error: { code: 'leftover', message: `探针对象 ${uri.replace(/\/+$/, '')}/.curator-probe 已写入，但没能删掉（没有 DeleteObject 权限），请手动清理` } };
  return { ok: true };
}

function toDatasetItem(d: DatasetDetail) {
  return {
    id: d.id,
    name: d.name,
    source: d.source,
    uri: d.uri,
    region: d.region,
    format: d.format,
    episode_count: d.episode_count,
    robot_type: d.robot_type,
    check_state: d.check_state,
    checked_at: d.checked_at,
    preflighted_at: d.preflighted_at,
    created_at: d.created_at,
    last_task: d.last_task,
  };
}

function repreflightDataset(d: DatasetDetail): void {
  const now = clock();
  d.check_state = 'ok';
  d.preflighted_at = now;
  d.checked_at = now;
  d.checks = [{ at: now, trigger: 'repreflight', result: 'same', change: null } as DatasetCheck, ...d.checks].slice(0, 20);
  if (d.id === 'ds_droid200' && d.episode_count) d.episode_count = 212;
}

// ------------------------------------------------------------------ overview

function overview(): Overview {
  const live = db.tasks.filter((t) => !t.deleted_at);
  const now = clock();
  const finished = live.filter((t) => TERMINAL.includes(t.state) && (t.finished_at ?? 0) > now - 7 * 86_400_000);
  const withSummary = finished.filter((t) => t.summary && t.summary.pass_rate !== null);
  const days: Overview['recent']['tokens_per_day'] = [];
  for (let i = 6; i >= 0; i -= 1) {
    const d = new Date(now - i * 86_400_000);
    const date = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const tokens = live.filter((t) => Math.floor((now - t.created_at) / 86_400_000) === i).reduce((a, t) => a + t.usage.prompt_tokens + t.usage.completion_tokens, 0);
    days.push({ date, tokens });
  }
  return {
    todo: {
      error_tasks: live.filter((t) => t.state === 'completed_with_errors').length,
      adjudication: { tasks: live.filter((t) => t.pending_adjudication > 0).length, episodes: live.reduce((a, t) => a + t.pending_adjudication, 0) },
      delivery_pending: live.filter((t) => t.delivery_stale).length,
      datasets_changed: db.datasets.filter((d) => d.check_state === 'changed').length,
      credentials_failed: db.credentials.filter((c) => c.verify_state === 'failed').length,
      backends_failed: db.backends.filter((b) => b.verify_state === 'failed').length,
    },
    running: {
      running: live.filter((t) => t.state === 'running' || t.state === 'pausing' || t.state === 'stopping').length,
      queued: live.filter((t) => t.state === 'queued').length,
      paused: live.filter((t) => t.state === 'paused').length,
      active: live
        .filter((t) => t.state === 'running')
        .map((t) => {
          const cur = [...t.progress.stages].reverse().find((s) => s.state === 'running') ?? t.progress.stages[t.progress.stages.length - 1];
          return { task: { id: t.id, name: t.name, state: t.state, created_at: t.created_at }, stage: cur?.id ?? null, done: cur?.done ?? 0, total: cur?.total ?? 0 };
        }),
    },
    recent: {
      days: 7,
      tasks_finished: finished.length,
      episodes_checked: finished.reduce((a, t) => a + (t.summary?.total ?? 0), 0),
      pass_rate: withSummary.length ? withSummary.reduce((a, t) => a + (t.summary!.pass_rate ?? 0), 0) / withSummary.length : null,
      tokens_per_day: days,
    },
    datasets: { total: db.datasets.length, changed: db.datasets.filter((d) => d.check_state === 'changed').length },
    generated_at: now,
  };
}

// ------------------------------------------------------------------ tasks

function moduleIds(choices: ModuleChoice[]): string[] {
  return choices.map((c) => (typeof c === 'string' ? c : c.id));
}

type StartFailure = Response | null;

interface PrecheckResult {
  id: 'input' | 'output' | 'vlm';
  ok: boolean;
  code?: string;
  reason?: string;
  target: string;
  elapsed_ms: number;
}

/** The three pre-start checks (D30) and the fingerprint check (D37); details.checks as W8 sends it. */
function startChecks(t: Task): StartFailure {
  const checks: PrecheckResult[] = [];
  const inBad = t.input.credential === 'old-ci' || /nope|missing/.test(t.input.uri);
  checks.push(
    inBad
      ? { id: 'input', ok: false, code: 'auth_failed', reason: `用访问密钥 ${t.input.credential ?? '（匿名）'} 读不到 meta/info.json`, target: `${t.input.uri}/meta/info.json`, elapsed_ms: 212 }
      : { id: 'input', ok: true, target: `${t.input.uri}/meta/info.json`, elapsed_ms: 188 },
  );
  const p = probe(t.output.uri, t.output.credential ?? '');
  checks.push(
    p.ok
      ? { id: 'output', ok: true, ...(p.error ? { code: p.error.code, reason: p.error.message } : {}), target: t.output.uri, elapsed_ms: 305 }
      : { id: 'output', ok: false, code: p.error?.code ?? 'failed', reason: p.error?.message ?? '交付目录写不进去', target: t.output.uri, elapsed_ms: 290 },
  );
  if (t.vlm) {
    const vb = db.backends.find((b) => b.name === t.vlm!.backend);
    const bad = !vb || vb.verify_state === 'failed';
    const target = `${t.vlm.backend} / ${t.vlm.model}`;
    checks.push(
      bad
        ? { id: 'vlm', ok: false, code: vb ? 'unreachable' : 'not_found', reason: vb ? `用 ${vb.name} 的 ${t.vlm.model} 发最小请求失败：${vb.last_verify_error ?? '调不通'}` : `VLM 后端「${t.vlm.backend}」不存在`, target, elapsed_ms: 10_004 }
        : { id: 'vlm', ok: true, target, elapsed_ms: 1_320 },
    );
  }
  if (checks.some((c) => !c.ok)) {
    return err(422, 'precheck_failed', `开始前检查没过：${checks.filter((c) => !c.ok).map((c) => c.reason).join('；')}`, { checks });
  }
  const d = t.dataset_id ? db.datasets.find((x) => x.id === t.dataset_id) : undefined;
  if (d && d.check_state === 'changed') {
    const change = d.checks.find((c) => c.change)?.change ?? { ...DROID200_CHANGE, preflighted_at: d.preflighted_at };
    return err(409, 'source_changed', '数据集和添加时不一样了，确认后重新预检', change as unknown as Record<string, unknown>);
  }
  return null;
}

function startTask(t: Task): void {
  t.state = 'queued';
  t.pause_reason = null;
  t.started_at = clock();
  t.run_id = t.run_id ?? new Date(clock()).toISOString().replace(/[-:]/g, '').replace('T', '-').slice(0, 15);
  t.progress = { stages: [{ id: 'numeric', state: 'pending', done: 0, total: 0, elapsed_s: null, eta_s: null }] };
  timeline(t.id).push({ at: clock(), kind: 'started', text: '开始前检查通过，进入队列', state: 'queued', subtask_id: null, revision: null });
  touch(t);
}

function buildNewTask(req: TaskCreate): Task | Response {
  const r = resolveInput(req.input as { dataset_id?: string } & Partial<InputRef>);
  if (r instanceof Response) return r;
  const pf = db.preflights.get(req.preflight_id);
  if (!pf || pf.expiresAt < clock()) return err(409, 'preflight_expired', '预检结果已过期（30 分钟），请重新预检');
  const selected = moduleIds(req.modules);
  const unknown = selected.filter((id) => !registry.modules.some((m) => m.id === id));
  if (unknown.length) return err(400, 'validation_failed', `不认识的模块：${unknown.join(', ')}`);
  const unsupported = selected.filter((id) => pf.result.modules.find((m) => m.id === id)?.availability === 'unsupported');
  if (unsupported.length) return err(400, 'validation_failed', `这些模块在预检里不可用：${unsupported.join(', ')}`);
  if (selected.includes('kinematic_limits') && pf.result.modules.find((m) => m.id === 'kinematic_limits')?.availability === 'needs_input' && !req.embodiment_id) {
    return err(400, 'validation_failed', '运动学极限需要机器人型号：补一个型号，或者不选这个模块', { field: 'embodiment_id' });
  }
  if (req.embodiment_id && !EMBODIMENTS.includes(req.embodiment_id)) return err(400, 'validation_failed', `机器人型号 ${req.embodiment_id} 不在规格库`);
  const needsVlm = selected.some((id) => (registry.modules.find((m) => m.id === id)?.needs as string[]).includes('vlm'));
  if (needsVlm && !req.vlm) return err(400, 'validation_failed', '选了调用模型的模块，要选 VLM 后端和模型', { field: 'vlm' });
  if (req.episodes.mode === 'explicit' && !/^\s*\d+(\s*-\s*\d+)?(\s*,\s*\d+(\s*-\s*\d+)?)*\s*$/.test(req.episodes.expr)) {
    return err(400, 'validation_failed', `episode 表达式写得不对：${req.episodes.expr}（形如 3,10-12,34）`, { field: 'episodes' });
  }
  const count = pf.result.dataset?.episode_count ?? 0;
  if (req.episodes.mode === 'head' && req.episodes.n > count) return err(400, 'validation_failed', `只有 ${count} 条 episode，前 N 条最多填 ${count}`, { field: 'episodes' });
  const params = (m: string) => {
    const c = req.modules.find((x) => typeof x !== 'string' && x.id === m);
    return c && typeof c !== 'string' ? c.params : undefined;
  };
  for (const m of selected) {
    const schema = registry.modules.find((x) => x.id === m)!.param_schema as { properties?: Record<string, { oneOf?: { const: string }[] }> };
    const given = params(m) ?? {};
    for (const [k, v] of Object.entries(given)) {
      const prop = schema.properties?.[k];
      if (!prop) return err(400, 'validation_failed', `模块 ${m} 没有参数 ${k}`);
      if (prop.oneOf && !prop.oneOf.some((o) => o.const === v)) return err(400, 'validation_failed', `模块 ${m} 的参数 ${k} 取值不对`);
    }
  }
  const now = clock();
  const d = r.dataset ?? db.datasets.find((x) => datasetKey({ source: x.source, uri: x.uri, region: x.region ?? undefined }) === datasetKey(r.ref));
  const t: Task = {
    id: nextId('task'),
    name: req.name,
    note: req.note ?? null,
    state: 'created',
    state_reason: null,
    pause_reason: null,
    input: r.ref,
    dataset_id: d?.id ?? null,
    output: req.output,
    run_id: null,
    episodes: req.episodes,
    embodiment_id: req.embodiment_id ?? null,
    vlm: req.vlm ? { ...req.vlm } : null,
    params: { start_now: true, export: true, vlm_retry: 3, vlm_hedge: true, clips: false, ...(req.params ?? {}) },
    source: null,
    progress: { stages: [] },
    modules: registry.modules.map((m) => ({
      id: m.id,
      name: m.name_zh,
      selected: selected.includes(m.id),
      availability: pf.result.modules.find((x) => x.id === m.id)?.availability ?? 'available',
      unavailable_reason: null,
      state: selected.includes(m.id) ? 'pending' : 'skipped',
      episodes_total: 0,
      episodes_error: 0,
      elapsed_s: null,
      error: null,
    })),
    summary: null,
    result_rev: 0,
    usage: { ...ZERO_USAGE },
    pending_adjudication: 0,
    delivery_stale: false,
    active_subtask: null,
    created_at: now,
    updated_at: now,
    started_at: null,
    finished_at: null,
    deleted_at: null,
    links: [],
  };
  t.links = [{ rel: 'task', title: 'Open task', url: `/tasks/${t.id}`, absolute: false }];
  return t;
}

function created(t: Task, warnings: string[] = []) {
  return { id: t.id, state: t.state, created_at: t.created_at, warnings, links: t.links };
}

function outputWarning(t: Task): string[] {
  const other = db.tasks.find((x) => x !== t && !x.deleted_at && x.output.uri === t.output.uri && x.input.uri !== t.input.uri);
  return other ? [`交付目录 ${t.output.uri} 下已经有别的数据集跑出来的批次（${other.name}），这次会写进新的批次目录，不会覆盖`] : [];
}

function newSubtask(t: Task, kind: Subtask['kind'], scope: Subtask['scope']): Subtask {
  const s: Subtask = { id: nextId('sub'), task_id: t.id, kind, scope, state: 'queued', state_reason: null, progress: null, created_at: clock(), started_at: null, finished_at: null, result_rev: null };
  const list = db.subtasks.get(t.id) ?? [];
  list.push(s);
  db.subtasks.set(t.id, list);
  t.active_subtask = s;
  timeline(t.id).push({ at: clock(), kind: 'subtask_started', text: { retry: '重试', resume: '继续运行', apply_adjudication: '执行裁决', reexport: '重新导出' }[kind] + '：子任务已创建，排队中', state: null, subtask_id: s.id, revision: null });
  touch(t);
  return s;
}

const tasks = [
  http.get(`${API}/tasks`, ({ request }) => {
    const url = new URL(request.url);
    const state = url.searchParams.get('state');
    const q = (url.searchParams.get('q') ?? '').toLowerCase();
    const delivery = url.searchParams.get('delivery');
    const mods = (url.searchParams.get('module') ?? '').split(',').filter(Boolean);
    const datasetId = url.searchParams.get('dataset_id');
    const items = db.tasks
      .filter((t) => (state === 'deleted' ? Boolean(t.deleted_at) : !t.deleted_at && (!state || t.state === state)))
      .filter((t) => !q || t.name.toLowerCase().includes(q) || t.id.toLowerCase().includes(q))
      .filter((t) => !delivery || t.output.uri === delivery)
      .filter((t) => mods.every((m) => t.modules.some((x) => x.id === m && x.selected)))
      .filter((t) => !datasetId || t.dataset_id === datasetId)
      .sort((a, b) => b.created_at - a.created_at)
      .map(toListItem);
    return HttpResponse.json(page(items, url));
  }),
  http.post(`${API}/tasks`, async ({ request }) =>
    idempotent(request, async () => {
      const req = await body<TaskCreate>(request, 'createTask');
      const t = buildNewTask(req);
      if (t instanceof Response) return t;
      if (req.params?.start_now !== false) {
        const fail = startChecks(t);
        if (fail) return fail;
        db.tasks.push(t);
        startTask(t);
      } else {
        db.tasks.push(t);
      }
      return HttpResponse.json(created(t, outputWarning(t)), { status: 201 });
    }),
  ),
  http.post(`${API}/tasks/batch`, async ({ request }) =>
    idempotent(request, async () => {
      const req = await body<{ items: { name: string; input: TaskCreate['input']; output: TaskCreate['output']; preflight_id: string }[]; shared: Omit<TaskCreate, 'name' | 'input' | 'output' | 'preflight_id'> }>(request, 'createTasksBatch');
      const out: Task[] = [];
      for (const item of req.items) {
        const t = buildNewTask({ ...req.shared, ...item });
        if (t instanceof Response) return t;
        out.push(t);
      }
      const startNow = req.shared.params?.start_now !== false;
      for (const t of out) {
        db.tasks.push(t);
        if (startNow && !startChecks(t)) startTask(t);
      }
      return HttpResponse.json({ tasks: out.map((t) => created(t)) }, { status: 201 });
    }),
  ),
  http.get(`${API}/tasks/:id`, ({ params }) => {
    const t = findTask(String(params.id));
    return t ? HttpResponse.json(t) : err(404, 'not_found', '任务不存在，可能已被删除');
  }),
  http.patch(`${API}/tasks/:id`, async ({ request, params }) => {
    const t = findTask(String(params.id));
    if (!t) return err(404, 'not_found', '任务不存在');
    const ifMatch = request.headers.get('If-Match');
    if (!ifMatch) return err(400, 'validation_failed', '缺少 If-Match');
    if (ifMatch !== String(t.updated_at)) return err(412, 'precondition_failed', '任务在别处被改过了，已为你刷新，请再改一次');
    const patch = await body<TaskPatch>(request, 'updateTask');
    const keys = Object.keys(patch);
    if (t.state !== 'created' && keys.some((k) => k !== 'name' && k !== 'note')) {
      return err(409, 'task_state_conflict', '任务已经开始，只能改名称和备注；要换数据、模块、模型或参数，请复制为新任务', { state: t.state });
    }
    if (t.state === 'created' && (patch.input || patch.modules || patch.episodes || patch.preflight_id)) {
      const merged = { ...configOf(t), ...patch } as TaskCreate;
      const rebuilt = buildNewTask(merged);
      if (rebuilt instanceof Response) return rebuilt;
      Object.assign(t, { input: rebuilt.input, dataset_id: rebuilt.dataset_id, output: rebuilt.output, episodes: rebuilt.episodes, modules: rebuilt.modules, embodiment_id: rebuilt.embodiment_id, vlm: rebuilt.vlm, params: rebuilt.params });
    } else if (t.state === 'created') {
      if (patch.output) t.output = patch.output;
      if ('embodiment_id' in patch) t.embodiment_id = patch.embodiment_id ?? null;
      if (patch.vlm) t.vlm = { ...patch.vlm };
      if (patch.params) t.params = { ...t.params, ...patch.params };
    }
    if (patch.name) t.name = patch.name;
    if ('note' in patch) t.note = patch.note ?? null;
    return HttpResponse.json(touch(t));
  }),
  http.delete(`${API}/tasks/:id`, ({ params }) => {
    const t = findTask(String(params.id));
    if (!t || t.deleted_at) return err(404, 'not_found', '任务不存在');
    if (t.state !== 'created' && !TERMINAL.includes(t.state)) return err(409, 'task_state_conflict', '任务还没结束，请先停止再删除', { state: t.state });
    t.deleted_at = clock();
    return new HttpResponse(null, { status: 204 });
  }),
  http.post(`${API}/tasks/:id/restore`, ({ params }) => {
    const t = findTask(String(params.id));
    if (!t || !t.deleted_at) return err(404, 'not_found', '没有可恢复的任务记录');
    t.deleted_at = null;
    return HttpResponse.json(touch(t));
  }),
  http.post(`${API}/tasks/:id/purge-artifacts`, async ({ request, params }) => {
    const t = findTask(String(params.id));
    if (!t) return err(404, 'not_found', '任务不存在');
    const b = await body<{ confirm_path: string }>(request, 'purgeTaskArtifacts');
    if (!t.run_id) return err(409, 'task_state_conflict', '这个任务还没有写过交付产物');
    const path = `${t.output.uri.replace(/\/+$/, '')}/${t.run_id}/`;
    if (b.confirm_path !== path) return err(422, 'confirm_path_mismatch', `确认的路径和要清理的不一致，应为 ${path}`);
    return HttpResponse.json({ path, bytes: 212 * 1024 * 1024, latest_removed: false }, { status: 202 });
  }),
  http.post(`${API}/tasks/:id/rebind-credentials`, async ({ request, params }) => {
    const t = findTask(String(params.id));
    if (!t) return err(404, 'not_found', '任务不存在');
    const b = await body<{ input_credential?: string; output_credential?: string }>(request, 'rebindTaskCredentials');
    if (b.input_credential) t.input = { ...t.input, credential: b.input_credential };
    if (b.output_credential) t.output = { ...t.output, credential: b.output_credential };
    return HttpResponse.json(touch(t));
  }),
  http.post(`${API}/tasks/:id/actions/:action`, ({ request, params }) =>
    idempotent(request, () => {
      const t = findTask(String(params.id));
      if (!t) return err(404, 'not_found', '任务不存在');
      const action = String(params.action);
      const conflict = (msg: string) => err(409, 'task_state_conflict', msg, { state: t.state });
      switch (action) {
        case 'start': {
          if (t.state !== 'created') return conflict('只有待启动的任务可以启动');
          const fail = startChecks(t);
          if (fail) return fail;
          startTask(t);
          break;
        }
        case 'pause':
          if (t.state !== 'running' && t.state !== 'queued') return conflict('只有运行中的任务可以暂停');
          t.state = 'paused';
          t.pause_reason = 'user';
          timeline(t.id).push({ at: clock(), kind: 'user_pause', text: '用户暂停：在飞的 episode 跑完后停下', state: 'paused', subtask_id: null, revision: null });
          break;
        case 'resume':
          if (t.state !== 'paused') return conflict('只有已暂停的任务可以恢复');
          if (t.pause_reason === 'system') return conflict('系统暂停的任务会自动恢复，不需要手动恢复');
          t.state = 'queued';
          t.pause_reason = null;
          timeline(t.id).push({ at: clock(), kind: 'user_resume', text: '用户恢复，重新排队', state: 'queued', subtask_id: null, revision: null });
          break;
        case 'stop':
          if (!['queued', 'running', 'pausing', 'paused'].includes(t.state)) return conflict('这个状态的任务不能停止');
          t.state = 'stopped';
          t.pause_reason = null;
          t.finished_at = clock();
          timeline(t.id).push({ at: clock(), kind: 'stopped', text: '用户停止', state: 'stopped', subtask_id: null, revision: null });
          break;
        default:
          return err(404, 'not_found', `未知操作 ${action}`);
      }
      return HttpResponse.json(touch(t));
    }),
  ),
  http.post(`${API}/tasks/:id/repreflight`, ({ request, params }) =>
    idempotent(request, () => {
      const t = findTask(String(params.id));
      if (!t) return err(404, 'not_found', '任务不存在');
      if (t.state !== 'created') return err(409, 'task_state_conflict', '只有待启动的任务需要重新预检', { state: t.state });
      const d = t.dataset_id ? db.datasets.find((x) => x.id === t.dataset_id) : undefined;
      if (d) repreflightDataset(d);
      const count = d?.episode_count ?? 0;
      const incompatibilities: { field: string; module?: string; reason_code: string; reason: string }[] = [];
      if (t.episodes.mode === 'explicit') {
        const max = Math.max(...t.episodes.expr.split(/[,-]/).map((x) => Number(x.trim())).filter((x) => !Number.isNaN(x)));
        if (max >= count) incompatibilities.push({ field: 'episodes', reason_code: 'episodes_out_of_range', reason: `自选的 episode 超出了范围：数据集现在只有 ${count} 条（ep 0–${count - 1}）` });
      }
      if (t.episodes.mode === 'head' && t.episodes.n > count) incompatibilities.push({ field: 'episodes', reason_code: 'episodes_out_of_range', reason: `前 ${t.episodes.n} 条超出了范围：数据集现在只有 ${count} 条` });
      if (incompatibilities.length) return HttpResponse.json({ compatible: false, incompatibilities, task: touch(t) });
      startTask(t);
      return HttpResponse.json({ compatible: true, incompatibilities: [], task: t });
    }),
  ),
  http.post(`${API}/tasks/:id/retry`, async ({ request, params }) =>
    idempotent(request, async () => {
      const t = findTask(String(params.id));
      if (!t) return err(404, 'not_found', '任务不存在');
      const b = await body<{ modules?: string[] }>(request, 'retryTask');
      if (t.active_subtask) return err(409, 'subtask_active', '这个任务已有未结束的子任务，等它结束后再试');
      if (t.state !== 'completed_with_errors') return err(409, 'task_state_conflict', '只有「错误」状态的任务可以重试', { state: t.state });
      const mods = b.modules?.length ? b.modules : t.modules.filter((m) => m.selected && (m.episodes_error > 0 || m.state === 'failed')).map((m) => m.id);
      const s = newSubtask(t, 'retry', { modules: mods, episodes: 'errors' });
      return HttpResponse.json({ subtask: s, links: t.links }, { status: 202 });
    }),
  ),
  http.post(`${API}/tasks/:id/continue`, ({ request, params }) =>
    idempotent(request, () => {
      const t = findTask(String(params.id));
      if (!t) return err(404, 'not_found', '任务不存在');
      if (t.active_subtask) return err(409, 'subtask_active', '这个任务已有未结束的子任务');
      if (t.state !== 'stopped' && t.state !== 'failed') return err(409, 'task_state_conflict', '只有已停止或失败的任务可以继续运行', { state: t.state });
      if (t.state_reason?.includes('source_changed')) return err(409, 'task_state_conflict', '源数据中途变了的任务不能继续，请复制为新任务', { state: t.state });
      const s = newSubtask(t, 'resume', { episodes: 'all' });
      return HttpResponse.json({ subtask: s, links: t.links }, { status: 202 });
    }),
  ),
  http.post(`${API}/tasks/:id/reexport`, ({ request, params }) =>
    idempotent(request, () => {
      const t = findTask(String(params.id));
      if (!t) return err(404, 'not_found', '任务不存在');
      if (t.active_subtask) return err(409, 'subtask_active', '这个任务已有未结束的子任务');
      if (!TERMINAL.includes(t.state)) return err(409, 'task_state_conflict', '任务结束后才能导出', { state: t.state });
      const s = newSubtask(t, 'reexport', {});
      return HttpResponse.json({ subtask: s, links: t.links }, { status: 202 });
    }),
  ),
  http.get(`${API}/tasks/:id/subtasks`, ({ params }) => HttpResponse.json({ items: db.subtasks.get(String(params.id)) ?? [] })),
  http.get(`${API}/tasks/:id/timeline`, ({ params }) => {
    const t = findTask(String(params.id));
    return t ? HttpResponse.json({ items: timeline(t.id) }) : err(404, 'not_found', '任务不存在');
  }),
  http.get(`${API}/tasks/:id/plan`, ({ params }) => {
    const t = findTask(String(params.id));
    if (!t) return err(404, 'not_found', '任务不存在');
    if (!t.started_at) return err(404, 'not_found', '任务还没开始，没有执行计划');
    return HttpResponse.json(mainPlan());
  }),
  http.get(`${API}/tasks/:id/logs`, ({ request, params }) => {
    const t = findTask(String(params.id));
    if (!t) return err(404, 'not_found', '任务不存在');
    const url = new URL(request.url);
    const stageF = url.searchParams.get('stage');
    const subtask = url.searchParams.get('subtask');
    const level = url.searchParams.get('level');
    const rank = { error: 0, warn: 1, info: 2, debug: 3 } as const;
    const all = (db.logs.get(t.id) ?? genericLogs(t))
      .filter((l) => !stageF || l.stage === stageF)
      .filter((l) => subtask === null || (subtask === '' ? !l.subtask_id : l.subtask_id === subtask))
      .filter((l) => !level || rank[l.level] <= rank[level as keyof typeof rank]);
    // Newest page first; next_cursor walks back to older lines (see README «契约缺口»).
    const limit = Math.min(Number(url.searchParams.get('limit') ?? 200), 200);
    const end = decodeCursor<{ end: number }>(url.searchParams.get('cursor'))?.end ?? all.length;
    const start = Math.max(0, end - limit);
    return HttpResponse.json({ items: all.slice(start, end), next_cursor: start > 0 ? encodeCursor({ end: start }) : null, has_more: start > 0 });
  }),
  http.get(`${API}/tasks/:id/usage`, ({ params }) => {
    const t = findTask(String(params.id));
    if (!t) return err(404, 'not_found', '任务不存在');
    if (t.id === MAIN_TASK) return HttpResponse.json(mainUsage());
    const u = t.usage;
    const rows: UsageRow[] = u.requests
      ? [{ subtask_id: '', module_id: 'task_success', call_kind: 'probe', model_name: t.vlm?.model ?? 'doubao-seed-2-0-pro-260215', ...u }]
      : [];
    return HttpResponse.json({ totals: u, actual: rows, attributed: rows });
  }),
];

function configOf(t: Task): TaskCreate {
  return {
    name: t.name,
    note: t.note ?? undefined,
    input: t.dataset_id ? { dataset_id: t.dataset_id } : t.input,
    output: t.output,
    preflight_id: [...db.preflights.keys()].pop() ?? '',
    episodes: t.episodes,
    modules: t.modules.filter((m) => m.selected).map((m) => m.id),
    ...(t.embodiment_id ? { embodiment_id: t.embodiment_id } : {}),
    ...(t.vlm ? { vlm: { backend: t.vlm.backend, model: t.vlm.model, reasoning_effort: t.vlm.reasoning_effort ?? null } } : {}),
    params: t.params,
  };
}

function genericLogs(t: Task) {
  const out = [];
  for (let i = 0; i < 12; i += 1) {
    out.push({ ts: t.created_at + i * 5000, stage: i < 4 ? 'numeric' : 'frame', subtask_id: null, level: 'info' as const, msg: `processed batch ${i + 1}`, episode_index: null });
  }
  return out;
}

// ------------------------------------------------------------------ report

function genericReport(t: Task, revision: number): Report {
  const s = t.summary ?? { total: 0, passed: 0, rejected: 0, held: 0, review: 0, pass_rate: null };
  return {
    schema_version: '1.0',
    revision,
    overview: {
      dataset: { name: datasetName(t) },
      run: { run_id: t.run_id },
      counts: { total: s.total, passed: s.passed, rejected: s.rejected, held: s.held, review: s.review, ...(s.skipped !== undefined ? { skipped: s.skipped } : {}) },
      pass_rate: s.pass_rate,
      reject_reasons: s.rejected ? [{ module: 'timestamp_check', count: s.rejected }] : [],
      token_usage: { prompt: t.usage.prompt_tokens, completion: t.usage.completion_tokens, reasoning: t.usage.reasoning_tokens, cached: t.usage.cached_tokens, requests: t.usage.requests, requests_unknown_usage: t.usage.requests_unknown_usage },
      duration_s: 1500,
    },
    modules: t.modules
      .filter((m) => m.selected)
      .map((m) => {
        const spec = registry.modules.find((x) => x.id === m.id)!;
        return {
          id: m.id,
          state: 'succeeded' as const,
          gate: spec.gate,
          summary: { checked: s.total },
          tables: spec.tables.map((tb) => ({ id: tb.id, rows: tableRows(tb.id).length, file: `tables/${tb.id}.parquet` })),
          adjudication: spec.produces_adjudication && t.pending_adjudication ? { pending: t.pending_adjudication } : null,
        };
      }),
    skipped_modules: t.modules.filter((m) => !m.selected && m.availability !== 'available').map((m) => ({ id: m.id, reason: m.unavailable_reason ?? '未运行' })),
    integrity: { format: 'LeRobot v2', ...(t.id === SO101_TASK ? { skipped_episodes: SO101_SKIPPED } : {}) },
    perf: {},
  };
}

function revisionOf(t: Task, url: URL): number | Response {
  const rev = url.searchParams.get('rev');
  if (t.result_rev < 1) return err(404, 'not_found', '任务还没有生成报告');
  if (!rev) return t.result_rev;
  const n = Number(rev);
  if (!Number.isInteger(n) || n < 1 || n > t.result_rev) return err(404, 'not_found', `没有结果版本 r${rev}`);
  return n;
}

const report = [
  http.get(`${API}/tasks/:id/report`, ({ request, params }) => {
    const t = findTask(String(params.id));
    if (!t) return err(404, 'not_found', '任务不存在');
    const rev = revisionOf(t, new URL(request.url));
    if (rev instanceof Response) return rev;
    const r = t.id === MAIN_TASK ? mainReport(rev === 1 ? 1 : 2) : genericReport(t, rev);
    return HttpResponse.json({ revision: rev, report: r, links: [{ rel: 'report', title: 'Open QA report', url: `/tasks/${t.id}/report?rev=${rev}`, absolute: false }] });
  }),
  http.get(`${API}/tasks/:id/report/tables/:table`, ({ request, params }) => {
    const t = findTask(String(params.id));
    if (!t) return err(404, 'not_found', '任务不存在');
    const url = new URL(request.url);
    const rev = revisionOf(t, url);
    if (rev instanceof Response) return rev;
    const table = String(params.table);
    const spec = registry.modules.flatMap((m) => m.tables).find((x) => x.id === table);
    if (!spec) return err(404, 'not_found', `没有明细表 ${table}`);
    const sort = url.searchParams.get('sort') ?? spec.default_sort;
    if (!spec.sortable.includes(sort)) return err(400, 'validation_failed', `明细表 ${table} 不能按 ${sort} 排序（可选：${spec.sortable.join('、')}）`);
    const order = url.searchParams.get('order') === 'desc' ? 'desc' : 'asc';
    const cur = decodeCursor<{ rev: number; o: number; sort: string; order: string }>(url.searchParams.get('cursor'));
    if (cur && cur.rev !== t.result_rev) return err(409, 'result_changed', '结果版本已经更新，明细表回到第一页');
    const rows = [...tableRows(table)].sort((a, b) => {
      const x = a[sort] as number | string;
      const y = b[sort] as number | string;
      const c = x === y ? 0 : x === null || x === undefined ? -1 : y === null || y === undefined ? 1 : x < y ? -1 : 1;
      return order === 'asc' ? c : -c;
    });
    const limit = Math.min(Number(url.searchParams.get('limit') ?? 100), 500);
    const offset = cur?.o ?? 0;
    const items = rows.slice(offset, offset + limit);
    const more = offset + limit < rows.length;
    const columns = rows.length ? Object.keys(rows[0]) : ['episode_index'];
    return HttpResponse.json({ items, columns, revision: rev, next_cursor: more ? encodeCursor({ rev: t.result_rev, o: offset + limit, sort, order }) : null, has_more: more });
  }),
  http.get(`${API}/tasks/:id/episodes/:index`, ({ request, params }) => {
    const t = findTask(String(params.id));
    if (!t) return err(404, 'not_found', '任务不存在');
    const rev = revisionOf(t, new URL(request.url));
    if (rev instanceof Response) return rev;
    const ep = Number(params.index);
    const total = t.summary?.total ?? 50;
    if (!Number.isInteger(ep) || ep < 0 || ep >= Math.max(total, 50)) return err(404, 'not_found', `没有 ep ${String(params.index)}`);
    return HttpResponse.json(episodeView(ep, rev));
  }),
  http.get(`${API}/tasks/:id/perf`, ({ request, params }) => {
    const t = findTask(String(params.id));
    if (!t) return err(404, 'not_found', '任务不存在');
    const url = new URL(request.url);
    const rev = revisionOf(t, url);
    if (rev instanceof Response) return rev;
    const scope = (url.searchParams.get('scope') ?? 'all') as 'all' | 'main' | 'subtask';
    if (scope === 'subtask' && !url.searchParams.get('subtask')) return err(400, 'validation_failed', '选了子任务范围，要给 subtask');
    return HttpResponse.json(mainPerf(scope, url.searchParams.get('subtask'), rev));
  }),
  http.get(`${API}/tasks/:id/adjudication`, ({ request, params }) => {
    const t = findTask(String(params.id));
    if (!t) return err(404, 'not_found', '任务不存在');
    const url = new URL(request.url);
    const tab = url.searchParams.get('tab') === 'appeals' ? 'appeals' : 'review';
    const source = url.searchParams.get('source');
    const status = url.searchParams.get('status') ?? 'pending';
    const cards = cardsOf(t.id, tab)
      .filter((c) => !source || c.questions.some((q) => q.source_module === source))
      .filter((c) => {
        if (status === 'all') return true;
        if (status === 'pending') return c.status === 'pending' || c.status === 'unsure';
        if (status === 'decided') return c.status === 'decided' || c.status === 'applied';
        return c.questions.some((q) => q.latest_decision && !q.latest_decision.applied && q.latest_decision.decision !== 'unsure');
      });
    return HttpResponse.json({ ...cursorPage<AdjudicationCard>(cards, url), counts: countsOf(t.id) });
  }),
  http.post(`${API}/tasks/:id/adjudication`, async ({ request, params }) => {
    const t = findTask(String(params.id));
    if (!t) return err(404, 'not_found', '任务不存在');
    const b = await body<{ decisions: DecisionInput[] }>(request, 'submitAdjudication');
    // C4 1.5 (D43): a decision must be one of its line's catalog decisions, on a question the
    // episode's card has; anything else is 400. Appeal cards only exist for appealable modules (D42).
    const catalog = reviewCatalog();
    for (const d of b.decisions) {
      const line = catalog.find((l) => l.id === d.line);
      if (!line) return err(400, 'validation_failed', `没有「${d.line}」这种复核`);
      const card = cardsOf(t.id, line.applies_to === 'reject' ? 'appeals' : 'review').find((c) => c.episode_index === d.episode_index);
      if (!card || !card.questions.some((q) => q.line === d.line)) return err(400, 'validation_failed', `ep ${d.episode_index} 没有「${line.title_zh}」这一问`);
      if (!line.decisions.some((x) => x.const === d.decision)) return err(400, 'validation_failed', `「${line.title_zh}」不能选 ${d.decision}`);
      if (d.decision === 'custom_label' && !d.new_label?.trim()) return err(400, 'validation_failed', '自行改写标注要填新标注');
    }
    const list = decisionsOf(t.id);
    for (const d of b.decisions) {
      list.push({ ...d, new_label: d.new_label ?? null, note: d.note ?? null, id: list.length + 1, decided_by: 'galbot', decided_at: clock(), applied: false });
    }
    return HttpResponse.json(countsOf(t.id));
  }),
  http.post(`${API}/tasks/:id/adjudication/apply`, ({ request, params }) =>
    idempotent(request, async () => {
      const t = findTask(String(params.id));
      if (!t) return err(404, 'not_found', '任务不存在');
      // C4 1.4: optional AdjudicationApply body; v1's two layers unless full is asked for (D39).
      const b = await body<{ relabel_rerun?: 'v1' | 'full' }>(request, 'applyAdjudication');
      if (t.active_subtask) return err(409, 'subtask_active', '这个任务已有未结束的子任务，等它结束后再执行裁决');
      if (countsOf(t.id).unapplied === 0) return err(400, 'validation_failed', '没有尚未应用的裁决');
      const s = newSubtask(t, 'apply_adjudication', { relabel_rerun: b.relabel_rerun ?? 'v1' });
      for (const d of decisionsOf(t.id)) if (d.decision !== 'unsure') d.applied = true;
      t.delivery_stale = true;
      return HttpResponse.json({ subtask: s, links: t.links }, { status: 202 });
    }),
  ),
];

// ------------------------------------------------------------------ media

let signSeq = 0;

const system = [
  http.get(`${API}/media/sign`, ({ request }) => {
    const url = new URL(request.url);
    const taskId = url.searchParams.get('task') ?? '';
    const scope = url.searchParams.get('scope');
    const path = url.searchParams.get('path') ?? '';
    const ttl = Number(url.searchParams.get('ttl') ?? 1800);
    const t = findTask(taskId);
    if (!t) return err(404, 'not_found', '任务不存在');
    if (scope !== 'delivery' && scope !== 'input') return err(400, 'validation_failed', 'scope 只能是 delivery 或 input');
    if (path.includes('..')) return err(400, 'validation_failed', '路径不合法');
    signSeq += 1;
    const root = scope === 'delivery' ? `${t.output.uri}/${t.run_id ?? ''}` : t.input.uri;
    const host = root.replace(/^tos:\/\//, '').split('/')[0];
    const key = `${root.replace(/^tos:\/\/[^/]+\/?/, '')}/${path}`.replace(/\/+/g, '/');
    return HttpResponse.json({
      url: `https://${host}.tos-cn-beijing.volces.com/${key}?X-Tos-Expires=${ttl}&X-Tos-Signature=mock${signSeq}`,
      expires_at: clock() + ttl * 1000,
    });
  }),
  http.get('*/healthz', () => HttpResponse.json({ status: 'ok' })),
  http.get('*/readyz', () => HttpResponse.json({ status: 'ok', checks: { db_writable: true, master_key: true, workdir_writable: true, scratch_writable: true, reconciled: true } })),
];

export const handlers = [...credentials, ...backends, ...datasets, ...tasks, ...report, ...system];
