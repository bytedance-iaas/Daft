// The mock world: one consistent set of fixtures, typed with the generated C4 types and
// validated against the OpenAPI schemas in src/mocks/contract.test.ts. The storyline follows
// the approved mockups (frontend/mockups/README.md): «droid 前 50 条质检» is the task that the
// detail, report and adjudication pages tell the whole story of.
import modulesJson from '../../../docs/contracts/modules.json';
import type {
  AdjudicationCard,
  Credential,
  DatasetCheck,
  DatasetDetail,
  Decision,
  EpisodePreview,
  EpisodeView,
  LogLine,
  ModuleRegistry,
  ModuleState,
  Perf,
  Plan,
  PreflightResult,
  Report,
  ResultRecord,
  StageProgress,
  Subtask,
  Task,
  TaskState,
  TimelineEntry,
  UsageRow,
  UsageTotals,
  VlmBackend,
} from '../api/types';

export const registry = modulesJson as ModuleRegistry;

export const EMBODIMENTS = ['agibot', 'aloha', 'franka', 'google_robot', 'pusht', 'so100', 'so101', 'ur5', 'widowx'];

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

// ------------------------------------------------------------------ helpers

export function usageTotals(p: number, c: number, r: number, cached: number, req: number, unknown = 0): UsageTotals {
  return {
    prompt_tokens: p,
    completion_tokens: c,
    reasoning_tokens: r,
    cached_tokens: cached,
    requests: req,
    requests_unknown_usage: unknown,
  };
}

export const ZERO_USAGE = usageTotals(0, 0, 0, 0, 0, 0);

function stage(id: StageProgress['id'], state: StageProgress['state'], done: number, total: number, elapsed: number | null, extra: Partial<StageProgress> = {}): StageProgress {
  return { id, state, done, total, elapsed_s: elapsed, eta_s: null, ...extra };
}

type ModId = string;

/** ModuleState rows for every registry module (unselected ones keep their availability). */
function moduleStates(selected: ModId[], overrides: Record<ModId, Partial<ModuleState>>, total: number): ModuleState[] {
  return registry.modules.map((m) => {
    const isSel = selected.includes(m.id);
    const base: ModuleState = {
      id: m.id,
      name: m.name_zh,
      selected: isSel,
      availability: 'available',
      unavailable_reason: null,
      state: isSel ? 'succeeded' : 'skipped',
      episodes_total: isSel ? total : 0,
      episodes_error: 0,
      elapsed_s: null,
      error: null,
    };
    return { ...base, ...(overrides[m.id] ?? {}) };
  });
}

// ------------------------------------------------------------------ credentials and backends

export function seedCredentials(now: number): Credential[] {
  return [
    {
      id: 'cred_prod',
      name: 'prod-tos',
      kind: 'tos',
      meta: { region: 'cn-beijing', access_key_id_hint: '7Q2D' },
      verify_state: 'ok',
      last_verified_at: now - 2 * HOUR,
      last_verify_error: null,
      references: { active_tasks: 1, historical_tasks: 8 },
      created_at: now - 19 * DAY,
      updated_at: now - 2 * HOUR,
    },
    {
      id: 'cred_readonly',
      name: 'readonly-tos',
      kind: 'tos',
      meta: { region: 'cn-beijing', access_key_id_hint: 'K9LM' },
      verify_state: 'ok',
      last_verified_at: now - 2 * HOUR,
      last_verify_error: null,
      references: { active_tasks: 0, historical_tasks: 6 },
      created_at: now - 19 * DAY,
      updated_at: now - 2 * HOUR,
    },
    {
      id: 'cred_partner',
      name: 'partner-upload',
      kind: 'tos',
      meta: { region: 'cn-shanghai', access_key_id_hint: 'P0RT' },
      verify_state: 'unverified',
      last_verified_at: null,
      last_verify_error: '这对密钥没有列存储桶的权限，也没填测试用存储桶',
      references: { active_tasks: 0, historical_tasks: 0 },
      created_at: now - 3 * DAY,
      updated_at: now - 3 * DAY,
    },
    {
      id: 'cred_oldci',
      name: 'old-ci',
      kind: 'tos',
      meta: { region: 'cn-beijing', access_key_id_hint: 'CI01' },
      verify_state: 'failed',
      last_verified_at: now - 2 * DAY,
      last_verify_error: 'SignatureDoesNotMatch：Secret Access Key 不对',
      references: { active_tasks: 0, historical_tasks: 1 },
      created_at: now - 32 * DAY,
      updated_at: now - 2 * DAY,
    },
  ];
}

const DOUBAO_LEVELS = ['minimal', 'low', 'medium', 'high'] as const;
const ALL_LEVELS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] as const;

export function seedBackends(now: number): VlmBackend[] {
  return [
    {
      id: 'vb_ark_prod',
      name: 'ark-prod',
      kind: 'ark',
      endpoint: 'https://ark.cn-beijing.volces.com/api/v3',
      max_concurrency: 64,
      has_api_key: true,
      verify_state: 'ok',
      last_verified_at: now - 2 * HOUR,
      last_verify_error: null,
      models_listed: true,
      models: [
        { id: 'vm_pro', model_name: 'doubao-seed-2-0-pro-260215', reasoning_effort: null, max_concurrency: null, capabilities: { vision: true, reasoning_effort_levels: [...DOUBAO_LEVELS] }, source: 'listed' },
        { id: 'vm_lite', model_name: 'doubao-seed-2-0-lite-260215', reasoning_effort: 'minimal', max_concurrency: null, capabilities: { vision: true, reasoning_effort_levels: [...DOUBAO_LEVELS] }, source: 'listed' },
        { id: 'vm_16', model_name: 'doubao-seed-1-6-251015', reasoning_effort: null, max_concurrency: 32, capabilities: { vision: true, reasoning_effort_levels: [...DOUBAO_LEVELS] }, source: 'listed' },
      ],
      created_at: now - 19 * DAY,
      updated_at: now - 2 * HOUR,
    },
    {
      id: 'vb_ark_ep',
      name: 'ark-ep',
      kind: 'ark',
      endpoint: 'https://ark.cn-beijing.volces.com/api/v3',
      max_concurrency: 64,
      has_api_key: true,
      verify_state: 'ok',
      last_verified_at: now - DAY,
      last_verify_error: null,
      models_listed: false,
      models: [
        { id: 'vm_ep', model_name: 'ep-20260915173012-x7k2p', reasoning_effort: null, max_concurrency: null, capabilities: { vision: null, reasoning_effort_levels: [...ALL_LEVELS] }, source: 'manual' },
      ],
      created_at: now - 6 * DAY,
      updated_at: now - DAY,
    },
    {
      id: 'vb_vllm',
      name: 'vllm-a100',
      kind: 'custom',
      endpoint: 'http://vllm-a100.infra.svc:8000/v1',
      max_concurrency: 32,
      has_api_key: false,
      verify_state: 'failed',
      last_verified_at: now - 3 * HOUR,
      last_verify_error: '连接超时：10 秒内没有响应',
      models_listed: true,
      models: [
        { id: 'vm_qwen', model_name: 'Qwen2.5-VL-72B-Instruct', reasoning_effort: null, max_concurrency: null, capabilities: { vision: true, reasoning_effort_levels: [...DOUBAO_LEVELS] }, source: 'listed' },
      ],
      created_at: now - 4 * DAY,
      updated_at: now - 3 * HOUR,
    },
  ];
}

// ------------------------------------------------------------------ preflight results

export interface DatasetProfile {
  uri: string;
  source: 'tos' | 'public' | 'local';
  name: string;
  format: PreflightResult['format'];
  episodes: number;
  cameras: string[];
  fps: number;
  robotType: string | null;
  withTask: number;
  missing: string[];
  profile: { matched: string; by: string } | null;
}

export const DATASET_PROFILES: DatasetProfile[] = [
  {
    uri: 'tos://pai-kit-datasets/lerobot/droid_100',
    source: 'tos',
    name: 'droid_100',
    format: { kind: 'lerobot', version: 'v3', supported: true, detail: 'LeRobot v3, 100 episodes, 3 cameras' },
    episodes: 100,
    cameras: ['exterior_image_1_left', 'exterior_image_2_left', 'wrist_image_left'],
    fps: 15,
    robotType: null,
    withTask: 56,
    missing: [],
    profile: { matched: 'droid_100', by: 'features' },
  },
  {
    uri: 'tos://pai-kit-datasets/lerobot/droid-200',
    source: 'tos',
    name: 'droid-200',
    format: { kind: 'lerobot', version: 'v2', supported: true, detail: 'LeRobot v2, 200 episodes, 3 cameras' },
    episodes: 200,
    cameras: ['exterior_1', 'exterior_2', 'wrist'],
    fps: 15,
    robotType: null,
    withTask: 112,
    missing: ['state'],
    profile: { matched: 'droid', by: 'robot_type' },
  },
  {
    uri: 'tos://pai-kit-datasets/umi/umi_640_notask',
    source: 'tos',
    name: 'umi_640_notask',
    format: { kind: 'lerobot', version: 'v2', supported: true, detail: 'LeRobot v2, 640 episodes, 2 cameras' },
    episodes: 640,
    cameras: ['left_wrist', 'right_wrist'],
    fps: 30,
    robotType: 'umi_dual_handheld_gripper',
    withTask: 0,
    missing: [],
    profile: { matched: 'umi', by: 'robot_type' },
  },
  {
    uri: 'tos://hf-cache/lerobot/libero_10',
    source: 'public',
    name: 'libero_10',
    format: { kind: 'lerobot', version: 'v2', supported: true, detail: 'LeRobot v2, 379 episodes, 2 cameras' },
    episodes: 379,
    cameras: ['image', 'wrist_image'],
    fps: 10,
    robotType: 'franka',
    withTask: 379,
    missing: [],
    profile: { matched: 'libero', by: 'robot_type' },
  },
  {
    uri: 'tos://hf-cache/lerobot/pusht',
    source: 'public',
    name: 'pusht',
    format: { kind: 'lerobot', version: 'v2', supported: true, detail: 'LeRobot v2, 206 episodes, 1 camera' },
    episodes: 206,
    cameras: ['image'],
    fps: 10,
    robotType: 'pusht',
    withTask: 206,
    missing: [],
    profile: null,
  },
  {
    uri: 'tos://pai-kit-datasets/raw/warehouse_mcap',
    source: 'tos',
    name: 'warehouse_mcap',
    format: { kind: 'mcap', version: null, supported: false, detail: '当前版本仅支持 LeRobot v2/v3，检测到 mcap' },
    episodes: 0,
    cameras: [],
    fps: 0,
    robotType: null,
    withTask: 0,
    missing: [],
    profile: null,
  },
];

export const PUBLIC_BUCKET = 'hf-cache';

export function profileFor(uri: string): DatasetProfile {
  const clean = uri.replace(/\/+$/, '');
  const hit = DATASET_PROFILES.find((p) => p.uri === clean);
  if (hit) return hit;
  const name = clean.split('/').pop() || 'dataset';
  return {
    uri: clean,
    source: clean.includes(`//${PUBLIC_BUCKET}/`) ? 'public' : 'tos',
    name,
    format: { kind: 'lerobot', version: 'v2', supported: true, detail: 'LeRobot v2, 120 episodes, 2 cameras' },
    episodes: 120,
    cameras: ['front', 'wrist'],
    fps: 30,
    robotType: 'so101',
    withTask: 120,
    missing: [],
    profile: null,
  };
}

const DIGEST = 'sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08';
const DIGEST2 = 'sha256:4be1c02d9f3ce21a7d24c8b3a90cf1e8d7a5b6c4e3f2a1b0c9d8e7f6a5b4c3d2';

/** The preflight result for a dataset, given the VLM backend and robot type chosen so far. */
export function preflightFor(p: DatasetProfile, opts: { vlmBackend?: string; embodiment?: string }): PreflightResult {
  if (!p.format.supported) {
    return {
      schema_version: '1.0',
      format: p.format,
      validation: [],
      dataset: null,
      modules: registry.modules.map((m) => ({
        id: m.id,
        availability: 'unsupported' as const,
        reason: `only LeRobot v2/v3 is supported in this version; detected ${p.format.kind} files`,
        reason_code: 'format_unsupported',
        reason_args: { detected: p.format.kind },
      })),
      meta_fingerprint: DIGEST,
      warnings: [],
    };
  }
  const robot = opts.embodiment || p.robotType;
  const modules: PreflightResult['modules'] = registry.modules.map((m) => {
    const needs = m.needs as string[];
    if (needs.includes('state') && p.missing.includes('state')) {
      return {
        id: m.id,
        availability: 'unsupported',
        reason: 'observation.state is missing from info.json features',
        reason_code: 'missing_input',
        reason_args: { missing: ['state'] },
      };
    }
    if (needs.includes('embodiment_profile')) {
      if (!robot) {
        return {
          id: m.id,
          availability: 'needs_input',
          reason: 'robot_type not found in info.json; pick a model or skip this module',
          reason_code: 'robot_type_unknown',
          reason_args: { robot_type: null },
          input_hint: { field: 'embodiment_id', options: EMBODIMENTS },
        };
      }
      if (!EMBODIMENTS.includes(robot)) {
        return {
          id: m.id,
          availability: 'unsupported',
          reason: `robot_type '${robot}' is not in the embodiment registry`,
          reason_code: 'embodiment_unsupported',
          reason_args: { subject: robot, given_by: opts.embodiment ? 'embodiment_id' : 'robot_type', supported: EMBODIMENTS },
        };
      }
    }
    if (needs.includes('vlm')) {
      const notes = p.episodes - p.withTask > 0 ? [`${p.episodes - p.withTask} episodes have no task text; the model will caption them first`] : [];
      if (!opts.vlmBackend) {
        return {
          id: m.id,
          availability: 'needs_input',
          reason: 'no VLM backend selected',
          reason_code: 'vlm_backend_missing',
          input_hint: { field: 'vlm' },
          notes,
        };
      }
      return { id: m.id, availability: 'available', notes };
    }
    return { id: m.id, availability: 'available' };
  });
  return {
    schema_version: '1.0',
    format: p.format,
    validation: [],
    dataset: {
      episode_count: p.episodes,
      cameras: p.cameras,
      fps: p.fps,
      robot_type: p.robotType,
      total_frames: p.episodes * 256,
      labels: { with_task: p.withTask, without_task: p.episodes - p.withTask },
      profile: p.profile,
    },
    modules,
    meta_fingerprint: DIGEST,
    warnings: p.format.version === 'v2' && p.withTask < p.episodes ? ['episodes_stats.json missing; per-episode stats will be recomputed'] : [],
  };
}

// ------------------------------------------------------------------ datasets

function datasetDetail(
  id: string,
  p: DatasetProfile,
  now: number,
  extra: Partial<DatasetDetail> & { region: string | null; credential: string | null },
): DatasetDetail {
  const check: DatasetCheck = { at: now - 5 * DAY, trigger: 'add', result: 'same', change: null };
  return {
    id,
    name: p.name,
    source: p.source,
    uri: p.uri,
    format: p.format.supported ? (p.format.version === 'v3' ? 'lerobot_v3' : 'lerobot_v2') : 'unsupported',
    episode_count: p.format.supported ? p.episodes : null,
    robot_type: p.robotType,
    check_state: 'ok',
    checked_at: null,
    preflighted_at: now - 5 * DAY,
    created_at: now - 5 * DAY,
    last_task: null,
    note: null,
    preflight: preflightFor(p, {}),
    meta_fingerprint: DIGEST,
    listing: { objects: p.episodes * 2 + 4, bytes: p.episodes * 30_406_622, digest: DIGEST2 },
    checks: [check],
    tasks: [],
    links: [],
    ...extra,
  };
}

export function seedDatasets(now: number): DatasetDetail[] {
  const byName = (n: string) => DATASET_PROFILES.find((p) => p.name === n)!;
  const droid200Change = { meta_changed: true, added: 12, removed: 0, modified: 1, sample_keys: ['data/chunk-000/episode_000200.parquet', 'meta/info.json'], preflighted_at: now - 4 * DAY };
  return [
    datasetDetail('ds_droid100', byName('droid_100'), now, {
      region: 'cn-beijing',
      credential: 'readonly-tos',
      checked_at: now - 2 * HOUR,
      created_at: now - 12 * DAY,
      checks: [
        { at: now - 2 * HOUR, trigger: 'task_start', result: 'same', change: null },
        { at: now - 12 * DAY, trigger: 'add', result: 'same', change: null },
      ],
    }),
    datasetDetail('ds_droid200', byName('droid-200'), now, {
      region: 'cn-beijing',
      credential: 'readonly-tos',
      check_state: 'changed',
      checked_at: now - 30 * MIN,
      preflighted_at: now - 4 * DAY,
      created_at: now - 10 * DAY,
      checks: [
        { at: now - 30 * MIN, trigger: 'recheck', result: 'changed', change: droid200Change },
        { at: now - 4 * DAY, trigger: 'repreflight', result: 'same', change: null },
        { at: now - 10 * DAY, trigger: 'add', result: 'same', change: null },
      ],
    }),
    datasetDetail('ds_umi', byName('umi_640_notask'), now, {
      region: 'cn-beijing',
      credential: 'readonly-tos',
      created_at: now - 9 * DAY,
    }),
    datasetDetail('ds_libero', byName('libero_10'), now, {
      region: null,
      credential: null,
      created_at: now - 2 * DAY,
    }),
    datasetDetail('ds_mcap', byName('warehouse_mcap'), now, {
      region: 'cn-beijing',
      credential: 'readonly-tos',
      created_at: now - DAY,
    }),
  ];
}

// ------------------------------------------------------------------ tasks

export const MAIN_TASK = 'task_01HXR2D8';
export const RUNNING_TASK = 'task_01HXR4M7';

const ALL_MODULES = registry.modules.map((m) => m.id);
const NON_VLM = registry.modules.filter((m) => !(m.needs as string[]).includes('vlm')).map((m) => m.id);

interface TaskSeed {
  id: string;
  name: string;
  state: TaskState;
  datasetId: string | null;
  uri: string;
  source?: 'tos' | 'public';
  output: string;
  created: number;
  selected: ModId[];
  episodes: Task['episodes'];
  total: number;
  stages: StageProgress[];
  modules?: Record<ModId, Partial<ModuleState>>;
  summary?: Task['summary'];
  usage?: UsageTotals;
  pending?: number;
  stale?: boolean;
  resultRev?: number;
  pauseReason?: Task['pause_reason'];
  stateReason?: string | null;
  vlm?: boolean;
  vlmChoice?: { backend: string; model: string };
  note?: string | null;
  runId?: string | null;
  embodiment?: string | null;
}

function buildTask(s: TaskSeed): Task {
  const started = s.state === 'created' ? null : s.created + 2000;
  const terminal = ['stopped', 'succeeded', 'completed_with_errors', 'failed'].includes(s.state);
  const vlm = s.vlm ?? s.selected.some((id) => (registry.modules.find((m) => m.id === id)?.needs as string[] | undefined)?.includes('vlm'));
  return {
    id: s.id,
    name: s.name,
    note: s.note ?? null,
    state: s.state,
    state_reason: s.stateReason ?? null,
    pause_reason: s.pauseReason ?? null,
    input:
      s.source === 'public'
        ? { source: 'public', uri: s.uri }
        : { source: 'tos', uri: s.uri, region: 'cn-beijing', credential: 'readonly-tos' },
    dataset_id: s.datasetId,
    output: { uri: s.output, region: 'cn-beijing', credential: 'prod-tos' },
    run_id: s.runId !== undefined ? s.runId : started ? new Date(started).toISOString().replace(/[-:]/g, '').replace('T', '-').slice(0, 15) : null,
    episodes: s.episodes,
    embodiment_id: s.embodiment ?? null,
    // No `snapshot`: C4 1.1.0 composes it with the closed VlmChoice, so no instance with it validates.
    vlm: vlm ? { backend: s.vlmChoice?.backend ?? 'ark-prod', model: s.vlmChoice?.model ?? 'doubao-seed-2-0-pro-260215', reasoning_effort: null } : null,
    params: { start_now: s.state !== 'created', export: true, vlm_retry: 3, vlm_hedge: true, clips: false },
    source: started ? { objects: 204, bytes: 1_520_331_122, digest: DIGEST2 } : null,
    progress: { stages: s.stages },
    modules: moduleStates(s.selected, s.modules ?? {}, s.total),
    summary: s.summary ?? null,
    result_rev: s.resultRev ?? (terminal ? 1 : 0),
    usage: s.usage ?? ZERO_USAGE,
    pending_adjudication: s.pending ?? 0,
    delivery_stale: s.stale ?? false,
    active_subtask: null,
    created_at: s.created,
    updated_at: s.created + 60_000,
    started_at: started,
    finished_at: terminal ? s.created + 25 * MIN : null,
    deleted_at: null,
    links: [{ rel: 'task', title: 'Open task', url: `/tasks/${s.id}`, absolute: false }],
  };
}

const allDone = (total: number, withProfile = true): StageProgress[] => [
  stage('autolabel', 'succeeded', Math.round(total * 0.4), Math.round(total * 0.4), 46),
  stage('numeric', 'succeeded', total, total, 3),
  stage('frame', 'succeeded', total, total, 144),
  stage('vlm', 'succeeded', total, total, 408),
  stage('verdict', 'succeeded', 1, 1, 1),
  stage('dedup', 'succeeded', total, total, 11),
  ...(withProfile ? [stage('profile', 'succeeded', total, total, 131)] : []),
  stage('final', 'succeeded', 1, 1, 1),
  stage('export', 'succeeded', total, total, 64),
  stage('report', 'succeeded', 1, 1, 6),
  stage('verify', 'succeeded', 1, 1, 9),
];

export function seedTasks(now: number): Task[] {
  const seeds: TaskSeed[] = [
    {
      id: 'task_01HXR6T3',
      name: 'libero-10 抽检',
      state: 'created',
      datasetId: 'ds_libero',
      uri: 'tos://hf-cache/lerobot/libero_10',
      source: 'public',
      output: 'tos://pai-kit-deliveries/libero_10-0920',
      created: now - 8 * MIN,
      selected: NON_VLM.filter((id) => id !== 'kinematic_limits').concat('kinematic_limits'),
      episodes: { mode: 'head', n: 200 },
      total: 200,
      stages: [],
      embodiment: null,
      runId: null,
    },
    {
      id: 'task_01HXR5Q2',
      name: 'bridge-v2 全量',
      state: 'queued',
      datasetId: null,
      uri: 'tos://pai-kit-datasets/oxe/bridge_v2',
      output: 'tos://pai-kit-deliveries/bridge_v2-0920',
      created: now - 29 * MIN,
      selected: ALL_MODULES,
      episodes: { mode: 'all' },
      total: 2048,
      stages: [stage('autolabel', 'pending', 0, 0, null)],
      modules: Object.fromEntries(ALL_MODULES.map((id) => [id, { state: 'pending' as const }])),
      embodiment: 'widowx',
    },
    {
      id: RUNNING_TASK,
      name: 'umi_640 全量质检',
      state: 'running',
      datasetId: 'ds_umi',
      uri: 'tos://pai-kit-datasets/umi/umi_640_notask',
      output: 'tos://pai-kit-deliveries/umi_640-0920',
      created: now - 50 * MIN,
      selected: ALL_MODULES.filter((id) => id !== 'kinematic_limits'),
      episodes: { mode: 'all' },
      total: 640,
      stages: [
        stage('autolabel', 'succeeded', 640, 640, 612),
        stage('numeric', 'succeeded', 640, 640, 5),
        stage('frame', 'succeeded', 631, 631, 1033, { note: '数值档拦下了 9 条（时间戳异常），后面的档只处理剩下的 631 条' }),
        stage('vlm', 'running', 410, 631, 1102, { eta_s: 1440 }),
      ],
      modules: {
        kinematic_limits: { availability: 'unsupported', unavailable_reason: '机器人型号 umi_dual_handheld_gripper 不在规格库，整项跳过', state: 'skipped' },
        task_success: { state: 'running', episodes_total: 631 },
        dedup: { state: 'pending', episodes_total: 0 },
        skill_profile: { state: 'pending', episodes_total: 0 },
        visual_quality: { episodes_total: 631 },
        video_action_sync: { episodes_total: 631 },
      },
      usage: usageTotals(1_700_000, 120_000, 70_000, 812_000, 1210, 12),
    },
    {
      id: MAIN_TASK,
      name: 'droid 前 50 条质检',
      note: '第一轮抽检，看标注质量',
      state: 'completed_with_errors',
      datasetId: 'ds_droid100',
      uri: 'tos://pai-kit-datasets/lerobot/droid_100',
      output: 'tos://pai-kit-deliveries/droid-50',
      created: now - 2 * HOUR,
      selected: ALL_MODULES.filter((id) => id !== 'kinematic_limits'),
      episodes: { mode: 'head', n: 50 },
      total: 50,
      runId: '20260920-130514',
      stages: [
        stage('autolabel', 'succeeded', 22, 22, 46),
        stage('numeric', 'succeeded', 50, 50, 9),
        stage('frame', 'succeeded', 49, 49, 144, { note: '数值档拦下了 1 条（ep 18，残段），所以后面的档是 49 条' }),
        stage('vlm', 'completed_with_errors', 49, 49, 542, { note: '2 条出错（ep 7、ep 31），暂不交付，等待补跑' }),
        stage('verdict', 'succeeded', 1, 1, 1),
        stage('dedup', 'succeeded', 42, 42, 11),
        stage('profile', 'succeeded', 41, 41, 131),
        stage('final', 'succeeded', 1, 1, 1),
        stage('export', 'skipped', 0, 0, null, { note: '主流程结束时没有可交付的条目，没有导出' }),
        stage('report', 'succeeded', 1, 1, 6),
        stage('verify', 'skipped', 0, 0, null),
      ],
      modules: {
        kinematic_limits: { availability: 'needs_input', unavailable_reason: '预检未读到机器人型号，创建时选择跳过', state: 'skipped' },
        timestamp_check: { elapsed_s: 2 },
        motion_quality: { elapsed_s: 7 },
        visual_quality: { episodes_total: 49, elapsed_s: 144 },
        video_action_sync: { episodes_total: 49, elapsed_s: 144 },
        task_success: { state: 'completed_with_errors', episodes_total: 49, episodes_error: 2, elapsed_s: 542, error: null },
        dedup: { episodes_total: 42, elapsed_s: 11 },
        skill_profile: { episodes_total: 41, elapsed_s: 131 },
      },
      summary: { total: 50, passed: 41, rejected: 7, held: 2, review: 10, pass_rate: 0.82 },
      usage: usageTotals(2_010_000, 67_500, 42_900, 931_800, 886, 41),
      pending: 10,
      stale: true,
      resultRev: 2,
    },
    {
      id: 'task_01HXQ5R9',
      name: 'droid-200 抽检',
      state: 'succeeded',
      datasetId: 'ds_droid200',
      uri: 'tos://pai-kit-datasets/lerobot/droid-200',
      output: 'tos://pai-kit-deliveries/droid-200',
      created: now - 7 * HOUR,
      selected: ALL_MODULES.filter((id) => id !== 'motion_quality' && id !== 'kinematic_limits'),
      episodes: { mode: 'all' },
      total: 200,
      stages: allDone(200),
      modules: {
        motion_quality: { availability: 'unsupported', unavailable_reason: '数据集缺少 observation.state 列', state: 'skipped' },
        kinematic_limits: { availability: 'needs_input', unavailable_reason: '预检未读到机器人型号，创建时选择跳过', state: 'skipped' },
      },
      summary: { total: 200, passed: 188, rejected: 12, held: 0, review: 32, pass_rate: 0.94 },
      usage: usageTotals(3_300_000, 110_000, 70_000, 1_500_000, 2100),
      pending: 32,
    },
    {
      id: 'task_01HXPZ2K',
      name: 'so101 夜间批次',
      state: 'succeeded',
      datasetId: null,
      uri: 'tos://pai-kit-datasets/so101/so101-nightly',
      output: 'tos://pai-kit-deliveries/so101-nightly',
      created: now - 14 * HOUR,
      selected: ALL_MODULES,
      episodes: { mode: 'all' },
      total: 1024,
      stages: allDone(1024),
      // 1027 selected; 3 miss source files and are left out (D40): see SO101_SKIPPED.
      summary: { total: 1024, passed: 968, rejected: 56, held: 0, review: 0, pass_rate: 0.945, skipped: 3 },
      usage: usageTotals(4_900_000, 170_000, 90_000, 2_300_000, 5200),
      embodiment: 'so101',
      resultRev: 2,
    },
    {
      id: 'task_01HXPX4W',
      name: 'widowx 回归',
      state: 'paused',
      pauseReason: 'system',
      datasetId: null,
      uri: 'tos://pai-kit-datasets/oxe/widowx-recheck',
      output: 'tos://pai-kit-deliveries/widowx-recheck',
      created: now - 16 * HOUR,
      selected: ALL_MODULES,
      episodes: { mode: 'all' },
      total: 430,
      stages: [stage('autolabel', 'succeeded', 120, 120, 300), stage('numeric', 'succeeded', 430, 430, 4), stage('frame', 'succeeded', 430, 430, 700), stage('vlm', 'running', 180, 430, 900)],
      modules: { task_success: { state: 'running' }, dedup: { state: 'pending', episodes_total: 0 }, skill_profile: { state: 'pending', episodes_total: 0 } },
      usage: usageTotals(640_000, 20_000, 9_000, 300_000, 400),
      embodiment: 'widowx',
    },
    {
      id: 'task_01HXPW8D',
      name: 'agibot 预检回归',
      state: 'paused',
      pauseReason: 'user',
      datasetId: null,
      uri: 'tos://pai-kit-datasets/agibot/agibot-world',
      output: 'tos://pai-kit-deliveries/agibot-world',
      created: now - 18 * HOUR,
      selected: ALL_MODULES,
      episodes: { mode: 'all' },
      total: 3200,
      stages: [stage('autolabel', 'succeeded', 800, 800, 1900), stage('numeric', 'succeeded', 3200, 3200, 30), stage('frame', 'running', 1216, 3200, 4100)],
      modules: { visual_quality: { state: 'running' }, video_action_sync: { state: 'running' }, task_success: { state: 'pending', episodes_total: 0 }, dedup: { state: 'pending', episodes_total: 0 }, skill_profile: { state: 'pending', episodes_total: 0 } },
      usage: usageTotals(910_000, 30_000, 12_000, 400_000, 800),
      embodiment: 'agibot',
    },
    {
      id: 'task_01HXPN6F',
      name: 'franka 20 条冒烟',
      state: 'failed',
      stateReason: '数值档之后访问密钥失效：InvalidAccessKeyId',
      datasetId: null,
      uri: 'tos://pai-kit-datasets/franka/franka-smoke',
      output: 'tos://pai-kit-deliveries/franka-smoke',
      created: now - 21 * HOUR,
      selected: ALL_MODULES,
      episodes: { mode: 'head', n: 20 },
      total: 20,
      stages: [stage('autolabel', 'succeeded', 20, 20, 30), stage('numeric', 'succeeded', 20, 20, 1), stage('frame', 'failed', 3, 20, 12)],
      modules: { visual_quality: { state: 'failed', error: 'InvalidAccessKeyId' }, video_action_sync: { state: 'failed' }, task_success: { state: 'pending', episodes_total: 0 }, dedup: { state: 'pending', episodes_total: 0 }, skill_profile: { state: 'pending', episodes_total: 0 } },
      usage: usageTotals(28_000, 1_500, 800, 11_000, 20),
      embodiment: 'franka',
      resultRev: 0,
    },
    {
      id: 'task_01HXPJ3C',
      name: 'aloha 手眼标定',
      state: 'stopped',
      datasetId: null,
      uri: 'tos://pai-kit-datasets/aloha/aloha-calib',
      output: 'tos://pai-kit-deliveries/aloha-calib',
      created: now - 23 * HOUR,
      selected: ALL_MODULES,
      episodes: { mode: 'all' },
      total: 480,
      stages: [stage('autolabel', 'succeeded', 200, 200, 400), stage('numeric', 'succeeded', 480, 480, 5), stage('frame', 'succeeded', 470, 470, 900), stage('vlm', 'pending', 0, 470, null)],
      modules: { task_success: { state: 'pending', episodes_total: 0 }, dedup: { state: 'pending', episodes_total: 0 }, skill_profile: { state: 'pending', episodes_total: 0 } },
      usage: usageTotals(300_000, 9_000, 3_000, 100_000, 200),
      embodiment: 'aloha',
      resultRev: 0,
      // A finished task on ark-ep: deleting that backend needs confirm=true (W8 behaviour).
      vlmChoice: { backend: 'ark-ep', model: 'ep-20260915173012-x7k2p' },
    },
  ];
  // Older finished tasks, so the list really paginates.
  for (let i = 0; i < 30; i += 1) {
    const total = 40 + ((i * 37) % 300);
    const rejected = Math.round(total * (0.04 + (i % 5) * 0.01));
    const quick = i % 4 === 0;
    seeds.push({
      id: `task_01HX${String(1000 + i).padStart(4, '0')}`,
      name: `历史批次 ${String(i + 1).padStart(2, '0')}`,
      state: 'succeeded',
      datasetId: null,
      uri: `tos://pai-kit-datasets/archive/batch_${i + 1}`,
      output: `tos://pai-kit-deliveries/batch_${i + 1}`,
      created: now - (2 + i) * DAY,
      selected: quick ? NON_VLM : ALL_MODULES,
      episodes: { mode: 'all' },
      total,
      stages: allDone(total, !quick),
      summary: { total, passed: total - rejected, rejected, held: 0, review: 0, pass_rate: (total - rejected) / total },
      usage: quick ? ZERO_USAGE : usageTotals(total * 9000, total * 300, total * 200, total * 4000, total * 4),
      embodiment: 'franka',
    });
  }
  return seeds.map(buildTask);
}

export function seedDeletedTask(now: number): Task {
  const t = buildTask({
    id: 'task_01HXDEL1',
    name: '误建的重复任务',
    state: 'stopped',
    datasetId: null,
    uri: 'tos://pai-kit-datasets/lerobot/droid_100',
    output: 'tos://pai-kit-deliveries/droid-dup',
    created: now - 3 * DAY,
    selected: NON_VLM,
    episodes: { mode: 'all' },
    total: 100,
    stages: [stage('numeric', 'succeeded', 100, 100, 5)],
    resultRev: 0,
  });
  return { ...t, deleted_at: now - 2 * DAY };
}

// ------------------------------------------------------------------ the main task's details

export function mainSubtasks(now: number): Subtask[] {
  return [
    {
      id: 'sub_retry1',
      task_id: MAIN_TASK,
      kind: 'retry',
      scope: { modules: ['skill_profile', 'task_success'], episodes: 'errors' },
      state: 'succeeded',
      state_reason: null,
      progress: null,
      created_at: now - 58 * MIN,
      started_at: now - 58 * MIN,
      finished_at: now - 51 * MIN,
      result_rev: 2,
    },
  ];
}

// «so101 夜间批次»: 3 episodes skipped for missing source files (D40), and an adjudication whose
// relabels were judged again with the first run's full flow (D39), then re-exported.
export const SO101_TASK = 'task_01HXPZ2K';

/** Episodes of so101 left out because source files are missing (D40). */
export const SO101_SKIPPED: { episode_index: number; missing: string[] }[] = [
  { episode_index: 212, missing: ['videos/chunk-000/observation.images.wrist/episode_000212.mp4'] },
  { episode_index: 587, missing: ['data/chunk-000/episode_000587.parquet'] },
  {
    episode_index: 901,
    missing: ['videos/chunk-000/observation.images.front/episode_000901.mp4', 'videos/chunk-000/observation.images.wrist/episode_000901.mp4'],
  },
];

export function so101Subtasks(now: number): Subtask[] {
  const at = now - 9 * HOUR;
  return [
    {
      id: 'sub_apply1',
      task_id: SO101_TASK,
      kind: 'apply_adjudication',
      scope: { relabel_rerun: 'full' },
      state: 'succeeded',
      state_reason: null,
      progress: null,
      created_at: at,
      started_at: at,
      finished_at: at + 6 * MIN,
      result_rev: 2,
    },
    {
      id: 'sub_export1',
      task_id: SO101_TASK,
      kind: 'reexport',
      scope: {},
      state: 'succeeded',
      state_reason: null,
      progress: null,
      created_at: at + 20 * MIN,
      started_at: at + 20 * MIN,
      finished_at: at + 31 * MIN,
      result_rev: null,
    },
  ];
}

export function so101Timeline(now: number): TimelineEntry[] {
  const t0 = now - 14 * HOUR;
  const at = now - 9 * HOUR;
  return [
    { at: t0, kind: 'created', text: '创建并开始。开始前检查通过。', state: 'queued', subtask_id: null, revision: null },
    { at: t0 + 2000, kind: 'started', text: '源文件清单固化：选中 1027 条，其中 3 条缺源文件，不参与质检。', state: 'running', subtask_id: null, revision: null },
    { at: t0 + 3 * HOUR, kind: 'finished', text: '主流程结束：通过 968 · 拒绝 56；另有 3 条缺源文件，未参与质检。', state: 'succeeded', subtask_id: null, revision: 1 },
    { at: t0 + 3 * HOUR + 1000, kind: 'revision', text: '结果版本 r0001', state: null, subtask_id: null, revision: 1 },
    { at, kind: 'subtask_started', text: '执行裁决：应用 12 条裁决，其中 4 条改了标，按首轮的完整流程重判任务成败。', state: null, subtask_id: 'sub_apply1', revision: null },
    { at: at + 6 * MIN, kind: 'subtask_finished', text: '执行裁决结束：4 条改标重判完成。', state: 'succeeded', subtask_id: 'sub_apply1', revision: 2 },
    { at: at + 6 * MIN + 1000, kind: 'revision', text: '结果版本 r0002：判决更新。', state: null, subtask_id: 'sub_apply1', revision: 2 },
    { at: at + 20 * MIN, kind: 'subtask_started', text: '重新导出：只处理变动的 episode。', state: null, subtask_id: 'sub_export1', revision: null },
    { at: at + 31 * MIN, kind: 'subtask_finished', text: '重新导出完成，逐文件核验通过。', state: 'succeeded', subtask_id: 'sub_export1', revision: null },
  ];
}

export function mainTimeline(now: number): TimelineEntry[] {
  const t0 = now - 2 * HOUR;
  return [
    { at: t0, kind: 'created', text: '创建并开始。开始前检查通过：数据集能读（readonly-tos）、交付目录能写（prod-tos）、模型能调通（ark-prod）。', state: 'queued', subtask_id: null, revision: null },
    { at: t0 + 2000, kind: 'started', text: '结果目录 20260920-130514，源文件清单 204 个对象已固化。', state: 'running', subtask_id: null, revision: null },
    { at: t0 + 6 * MIN, kind: 'system_pause', text: 'Daemon 升级（v2.0.3 → v2.0.4），VLM 档停在 21 / 49 条。不是用户操作，不需要处理。', state: 'paused', subtask_id: null, revision: null },
    { at: t0 + 8 * MIN, kind: 'system_resume', text: '新版本起来后自动续跑。中断时在飞的 3 条（ep 26、27、30）重新调用了一次，服务端可能已为丢掉的那次计费，这部分 Token 统计不到。', state: 'running', subtask_id: null, revision: null },
    { at: t0 + 17 * MIN, kind: 'finished', text: '主流程结束：技能画像整体失败（方舟返回 429，额度用尽，开头 20 条同类失败后熔断），任务成败判定另有 2 条出错。没有可交付的条目，这次没有导出。', state: 'completed_with_errors', subtask_id: null, revision: 1 },
    { at: t0 + 17 * MIN + 1000, kind: 'revision', text: '结果版本 r0001：通过 0 · 拒绝 7 · 待补跑 43', state: null, subtask_id: null, revision: 1 },
    { at: now - 58 * MIN, kind: 'subtask_started', text: '重试 #1：技能画像（整体重跑 41 条）+ 任务成败判定（出错的 2 条）。开始前检查通过，额度已恢复。', state: null, subtask_id: 'sub_retry1', revision: null },
    { at: now - 51 * MIN, kind: 'subtask_finished', text: '重试 #1 结束：技能画像补齐 41 条；任务成败判定的 2 条仍在取证仲裁这一步超时。', state: 'completed_with_errors', subtask_id: 'sub_retry1', revision: 2 },
    { at: now - 51 * MIN + 1000, kind: 'revision', text: '结果版本 r0002：通过 41 · 拒绝 7 · 待补跑 2。通过名单变了，交付数据集待导出。', state: null, subtask_id: 'sub_retry1', revision: 2 },
  ];
}

export function mainPlan(): Plan {
  return {
    schema_version: '1.0',
    vlm_parallelism: 64,
    limits: { cpu_concurrency: { value: 8, bound_by: 'planner' }, vlm_parallelism: { value: 64, bound_by: 'model' } },
    stages: [
      { id: 'autolabel', kind: 'vlm', command: 'autolabel', episodes: 'unlabeled', gates: { caption: 32 } },
      { id: 'numeric', kind: 'cpu', command: 'check', concurrency: 8, modules: ['timestamp_check', 'motion_quality'], episodes: 'selected', hard_gates: ['timestamp_check'] },
      { id: 'frame', kind: 'cpu', command: 'check', concurrency: 8, modules: ['visual_quality', 'video_action_sync'], episodes: 'survivors:numeric', hard_gates: ['video_action_sync'] },
      { id: 'vlm', kind: 'vlm', command: 'check', modules: ['task_success'], episodes: 'survivors:frame', gates: { episode: 32, probe: 64, endstate: 64, arbitration: 32, guard_caption: 32 }, merge: { strategy: 'none', groups: [] } },
      { id: 'verdict', kind: 'aggregate', command: 'aggregate', phase: 'funnel' },
      { id: 'dedup', kind: 'cpu', command: 'check', concurrency: 1, modules: ['dedup'], episodes: 'keep' },
      { id: 'profile', kind: 'vlm', command: 'check', modules: ['skill_profile'], episodes: 'keep-minus-duplicates', gates: { caption: 32, llm: 16, audit: 16 }, merge: { strategy: 'none', groups: [] } },
      { id: 'final', kind: 'aggregate', command: 'aggregate', phase: 'final' },
    ],
    estimates: { vlm_requests: 780, wall_clock_s: 900, notes: ['现有两个 VLM 模块不参与请求合并（merge = none）'] },
  };
}

export function mainUsage(): { totals: UsageTotals; actual: UsageRow[]; attributed: UsageRow[] } {
  const row = (subtask: string, module: string, kind: UsageRow['call_kind'], p: number, c: number, r: number, cached: number, req: number, unk = 0): UsageRow => ({
    subtask_id: subtask,
    module_id: module,
    call_kind: kind,
    model_name: 'doubao-seed-2-0-pro-260215',
    prompt_tokens: p,
    completion_tokens: c,
    reasoning_tokens: r,
    cached_tokens: cached,
    requests: req,
    requests_unknown_usage: unk,
  });
  const actual = [
    row('', 'task_success', 'caption', 180_000, 6_000, 3_900, 90_000, 22, 1),
    row('', 'task_success', 'probe', 610_000, 17_000, 11_000, 300_000, 392, 20),
    row('', 'task_success', 'endstate', 540_000, 16_000, 10_800, 260_000, 294, 12),
    row('', 'task_success', 'arbitration', 190_000, 9_000, 6_000, 70_000, 66, 6),
    row('', 'skill_profile', 'caption', 20_000, 1_000, 600, 8_000, 20, 0),
    row('sub_retry1', 'task_success', 'arbitration', 24_000, 1_500, 1_000, 8_000, 12, 2),
    row('sub_retry1', 'skill_profile', 'caption', 330_000, 12_000, 7_600, 160_000, 41, 0),
    row('sub_retry1', 'skill_profile', 'llm', 116_000, 5_000, 2_000, 35_800, 39, 0),
  ];
  const sum = (k: keyof UsageTotals) => actual.reduce((a, r) => a + r[k], 0);
  const totals = usageTotals(sum('prompt_tokens'), sum('completion_tokens'), sum('reasoning_tokens'), sum('cached_tokens'), sum('requests'), sum('requests_unknown_usage'));
  return { totals, actual, attributed: actual };
}

// Episodes of the main task: ep 18 fragment, ep 44 duplicate, 5 task_success rejects, 2 errors.
export const TS_REJECTS = [6, 11, 23, 38, 45];
export const HELD = [7, 31];
export const LABEL_REVIEW = [29, 4, 36, 13, 22];
export const VERDICT_REVIEW = [29, 9, 16, 33, 40, 47];

const TASK_TEXTS = [
  'Put the marker in the cup',
  'Close the top drawer',
  'Wipe the table with the cloth',
  'Pour the beans into the pot',
  'Open the cabinet door',
  'Put the lid on the pot',
  'Turn on the faucet',
  'Pull the chair toward the table',
  'Put the orange thing in the can',
  'Close the microwave door',
];

export function taskText(ep: number): { text: string; source: '原始标注' | '自产caption' } {
  const own = ep % 9 === 2 || ep % 7 === 5;
  return { text: own ? `pour ${['water', 'cereal', 'rice'][ep % 3]} into the ${['sink', 'bowl', 'cup'][ep % 3]}` : TASK_TEXTS[ep % TASK_TEXTS.length], source: own ? '自产caption' : '原始标注' };
}

export function episodeList(ep: number): EpisodeView['list'] {
  if (HELD.includes(ep)) return 'held';
  if (ep === 18 || ep === 44 || TS_REJECTS.includes(ep)) return 'reject';
  return 'passed';
}

const CAMERAS = ['exterior_image_1_left', 'exterior_image_2_left', 'wrist_image_left'];

function record(ep: number, module: string, gate: ResultRecord['gate'], verdict: ResultRecord['verdict'], score: number | null, details: Record<string, unknown>, error: ResultRecord['error'] = null): ResultRecord {
  return {
    episode_index: ep,
    module,
    verdict,
    passed: verdict === 'pass' ? true : verdict === 'fail' ? false : null,
    score,
    gate,
    details,
    evidence: verdict === 'fail' || verdict === 'abstain' ? [`details/evidence/${module}/ep${String(ep).padStart(6, '0')}_0.jpg`] : [],
    elapsed_s: 0.4 + (ep % 7) * 0.3,
    error,
  };
}

export function episodeView(ep: number, revision: number): EpisodeView {
  const list = episodeList(ep);
  const modules: Record<string, ResultRecord> = {
    timestamp_check: record(ep, 'timestamp_check', 'hard', ep === 18 ? 'fail' : 'pass', null, ep === 18 ? { reason: 'fragment', duration_s: 0.5 } : { duration_s: 12 + (ep % 9) }),
    motion_quality: record(ep, 'motion_quality', 'soft', 'scored', 0.7 + (ep % 5) * 0.05, { smoothness: 0.86, spikes: 0.9 }),
  };
  if (ep !== 18) {
    modules.visual_quality = record(ep, 'visual_quality', 'soft', 'scored', 0.8 + (ep % 4) * 0.04, { cameras: CAMERAS.length });
    modules.video_action_sync = record(ep, 'video_action_sync', 'hard', 'pass', null, { lag_s: 0.03 });
    if (HELD.includes(ep)) {
      modules.task_success = record(ep, 'task_success', 'hard', 'error', null, {}, { kind: 'execution', incidents: [{ step: 'arbitration', cause: 'timeout 60s', call_kind: 'arbitration', attempts: 3 }] });
    } else if (TS_REJECTS.includes(ep)) {
      modules.task_success = record(ep, 'task_success', 'hard', 'fail', null, { completion_end: 0.12 });
    } else if (VERDICT_REVIEW.includes(ep)) {
      modules.task_success = record(ep, 'task_success', 'hard', 'abstain', null, { reason: 'insufficient evidence' });
    } else {
      modules.task_success = record(ep, 'task_success', 'hard', 'pass', null, { completion_end: 0.93 });
    }
  }
  const reasons: Record<string, unknown>[] = [];
  if (ep === 18) reasons.push({ module: 'timestamp_check', text: '残段：全程 0.5 秒（8 帧）' });
  if (ep === 44) reasons.push({ module: 'dedup', text: '与 ep 43 字节级完全重复' });
  if (TS_REJECTS.includes(ep)) reasons.push({ module: 'task_success', text: '3 路复核一致判未完成' });
  const review: Record<string, unknown>[] = [];
  if (LABEL_REVIEW.includes(ep)) review.push({ module: 'skill_profile', text: '标注与画面归入不同技能族' });
  if (VERDICT_REVIEW.includes(ep)) review.push({ module: 'task_success', text: '证据不足，弃权' });
  const scope: 'delivery' | 'input' = list === 'passed' ? 'delivery' : 'input';
  return {
    episode_index: ep,
    revision,
    list,
    reasons,
    review,
    task_text: taskText(ep),
    modules,
    evidence: TS_REJECTS.includes(ep) || VERDICT_REVIEW.includes(ep) ? [0, 1].map((k) => ({ module: 'task_success', path: `details/evidence/task_success/ep${String(ep).padStart(6, '0')}_${k}.jpg`, kind: 'frame' })) : [],
    videos: CAMERAS.map((camera) => ({
      camera,
      scope,
      origin: scope === 'delivery' ? ('delivery_dataset' as const) : ('source_dataset' as const),
      path: scope === 'delivery' ? `export/lerobot_curated/videos/chunk-000/${camera}/episode_${String(ep).padStart(6, '0')}.mp4` : `videos/chunk-000/${camera}/file-000.mp4`,
      ...(scope === 'input' ? { from_ts: ep * 14, to_ts: ep * 14 + 14 } : {}),
    })),
  };
}

export function mainReport(revision: 1 | 2): Report {
  const failedProfile = revision === 1;
  return {
    schema_version: '1.0',
    revision,
    overview: {
      dataset: { name: 'droid_100', format: 'LeRobot v3', episodes: 100 },
      run: { run_id: '20260920-130514', episodes: '前 50 条（ep 0–49）' },
      counts: failedProfile ? { total: 50, passed: 0, rejected: 7, held: 43, review: 10 } : { total: 50, passed: 41, rejected: 7, held: 2, review: 10 },
      pass_rate: failedProfile ? 0 : 0.82,
      reject_reasons: [
        { module: 'task_success', count: 5 },
        { module: 'timestamp_check', count: 1 },
        { module: 'dedup', count: 1 },
      ],
      token_usage: { prompt: 2_010_000, completion: 67_500, reasoning: 42_900, cached: 931_800, requests: 886, requests_unknown_usage: 41 },
      duration_s: failedProfile ? 1016 : 1428,
    },
    modules: [
      { id: 'timestamp_check', state: 'succeeded', gate: 'hard', summary: { checked: 50, failed: 1, out_of_order: 0, jumps: 0, fragments: 1, min_duration_s: 1.0 }, tables: [{ id: 'timestamp_check', rows: 1, file: 'tables/timestamp_check.parquet' }], adjudication: null },
      { id: 'motion_quality', state: 'succeeded', gate: 'soft', summary: { mean: 0.84, smoothness: 0.86, spikes: 0.9, gripper_jitter: 0.77, path_efficiency: 0.71, stuck_episodes: 4 }, tables: [{ id: 'motion_quality', rows: 50, file: 'tables/motion_quality.parquet' }], adjudication: null },
      {
        id: 'visual_quality',
        state: 'succeeded',
        gate: 'soft',
        summary: {
          mean: 0.87,
          checked: 49,
          low_camera_readings: 3,
          score_distribution: [
            { name: '0.5–0.6', count: 1 },
            { name: '0.6–0.7', count: 2 },
            { name: '0.7–0.8', count: 9 },
            { name: '0.8–0.9', count: 21 },
            { name: '0.9–1.0', count: 16 },
          ],
        },
        tables: [{ id: 'visual_quality', rows: 147, file: 'tables/visual_quality.parquet' }],
        adjudication: null,
      },
      { id: 'video_action_sync', state: 'succeeded', gate: 'hard', summary: { misaligned: 0, flagged: 2, suspect: 1, undetermined: 3, aligned: 43, flagged_cameras: 2 }, tables: [{ id: 'video_action_sync', rows: 147, file: 'tables/video_action_sync.parquet' }], adjudication: null },
      {
        id: 'task_success',
        state: 'completed_with_errors',
        gate: 'hard',
        summary: {
          checked: 49,
          pass: 36,
          fail: 5,
          abstain: 6,
          error: 2,
          abstain_reasons: [
            { name: '打分层拿不准', count: 3 },
            { name: '复核分歧', count: 2 },
            { name: '护栏拦下', count: 1 },
          ],
        },
        tables: [{ id: 'task_success', rows: 49, file: 'tables/task_success.parquet' }],
        // 6 abstentions to decide; its 5 rejects may be appealed (not pending, D42).
        adjudication: { pending: 6, appealable: 5 },
        episodes_error: 2,
      },
      {
        id: 'dedup',
        state: 'succeeded',
        gate: 'dedup',
        summary: { checked: 42, groups: 1, removed: 1 },
        tables: [{ id: 'dedup_groups', rows: 2, file: 'tables/dedup_groups.parquet' }],
        adjudication: { pending: 0, appealable: 1 },
      },
      failedProfile
        ? { id: 'skill_profile', state: 'failed', gate: 'none', summary: {}, tables: [], adjudication: null, error: 'skill_profile: 429 QuotaExceeded x20, circuit open, exit 4' }
        : {
            id: 'skill_profile',
            state: 'succeeded',
            gate: 'none',
            summary: {
              covered: 41,
              families: 6,
              subskills: 10,
              divergences: 5,
              unstable: 2,
              family_distribution: [
                { name: '放置', count: 14 },
                { name: '倾倒', count: 8 },
                { name: '开合', count: 7 },
                { name: '推拉', count: 6 },
                { name: '擦拭', count: 3 },
                { name: '旋转开关', count: 3 },
              ],
            },
            tables: [{ id: 'skill_assignment', rows: 41, file: 'tables/skill_assignment.parquet' }],
            adjudication: { pending: 5 },
            fingerprints: { prompt: 'sha256:4be1…c02d', from_subtask: 'sub_retry1' },
          },
    ],
    skipped_modules: [{ id: 'kinematic_limits', reason: '预检没读到机器人型号（robot_type 为 unknown），创建任务时选择跳过；其余模块照常' }],
    integrity: {
      format: 'LeRobot v3（结构校验通过）',
      episodes: '数据集 100 条，本次前 50 条（ep 0–49）',
      cameras: CAMERAS.join('、'),
      fps: 15,
      robot_type: null,
      semantics_profile: 'droid_100：动作是归一化的末端速度 + 夹爪，状态是末端绝对位姿',
      labels: { with_task: 28, without_task: 22 },
      missing_fields: [],
    },
    perf: { vlm_wall_s: 900 },
  };
}

/** Rows of a detail table, sorted by episode_index (the Parquet order). */
export function tableRows(table: string): Record<string, unknown>[] {
  const eps = Array.from({ length: 50 }, (_, i) => i);
  switch (table) {
    case 'timestamp_check':
      return [{ episode_index: 18, reason: '残段', duration_s: 0.5, max_dt: 0.07, note: '短于 1.0 秒，按采集中断的碎片处理，判废' }];
    case 'motion_quality':
      return eps.map((ep) => ({ episode_index: ep, score: Number((0.7 + (ep % 5) * 0.05).toFixed(2)), smoothness: 0.86, spikes: 0.9, stuck_s: ep % 11 === 0 ? 1.4 : 0 }));
    case 'visual_quality':
      return eps.filter((ep) => ep !== 18).flatMap((ep) => CAMERAS.map((camera, k) => ({ episode_index: ep, camera, score: Number((0.62 + ((ep + k * 3) % 9) * 0.04).toFixed(2)), sharpness: 100 + ((ep * 7 + k) % 60), exposure: 'ok' })));
    case 'video_action_sync':
      return eps.filter((ep) => ep !== 18).flatMap((ep) => CAMERAS.map((camera, k) => ({ episode_index: ep, camera, lag_s: Number((((ep + k) % 7) * 0.02 - 0.05).toFixed(2)), corr_peak: Number((0.55 + ((ep * 3 + k) % 8) * 0.05).toFixed(2)), status: ep === 3 && k === 1 ? 'flagged' : 'aligned' })));
    case 'task_success':
      return eps.filter((ep) => ep !== 18).map((ep) => ({ episode_index: ep, verdict: HELD.includes(ep) ? 'error' : TS_REJECTS.includes(ep) ? 'fail' : VERDICT_REVIEW.includes(ep) ? 'abstain' : 'pass', text_source: taskText(ep).source }));
    case 'dedup_groups':
      return [
        { episode_index: 43, duplicate_of: null, group: 1 },
        { episode_index: 44, duplicate_of: 43, group: 1 },
      ];
    case 'skill_assignment':
      return eps.filter((ep) => episodeList(ep) === 'passed').map((ep) => ({ episode_index: ep, family: ['放置', '倾倒', '开合', '推拉', '擦拭', '旋转开关'][ep % 6], subskill: `子技能 ${(ep % 10) + 1}` }));
    default:
      return [];
  }
}

export function mainPerf(scope: Perf['scope'], subtask: string | null, revision: number): Perf {
  const k = scope === 'subtask' ? 0.12 : scope === 'main' ? 0.88 : 1;
  const lat = (call_kind: Perf['latency'][number]['call_kind'], count: number, p50: number, p90: number, p99: number, wall: number, failed = 0, hedged = 0) => ({
    call_kind,
    count: Math.round(count * k),
    failed: Math.round(failed * k),
    hedged: Math.round(hedged * k),
    p50_s: p50,
    p90_s: p90,
    p99_s: p99,
    wall_s: Math.round(wall * k),
  });
  return {
    revision,
    scope,
    subtask_id: scope === 'subtask' ? subtask : null,
    backend: { name: 'ark-prod', kind: 'ark', endpoint: 'https://ark.cn-beijing.volces.com/api/v3', model: 'doubao-seed-2-0-pro-260215', reasoning_effort: null, vlm_parallelism: 64 },
    container: { cpu_quota: 30, memory_gib: 112, node: '32 核 128G' },
    latency: [lat('probe', 392, 3.1, 6.4, 11.2, 402, 4, 9), lat('endstate', 294, 2.7, 5.2, 9.8, 388, 2, 5), lat('arbitration', 78, 11.6, 27.3, 58.9, 391, 8, 3), lat('caption', 83, 4.2, 7.9, 12.5, 173), lat('llm', 39, 18.2, 31.4, 44.0, 118)],
    effective_concurrency: 11.4,
    stages: [
      { id: 'autolabel', wall_s: Math.round(46 * k), share: 0.05 },
      { id: 'numeric', wall_s: Math.round(9 * k), share: 0.01 },
      { id: 'frame', wall_s: Math.round(144 * k), share: 0.14 },
      { id: 'vlm', wall_s: Math.round(542 * k), share: 0.55 },
      { id: 'dedup', wall_s: Math.round(11 * k), share: 0.01 },
      { id: 'profile', wall_s: Math.round(131 * k), share: 0.13 },
      { id: 'report', wall_s: Math.round(6 * k), share: 0.01 },
    ],
    retries: { outer_attempts: Math.round(12 * k), rescued: Math.round(9 * k) },
    merge: { requests: 0, estimated_unmerged: 0 },
    redone_after_interruption: scope === 'subtask' ? 0 : 3,
  };
}

// ------------------------------------------------------------------ adjudication

const LABEL_CAPTIONS: Record<number, { caption: string; suggestion: string; priority: string }> = {
  29: { caption: 'pour rice into the green bowl', suggestion: 'pour rice into the green bowl', priority: '重点' },
  4: { caption: 'fold the cloth and put it on the table', suggestion: 'fold the cloth and put it on the table', priority: '高置信' },
  36: { caption: 'push the plate to the back of the table', suggestion: 'push the plate to the back of the table', priority: '高置信' },
  13: { caption: 'open the pot and take the lid off', suggestion: 'open the pot and take the lid off', priority: '人工复核档' },
  22: { caption: 'pick up the sponge and put it in the sink', suggestion: 'pick up the sponge and put it in the sink', priority: '人工复核档' },
};

function decision(id: number, ep: number, line: Decision['line'], value: Decision['decision'], at: number, applied = false, newLabel: string | null = null): Decision {
  return { id, episode_index: ep, line, decision: value, new_label: newLabel, note: null, decided_by: 'galbot', decided_at: at, applied };
}

export function seedDecisions(now: number): Decision[] {
  return [
    decision(1, 4, 'label', 'adopt_suggestion', now - 40 * MIN),
    decision(2, 13, 'label', 'keep_label', now - 38 * MIN),
    decision(3, 9, 'task_verdict', 'success', now - 35 * MIN),
    decision(4, 16, 'task_verdict', 'unsure', now - 33 * MIN),
  ];
}

/** The adjudication questions of the main task, before decisions are attached. */
export function baseQuestions(): Map<number, AdjudicationCard['questions']> {
  const out = new Map<number, AdjudicationCard['questions']>();
  for (const ep of LABEL_REVIEW) {
    const c = LABEL_CAPTIONS[ep];
    out.set(ep, [
      {
        line: 'label',
        source_module: 'skill_profile',
        reason: '这条标注和画面对不上：原始标注与画面描述归进了不同的技能族',
        annotation: taskText(ep).text,
        caption: c.caption,
        suggestion: c.suggestion,
        priority: c.priority,
        latest_decision: null,
      },
    ]);
  }
  for (const ep of VERDICT_REVIEW) {
    const q = {
      line: 'task_verdict' as const,
      source_module: 'task_success',
      reason: '证据不足，弃权：打分层拿不准 → 复核分歧 → 仲裁拿不准 → 转人工',
      annotation: taskText(ep).text,
      caption: null,
      suggestion: null,
      priority: null,
      latest_decision: null,
    };
    out.set(ep, [...(out.get(ep) ?? []), q]);
  }
  return out;
}

export function appealQuestions(): Map<number, AdjudicationCard['questions']> {
  const out = new Map<number, AdjudicationCard['questions']>();
  const chains: Record<number, string> = {
    6: '打分层失败候选 → 3 路复核一致判未完成 → 判废（完成度末值 0.12）',
    11: '打分层全程无进展 → 3 路复核一致判未完成 → 判废（完成度全程 ≤ 0.08）',
    23: '打分层成功候选（弱）→ 2 路复核判未完成 → 仲裁 2 路一致未完成 → 判废',
    38: '打分层峰值后回落 → 2 路复核判未完成 → 仲裁 3 路一致未完成 → 判废',
    45: '打分层失败候选 → 3 路复核一致判未完成 → 判废（完成度末值 0.09）',
  };
  for (const ep of TS_REJECTS) {
    out.set(ep, [{ line: 'reject_appeal', source_module: 'task_success', reason: chains[ep], annotation: taskText(ep).text, caption: null, suggestion: null, priority: null, latest_decision: null }]);
  }
  // D42: a dedup reject can be appealed too; the card names the episode it duplicates.
  out.set(44, [
    {
      line: 'reject_appeal',
      source_module: 'dedup',
      reason: '动作数据与视频字节级完全一致；保留遍历顺序里先出现的那一条',
      duplicate_of: 43,
      annotation: taskText(44).text,
      caption: null,
      suggestion: null,
      priority: null,
      latest_decision: null,
    },
  ]);
  return out;
}

// ------------------------------------------------------------------ logs and previews

export function mainLogs(now: number): LogLine[] {
  const t0 = now - 2 * HOUR;
  const out: LogLine[] = [];
  const push = (dt: number, stageId: string, level: LogLine['level'], msg: string, episode: number | null = null, subtask: string | null = null) =>
    out.push({ ts: t0 + dt, stage: stageId, level, msg, episode_index: episode, subtask_id: subtask });
  push(0, 'system', 'info', 'task created; pre-start checks passed (input read, output write probe, vlm minimal call)');
  push(1500, 'system', 'info', 'source manifest frozen: 204 objects, 1.42 GiB');
  push(3000, 'autolabel', 'info', 'autolabel: 22 episodes without task text');
  for (let i = 0; i < 22; i += 1) push(4000 + i * 1800, 'autolabel', 'info', `caption episode ${i * 2 + 1}: ok (1 request)`, i * 2 + 1);
  push(48_000, 'numeric', 'info', 'check --modules timestamp_check,motion_quality on 50 episodes');
  push(49_000, 'numeric', 'warn', 'episode 18 too short (0.5s); marked as fragment', 18);
  push(58_000, 'numeric', 'info', 'numeric stage done: 49 survivors');
  for (let i = 0; i < 49; i += 1) push(60_000 + i * 2900, 'frame', 'info', `decode + visual_quality + video_action_sync episode ${i < 18 ? i : i + 1}`, i < 18 ? i : i + 1);
  push(204_000, 'frame', 'info', 'frame stage done: 49 survivors');
  for (let i = 0; i < 49; i += 1) {
    const ep = i < 18 ? i : i + 1;
    for (const [k, kind] of ['probe', 'endstate', 'arbitration'].entries()) push(206_000 + i * 11_000 + k * 3000, 'vlm', 'debug', `episode ${ep}: ${kind} request sent`, ep);
    push(206_000 + i * 11_000 + 9000, 'vlm', ep === 7 || ep === 31 ? 'error' : 'info', ep === 7 || ep === 31 ? `episode ${ep}: arbitration request timed out after 60s (attempt 3/3); marked as error` : `task_success episode ${ep}: done`, ep);
  }
  push(6 * MIN, 'system', 'warn', 'system pause: daemon upgrade v2.0.3 -> v2.0.4; stopping after in-flight episodes');
  push(8 * MIN, 'system', 'info', 'system resume: continuing from checkpoint (3 in-flight episodes will be re-requested)');
  push(15 * MIN, 'profile', 'error', 'skill_profile: 429 QuotaExceeded x20, circuit open, exit 4');
  push(17 * MIN, 'system', 'info', 'main run finished: completed_with_errors (revision 1)');
  push(62 * MIN, 'system', 'info', 'subtask retry #1 started', null, 'sub_retry1');
  push(63 * MIN, 'vlm', 'error', 'episode 7: arbitration request timed out after 60s (attempt 3/3); marked as error', 7, 'sub_retry1');
  push(63 * MIN + 2000, 'vlm', 'error', 'episode 31: arbitration request timed out after 60s (attempt 3/3); marked as error', 31, 'sub_retry1');
  push(68 * MIN, 'profile', 'info', 'skill_profile: 41 captions, 6 families, 5 label divergences', null, 'sub_retry1');
  push(69 * MIN, 'system', 'info', 'subtask retry #1 finished (revision 2)', null, 'sub_retry1');
  return out.sort((a, b) => a.ts - b.ts);
}

export function episodePreviews(uri: string, count: number): EpisodePreview[] {
  const p = profileFor(uri);
  const cams = p.cameras.length ? p.cameras : ['front'];
  return Array.from({ length: count }, (_, i) => ({
    index: i,
    length_s: Number((8 + ((i * 7) % 17) + 0.4).toFixed(1)),
    task: i < p.withTask ? TASK_TEXTS[i % TASK_TEXTS.length] : '',
    task_source: i < p.withTask ? ('原始标注' as const) : ('无' as const),
    cameras: cams.map((name) => ({
      name,
      url: `https://${uri.split('/')[2]}.tos-cn-beijing.volces.com/${uri.split('/').slice(3).join('/')}/videos/chunk-000/${name}/episode_${String(i).padStart(6, '0')}.mp4?X-Tos-Signature=mock`,
      from_ts: 0,
      to_ts: Number((8 + ((i * 7) % 17) + 0.4).toFixed(1)),
    })),
  }));
}
