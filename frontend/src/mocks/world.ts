// The mock world: one consistent set of fixtures, typed with the generated C4 types and
// validated against the OpenAPI schemas in src/mocks/contract.test.ts. The storyline follows
// the approved mockups (frontend/mockups/README.md): «droid 前 50 条质检» is the task that the
// detail, report and adjudication pages tell the whole story of.
//
// That task runs the two blocks side by side (design doc 17 §3; the console shows their stages in groups, design
// doc 26 §2): every selected module judges every episode and reports findings (C2 2.0), the policy grades them
// into the verdicts, the report has the findings statistics of every module, the episode list the items and levels
// of each episode's findings, the questions name the finding codes they ask about.
import { cameraRows, canGenerate, shortName } from '../lib/declaration';
import { db } from './db';
import modulesJson from '../../../docs/contracts/modules.json';
import type {
  AdjudicationCard,
  Credential,
  DatasetCheck,
  DatasetDetail,
  Decision,
  EpisodeFinding,
  EpisodePreview,
  EpisodeView,
  Finding,
  LogLine,
  ModuleRegistry,
  ModuleSpec,
  ModuleState,
  Perf,
  PipelineEpisode,
  Plan,
  PreflightResult,
  Report,
  ResultRecord,
  StageProgress,
  Subtask,
  SyncCurves,
  Task,
  TaskEpisode,
  TaskState,
  TimelineEntry,
  UsageRow,
  UsageTotals,
  VlmBackend,
} from '../api/types';
import { WAREHOUSE_MAPPING, mappingInfoOf, vizStatusOf } from './vizWorld';

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

export function stage(id: StageProgress['id'], state: StageProgress['state'], done: number, total: number, elapsed: number | null, extra: Partial<StageProgress> = {}): StageProgress {
  return { id, state, done, total, elapsed_s: elapsed, eta_s: null, ...extra };
}

/** A finished stage's streaming activity: the last batch, the time per episode, its run (minutes ago). */
function lane(now: number, last: number, mean: number, from: number, to: number): NonNullable<StageProgress['pipeline']> {
  return {
    inflight: 0, queued: 0, capacity: 8, dispatches: Math.ceil(last / 8),
    recent: [{ number: Math.ceil(last / 8), count: 2, episodes: [last - 2, last - 1], at: now - to * MIN }],
    started_at: now - from * MIN, finished_at: now - to * MIN, updated_at: now - to * MIN,
    processing: { count: last, total_s: Math.round(last * mean), mean_s: mean, min_s: mean / 2, max_s: mean * 3 },
    busy: [{ start: now - from * MIN, end: now - to * MIN }], held_by_downstream: false,
  };
}

/** The stages a running main run has not reached yet: the Daemon lists the whole plan from the start. */
function notYet(...ids: StageProgress['id'][]): StageProgress[] {
  return ids.map((id) => stage(id, 'pending', 0, 0, null));
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
      is_default: false,
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
      is_default: false,
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
      is_default: false,
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
      is_default: false,
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
        { id: 'vm_pro', is_default: true, model_name: 'doubao-seed-2-0-pro-260215', reasoning_effort: null, max_concurrency: null, capabilities: { vision: true, reasoning_effort_levels: [...DOUBAO_LEVELS] }, source: 'listed' },
        { id: 'vm_lite', is_default: false, model_name: 'doubao-seed-2-0-lite-260215', reasoning_effort: 'minimal', max_concurrency: null, capabilities: { vision: true, reasoning_effort_levels: [...DOUBAO_LEVELS] }, source: 'listed' },
        { id: 'vm_16', is_default: false, model_name: 'doubao-seed-1-6-251015', reasoning_effort: null, max_concurrency: 32, capabilities: { vision: true, reasoning_effort_levels: [...DOUBAO_LEVELS] }, source: 'listed' },
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
        { id: 'vm_ep', is_default: false, model_name: 'ep-20260915173012-x7k2p', reasoning_effort: null, max_concurrency: null, capabilities: { vision: null, reasoning_effort_levels: [...ALL_LEVELS] }, source: 'manual' },
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
        { id: 'vm_qwen', is_default: false, model_name: 'Qwen2.5-VL-72B-Instruct', reasoning_effort: null, max_concurrency: null, capabilities: { vision: true, reasoning_effort_levels: [...DOUBAO_LEVELS] }, source: 'listed' },
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
  fps: number | null; // null: mcap, whose time axis is the action topic's log_time
  robotType: string | null;
  withTask: number;
  missing: string[];
  profile: { matched: string; by: string } | null;
}

export const DATASET_PROFILES: DatasetProfile[] = [
  {
    // F5.16 (D-E17): a DROID-like Franka dataset (the DEMO's dataset2), its record mapping drafted from info.json
    uri: 'tos://pai-kit-datasets/lerobot/eef_ds2_lr3',
    source: 'tos',
    name: 'eef_ds2_lr3',
    format: { kind: 'lerobot', version: 'v3', supported: true, detail: 'LeRobot v3, 7 episodes, 2 cameras' },
    episodes: 7,
    cameras: ['exterior_1_left', 'exterior_2_left'],
    fps: 15,
    robotType: 'Franka',
    withTask: 7,
    missing: [],
    profile: null,
  },
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
    // D44: mcap and lance are read since F6.5
    uri: 'tos://pai-kit-datasets/raw/warehouse_mcap',
    source: 'tos',
    name: 'warehouse_mcap',
    format: { kind: 'mcap', version: null, supported: true, detail: "mcap, 12 episodes, 2 cameras; time axis from the action topic's log_time" },
    episodes: 12,
    cameras: ['front', 'wrist'],
    fps: null,
    robotType: 'franka',
    withTask: 12,
    missing: [],
    profile: null,
  },
  {
    uri: 'tos://pai-kit-datasets/raw/pusht_lance',
    source: 'tos',
    name: 'pusht_lance',
    format: { kind: 'lance', version: 'v3', supported: true, detail: 'LeRobot v3.0 in lance tables (lerobot-lance-convert), 206 episodes, 1 camera' },
    episodes: 206,
    cameras: ['image'],
    fps: 10,
    robotType: 'pusht',
    withTask: 206,
    missing: [],
    profile: null,
  },
  {
    // design doc 19 §2: RH20T's ten cameras (cam_<serial>), more than the phase-one 3 × 3 grid holds; not seeded,
    // the tests that need it add it
    uri: 'tos://pai-kit-datasets/rh20t/rh20t_cfg1',
    source: 'tos',
    name: 'rh20t_cfg1',
    format: { kind: 'lerobot', version: 'v2', supported: true, detail: 'LeRobot v2, 30 episodes, 10 cameras' },
    episodes: 30,
    cameras: ['cam_036422060215', 'cam_037522062165', 'cam_038522062288', 'cam_045322071843', 'cam_104122061850',
      'cam_104122062295', 'cam_104122062823', 'cam_104122063550', 'cam_104422070011', 'cam_f0172289'],
    fps: 10,
    robotType: 'ur5',
    withTask: 30,
    missing: [],
    profile: null,
  },
  {
    uri: 'tos://pai-kit-datasets/raw/warehouse_rrd',
    source: 'tos',
    name: 'warehouse_rrd',
    format: { kind: 'rrd', version: null, supported: false, detail: 'only LeRobot v2/v3, mcap and lance (lerobot-lance-convert >= 0.3.0) are supported; detected .rrd (rerun) files' },
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

/** An address the mock world takes for an mcap dataset (GenRobot UMI, ABC-130k, RoboMIND, anything …mcap…). */
export const MCAP_URI = /mcap|genrobot|abc130k|robomind/i;

export function profileFor(uri: string): DatasetProfile {
  const clean = uri.replace(/\/+$/, '');
  const hit = DATASET_PROFILES.find((p) => p.uri === clean);
  if (hit) return hit;
  const name = clean.split('/').pop() || 'dataset';
  if (MCAP_URI.test(clean)) {
    // design doc 18 §6: GenRobot reads with the site's defaults (UMI); ABC-130k's and RoboMIND's topics
    // are their own, so the checks call them unsupported until a mapping names them - the visualizer still takes them as mcap
    const robomind = /robomind/i.test(clean);
    const abc = robomind || /abc/i.test(clean);
    return {
      uri: clean,
      source: 'tos',
      name,
      format: abc
        ? { kind: 'mcap', version: null, supported: false, detail: 'mcap episode files found, but no required action topic (/action) - confirm a field mapping' }
        : { kind: 'mcap', version: null, supported: true, detail: 'mcap, 4 episodes, 2 cameras; UMI layout' },
      episodes: 4,
      cameras: robomind ? ['top-camera'] : abc ? ['top-left-camera', 'left-wrist-camera'] : ['robot0_sensor_camera0_compressed', 'robot1_sensor_camera0_compressed'],
      fps: null,
      robotType: abc ? null : 'das_gripper',
      withTask: abc ? 4 : 0,
      missing: [],
      profile: null,
    };
  }
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

type EefCamera = NonNullable<PreflightResult['modules'][number]['cameras']>[number];

/** EEF's parameters that apply (design doc 25 §4.2, as the CLI's): the gripper reference unless every camera is known
 * not to be a third-person one (wrist, moving) - a camera whose mount nobody declared may be one. */
function eefApplicable(m: (typeof registry.modules)[number], cameras: EefCamera[]): string[] {
  const props = ((m.param_schema as { properties?: Record<string, { 'x-applies-when'?: string }> }).properties ?? {}) as Record<string, { 'x-applies-when'?: string }>;
  const third = !cameras.length || cameras.some((c) => c.mount !== 'wrist' && c.mount !== 'moving');
  return Object.entries(props)
    .filter(([, x]) => x['x-applies-when'] !== 'third_person_camera' || third)
    .map(([k]) => k);
}

/** Without a model backend the module still runs (design doc 25 D84): a reminder, as the CLI's note. */
function eefNotes(opts: { vlmBackend?: string }, notes: string[] = []): string[] {
  return opts.vlmBackend ? notes : [...notes, "vlm_backend_missing: no VLM backend chosen - the module runs on the CPU's measurement alone"];
}

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
        reason: `only LeRobot v2/v3, mcap and lance (lerobot-lance-convert >= 0.3.0) are supported; detected ${p.format.kind} files`,
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
    if (needs.includes('eef_input') && p.format.kind === 'lance') {
      // like the CLI (D44, F5.13): EEF-video consistency reads LeRobot videos and mcap image topics
      return {
        id: m.id,
        availability: 'unsupported',
        reason: `EEF-video consistency reads LeRobot and mcap datasets only, not ${p.format.kind}`,
        reason_code: 'format_unsupported_by_module',
        reason_args: { format: p.format.kind },
      };
    }
    if (needs.includes('eef_input')) {
      // like the CLI: the file is optional only where the platform can compute the trajectory - a handheld gripper's
      // data (design doc 24, design doc 22 §5.4); any other dataset is offered and asks for the upload on screen 2
      if (/umi|das_gripper/i.test(p.robotType ?? '')) {
        // two wrist cameras, one per hand: no third-person camera, no gripper reference to offer
        const cameras: EefCamera[] = p.cameras.map((c, i) => ({ source: `observation.images.${c}`, camera_id: c, mount: 'wrist', owner: `robot${i}`, drawable: true, reason: null }));
        return {
          id: m.id,
          availability: 'available',
          notes: eefNotes(opts, ["trajectory generated from the dataset's state and camera calibration"]),
          trajectory_source: { kind: 'generate' },
          cameras,
          applicable_params: eefApplicable(m, cameras),
        };
      }
      // design doc 25 §4.1: a confirmed declaration that is enough generates the trajectory; a DROID-like dataset
      // records the pose and lacks the cameras' intrinsics (declaration_incomplete); the rest need the upload
      const ds = db.datasets.find((x: { uri: string }) => x.uri === p.uri);
      const stored = ds ? db.declarations.get(ds.id) : undefined;
      const sources = p.cameras.map((c) => `observation.images.${c}`);
      const declared = (doc: Parameters<typeof cameraRows>[0]): EefCamera[] =>
        cameraRows(
          doc,
          p.cameras.map((c) => ({ source: `observation.images.${c}`, name: c, width: null, height: null })),
        ).map((r) => ({ source: r.source, camera_id: r.calibration?.camera_id ?? shortName(r.source), mount: r.calibration?.mount ?? null, owner: r.calibration?.owner ?? null, drawable: r.reason === null, reason: r.reason }));
      if (stored && canGenerate(stored.doc, sources)) {
        const cameras = declared(stored.doc);
        return {
          id: m.id,
          availability: 'available',
          notes: eefNotes(opts),
          trajectory_source: { kind: 'generate', declaration: { drafted: false } },
          cameras,
          applicable_params: eefApplicable(m, cameras),
        };
      }
      if (p.name.startsWith('eef_ds2') || p.name.startsWith('droid')) {
        const missing = sources.map((src) => ({ field: `calibration.cameras.${src}.intrinsics`, code: 'intrinsics_missing' }));
        return {
          id: m.id,
          availability: 'needs_input',
          reason: 'the dataset records the pose, but its declaration lacks the cameras\' intrinsics: complete the declaration (the dataset page) or upload a trajectory.json',
          reason_code: 'declaration_incomplete',
          reason_args: { missing },
          input_hint: { field: 'trajectory_json' },
          trajectory_source: { kind: 'missing_declaration', declaration: { drafted: !stored }, missing },
          cameras: sources.map((src) => ({ source: src, camera_id: shortName(src), mount: 'fixed_external', owner: null, drawable: false, reason: 'intrinsics_missing' })),
          applicable_params: eefApplicable(m, [{ camera_id: 'x', mount: 'fixed_external', drawable: false }]),
        };
      }
      return {
        id: m.id,
        availability: 'needs_input',
        reason: "the platform cannot compute the trajectory from this dataset (it records no end-effector poses with the cameras' calibration): upload a trajectory.json",
        reason_code: 'trajectory_missing',
        input_hint: { field: 'trajectory_json' },
        trajectory_source: { kind: 'missing_pose' },
        cameras: [],
        applicable_params: eefApplicable(m, []),
      };
    }
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
      const notes = p.episodes - p.withTask > 0 ? [`${p.episodes - p.withTask} episodes have no task text; task_success will not judge them, their cameras are still checked for picture defects`] : [];
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

/** C4 ``DatasetFormat`` of a preflight format, like the Daemon's ``dataset_format`` (D44). */
export function datasetFormatOf(f: PreflightResult['format']): DatasetDetail['format'] {
  if (!f.supported) return 'unsupported';
  if (f.kind === 'mcap' || f.kind === 'lance') return f.kind;
  return f.version === 'v3' ? 'lerobot_v3' : 'lerobot_v2';
}

export function datasetDetail(
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
    format: datasetFormatOf(p.format),
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
    viz: vizStatusOf(datasetFormatOf(p.format), id === 'ds_mcap'),
    viz_mapping: mappingInfoOf(datasetFormatOf(p.format), id === 'ds_mcap' ? { mapping: WAREHOUSE_MAPPING, version: 1, updatedAt: now - 4 * DAY } : undefined),
    declaration: { state: 'none', version: 0, updated_at: null, name: null, layers: [], assumed: 0, suspects: 0 },
    annotations: null,
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

// the mock tasks select the eight that need no file; the EEF module (opt-in, needs a file) and the camera defects,
// which ride on task_success and are never selected, are not among them
const SELECTABLE_MODULES = registry.modules.filter((m) => !m.rides_on && !(m.needs as string[]).includes('eef_input'));
export const ALL_MODULES = SELECTABLE_MODULES.map((m) => m.id);
const NON_VLM = SELECTABLE_MODULES.filter((m) => !(m.needs as string[]).includes('vlm')).map((m) => m.id);

export interface TaskSeed {
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
  resultRev?: number;
  pauseReason?: Task['pause_reason'];
  stateReason?: string | null;
  vlm?: boolean;
  vlmChoice?: { backend: string; model: string };
  note?: string | null;
  runId?: string | null;
  embodiment?: string | null;
}

export function buildTask(s: TaskSeed): Task {
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
    vlm: vlm ? { backend: s.vlmChoice?.backend ?? 'ark-prod', model: s.vlmChoice?.model ?? 'doubao-seed-2-0-pro-260215', reasoning_effort: null } : null,
    params: { start_now: s.state !== 'created', vlm_retry: 3, vlm_hedge: true, clips: false },
    source: started ? { objects: 204, bytes: 1_520_331_122, digest: DIGEST2 } : null,
    progress: { stages: s.stages },
    modules: moduleStates(s.selected, s.modules ?? {}, s.total),
    summary: s.summary ?? null,
    result_rev: s.resultRev ?? (terminal ? 1 : 0),
    usage: s.usage ?? ZERO_USAGE,
    pending_adjudication: s.pending ?? 0,
    active_subtask: null,
    created_at: s.created,
    updated_at: s.created + 60_000,
    started_at: started,
    finished_at: terminal ? s.created + 25 * MIN : null,
    deleted_at: null,
    links: [{ rel: 'task', title: 'Open task', url: `/tasks/${s.id}`, absolute: false }],
  };
}

/** A finished two-block run (design doc 17 §3): the CPU block's stages and the model's, then the steps after both. */
const allDone = (total: number, withModel = true): StageProgress[] => [
  stage('numeric', 'succeeded', total, total, 3, { block: 'cpu' }),
  stage('frame', 'succeeded', total, total, 144, { block: 'cpu' }),
  stage('duplicate', 'succeeded', total, total, 11, { block: 'cpu' }),
  ...(withModel ? [stage('vlm', 'succeeded', total, total, 408, { block: 'vlm' })] : []),
  stage('final', 'succeeded', 1, 1, 1),
  stage('report', 'succeeded', 1, 1, 6),
  stage('verify', 'succeeded', 1, 1, 9),
];

/** The steps after both blocks, not reached yet. */
const AFTER = ['final', 'report', 'verify'] as const;

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
      stages: [],
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
        // streaming pipeline activity (the task page's PipelineActivity): per layer what is in flight,
        // the latest batch handed on and the time per episode; `busy` the spans with episodes out
        // (the rest of a layer's run is waiting, C4 1.18)
        stage('integrity', 'succeeded', 640, 640, 190, { block: 'cpu',
          pipeline: { inflight: 0, queued: 0, capacity: 8, dispatches: 80, recent: [{ number: 80, count: 8, episodes: [632, 633, 634, 635, 636, 637, 638, 639], at: now - 47 * MIN }],
            started_at: now - 50 * MIN, finished_at: now - 47 * MIN, updated_at: now - 47 * MIN, processing: { count: 640, total_s: 1696, mean_s: 2.65, min_s: 0.8, max_s: 3.66 },
            busy: [{ start: now - 50 * MIN, end: now - 47 * MIN }], held_by_downstream: false },
        }),
        stage('numeric', 'succeeded', 640, 640, 5, { block: 'cpu',
          pipeline: { inflight: 0, queued: 0, capacity: 8, dispatches: 80, recent: [{ number: 80, count: 8, episodes: [632, 633, 634, 635, 636, 637, 638, 639], at: now - 46 * MIN }],
            started_at: now - 49 * MIN, finished_at: now - 46 * MIN, updated_at: now - 46 * MIN, processing: { count: 640, total_s: 38.4, mean_s: 0.06, min_s: 0.02, max_s: 0.41 },
            busy: [0, 1, 2, 3, 4, 5].map((k) => ({ start: now - 49 * MIN + k * 30_000, end: now - 49 * MIN + k * 30_000 + 6_000 })), held_by_downstream: false },
        }),
        stage('frame', 'succeeded', 640, 640, 1033, { block: 'cpu',
          pipeline: { inflight: 0, queued: 0, capacity: 8, dispatches: 80, recent: [{ number: 80, count: 8, episodes: [632, 633, 634, 635, 636, 637, 638, 639], at: now - 21 * MIN }],
            started_at: now - 48 * MIN, finished_at: now - 21 * MIN, updated_at: now - 21 * MIN, processing: { count: 640, total_s: 8320, mean_s: 13, min_s: 6.2, max_s: 31.5 },
            busy: [{ start: now - 48 * MIN, end: now - 40 * MIN }, { start: now - 37 * MIN, end: now - 30 * MIN }, { start: now - 26 * MIN, end: now - 21 * MIN }], held_by_downstream: false },
        }),
        stage('duplicate', 'succeeded', 640, 640, 1020, { block: 'cpu',
          pipeline: { inflight: 0, queued: 0, capacity: 1, dispatches: 80, recent: [{ number: 80, count: 8, episodes: [632, 633, 634, 635, 636, 637, 638, 639], at: now - 21 * MIN + 2000 }],
            started_at: now - 48 * MIN + 20_000, finished_at: now - 21 * MIN + 3000, updated_at: now - 21 * MIN + 3000, processing: { count: 640, total_s: 13, mean_s: 0.02, min_s: 0.01, max_s: 0.4 },
            busy: [{ start: now - 48 * MIN + 20_000, end: now - 21 * MIN + 3000 }], held_by_downstream: false },
        }),
        stage('vlm', 'running', 410, 640, 1102, { eta_s: 1440, block: 'vlm',
          pipeline: { inflight: 32, queued: 198, capacity: 32, dispatches: 52, recent: [{ number: 52, count: 8, episodes: [441, 442, 443, 444, 445, 446, 447, 448], at: now - 4000 }],
            started_at: now - 47 * MIN, finished_at: null, updated_at: now - 4000, processing: { count: 410, total_s: 17630, mean_s: 43, min_s: 12.8, max_s: 118.6 },
            busy: [{ start: now - 47 * MIN, end: null }], held_by_downstream: false },
        }),
        ...notYet(...AFTER),
      ],
      modules: {
        kinematic_limits: { availability: 'unsupported', unavailable_reason: '机器人型号 umi_dual_handheld_gripper 不在规格库，整项跳过', state: 'skipped' },
        task_success: { state: 'running', episodes_total: 640 },
        visual_quality: { episodes_total: 640 },
        video_action_sync: { episodes_total: 640 },
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
      selected: ALL_MODULES.filter((id) => id !== 'kinematic_limits' && id !== 'data_integrity'),
      episodes: { mode: 'head', n: 50 },
      total: 50,
      runId: '20260920-130514',
      stages: [
        // the episodes' stages report their streaming activity, as the Daemon's two blocks do (C4 1.18)
        stage('numeric', 'succeeded', 50, 50, 9, { block: 'cpu', pipeline: lane(now, 50, 0.18, 120, 112) }),
        stage('frame', 'succeeded', 50, 50, 146, { block: 'cpu', pipeline: lane(now, 50, 2.9, 120, 96) }),
        stage('duplicate', 'succeeded', 50, 50, 146, { block: 'cpu', pipeline: lane(now, 50, 0.02, 120, 96) }),
        stage('vlm', 'completed_with_errors', 50, 50, 560, { block: 'vlm', note: '2 条出错（ep 7、ep 31），暂不交付，等待补跑', pipeline: lane(now, 50, 11.2, 119, 108) }),
        stage('final', 'succeeded', 1, 1, 1),
        stage('report', 'succeeded', 1, 1, 6),
        stage('verify', 'succeeded', 1, 1, 9),
      ],
      modules: {
        kinematic_limits: { availability: 'needs_input', unavailable_reason: '预检未读到机器人型号，创建时选择跳过', state: 'skipped' },
        timestamp_check: { elapsed_s: 2 },
        motion_quality: { elapsed_s: 7 },
        visual_quality: { elapsed_s: 144 },
        video_action_sync: { elapsed_s: 144 },
        task_success: { state: 'completed_with_errors', episodes_error: 2, elapsed_s: 560, error: null },
        duplicate_check: { elapsed_s: 11 },
      },
      summary: { total: 50, passed: 42, rejected: 6, held: 2, review: 10, pass_rate: 0.84 },
      usage: usageTotals(2_010_000, 67_500, 42_900, 931_800, 886, 41),
      pending: 10,
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
      stages: [
        stage('numeric', 'succeeded', 430, 430, 4, { block: 'cpu' }),
        stage('frame', 'succeeded', 430, 430, 700, { block: 'cpu' }),
        stage('duplicate', 'succeeded', 430, 430, 690, { block: 'cpu' }),
        stage('vlm', 'running', 180, 430, 900, { block: 'vlm' }),
        ...notYet(...AFTER),
      ],
      modules: { task_success: { state: 'running' } },
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
      stages: [
        stage('numeric', 'succeeded', 3200, 3200, 30, { block: 'cpu' }),
        stage('frame', 'running', 1216, 3200, 4100, { block: 'cpu' }),
        stage('duplicate', 'running', 1210, 3200, 4080, { block: 'cpu' }),
        stage('vlm', 'running', 960, 3200, 4100, { block: 'vlm' }),
        ...notYet(...AFTER),
      ],
      modules: { visual_quality: { state: 'running' }, video_action_sync: { state: 'running' }, duplicate_check: { state: 'running' }, task_success: { state: 'running' } },
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
      stages: [stage('numeric', 'succeeded', 20, 20, 1, { block: 'cpu' }), stage('frame', 'failed', 3, 20, 12, { block: 'cpu' }), stage('duplicate', 'pending', 0, 20, null, { block: 'cpu' }), stage('vlm', 'succeeded', 20, 20, 40, { block: 'vlm' })],
      modules: { visual_quality: { state: 'failed', error: 'InvalidAccessKeyId' }, video_action_sync: { state: 'failed' }, duplicate_check: { state: 'pending', episodes_total: 0 } },
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
      stages: [stage('numeric', 'succeeded', 480, 480, 5, { block: 'cpu' }), stage('frame', 'succeeded', 480, 480, 900, { block: 'cpu' }), stage('duplicate', 'succeeded', 480, 480, 880, { block: 'cpu' }), stage('vlm', 'running', 120, 480, 600, { block: 'vlm' })],
      modules: { task_success: { state: 'running', episodes_total: 120 } },
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

/** The modules the main task selected: the per-episode checks and the duplicate check, without data integrity (not asked for)
 * and kinematic limits (no robot type). */
export const MAIN_MODULES = ALL_MODULES.filter((id) => id !== 'kinematic_limits' && id !== 'data_integrity');

export function mainSubtasks(now: number): Subtask[] {
  return [
    {
      id: 'sub_retry1',
      task_id: MAIN_TASK,
      kind: 'retry',
      scope: { modules: ['task_success'], episodes: 'errors' },
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
// relabels were judged again with their new task text (D39).
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
      scope: {},
      state: 'succeeded',
      state_reason: null,
      progress: null,
      created_at: at,
      started_at: at,
      finished_at: at + 6 * MIN,
      result_rev: 2,
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
    { at, kind: 'subtask_started', text: '执行裁决：应用 12 条裁决，其中 4 条改了标，按新标注重判任务成败。', state: null, subtask_id: 'sub_apply1', revision: null },
    { at: at + 6 * MIN, kind: 'subtask_finished', text: '执行裁决结束：4 条改标重判完成。', state: 'succeeded', subtask_id: 'sub_apply1', revision: 2 },
    { at: at + 6 * MIN + 1000, kind: 'revision', text: '结果版本 r0002：判决更新。', state: null, subtask_id: 'sub_apply1', revision: 2 },
  ];
}

export function mainTimeline(now: number): TimelineEntry[] {
  const t0 = now - 2 * HOUR;
  return [
    { at: t0, kind: 'created', text: '创建并开始。开始前检查通过：数据集能读（readonly-tos）、交付目录能写（prod-tos）、模型能调通（ark-prod）。', state: 'queued', subtask_id: null, revision: null },
    { at: t0 + 2000, kind: 'started', text: '结果目录 20260920-130514，源文件清单 204 个对象已固化。', state: 'running', subtask_id: null, revision: null },
    { at: t0 + 6 * MIN, kind: 'system_pause', text: 'Daemon 升级（v2.0.3 → v2.0.4），模型检查停在 21 / 49 条。不是用户操作，不需要处理。', state: 'paused', subtask_id: null, revision: null },
    { at: t0 + 8 * MIN, kind: 'system_resume', text: '新版本起来后自动续跑。中断时在飞的 3 条（ep 26、27、30）重新调用了一次，服务端可能已为丢掉的那次计费，这部分 Token 统计不到。', state: 'running', subtask_id: null, revision: null },
    { at: t0 + 17 * MIN, kind: 'finished', text: '主流程结束：任务成败判定整体失败（方舟返回 429，额度用尽，连续 20 条失败后熔断），CPU 检查全部完成。', state: 'completed_with_errors', subtask_id: null, revision: 1 },
    { at: t0 + 17 * MIN + 1000, kind: 'revision', text: '结果版本 r0001：通过 0 · 拒绝 1 · 待补跑 49', state: null, subtask_id: null, revision: 1 },
    { at: now - 58 * MIN, kind: 'subtask_started', text: '重试 #1：任务成败判定（出错的 50 条）。开始前检查通过，额度已恢复。', state: null, subtask_id: 'sub_retry1', revision: null },
    { at: now - 51 * MIN, kind: 'subtask_finished', text: '重试 #1 结束：任务成败判定补齐 48 条；2 条（ep 7、ep 31）的模型请求仍超时。', state: 'completed_with_errors', subtask_id: 'sub_retry1', revision: 2 },
    { at: now - 51 * MIN + 1000, kind: 'revision', text: '结果版本 r0002：通过 42 · 拒绝 6 · 待补跑 2', state: null, subtask_id: 'sub_retry1', revision: 2 },
  ];
}

/** The main task's plan: the two blocks side by side (design doc 17 §3), every stage on every selected episode. */
export function mainPlan(): Plan {
  return {
    schema_version: '2.0',
    vlm_parallelism: 64,
    limits: { cpu_concurrency: { value: 30, bound_by: 'planner' }, vlm_parallelism: { value: 64, bound_by: 'model' } },
    stages: [
      { id: 'numeric', block: 'cpu', kind: 'cpu', command: 'check', concurrency: 30, modules: ['timestamp_check', 'motion_quality', 'declared_steps', 'step_detection'], episodes: 'selected' },
      { id: 'frame', block: 'cpu', after: 'numeric', kind: 'cpu', command: 'check', concurrency: 30, modules: ['visual_quality', 'video_action_sync'], episodes: 'selected' },
      { id: 'duplicate', block: 'cpu', after: 'frame', kind: 'cpu', command: 'check', concurrency: 1, modules: ['duplicate_check'], episodes: 'selected' },
      { id: 'vlm', block: 'vlm', kind: 'vlm', command: 'check', modules: ['task_success', 'camera_defects'], episodes: 'selected', gates: { episode: 32, probe: 64 }, merge: { strategy: 'none', groups: [] } },
      { id: 'final', kind: 'aggregate', command: 'aggregate', phase: 'final' },
    ],
    // as backend/curation/planner/estimates.py writes them (English; the page translates)
    estimates: {
      vlm_requests: 800,
      wall_clock_s: 640,
      notes: ['rough estimate: every selected episode goes through both blocks, which run side by side; 22.7 s per request at 67% gate use (image-request baseline from v1, 2026-09-07; video latency is not calibrated)'],
    },
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
  // the main run's requests before the quota ran out, then the retry's (one request per episode, D71)
  const actual = [row('', 'task_success', 'probe', 210_000, 6_500, 4_100, 98_000, 92, 20), row('sub_retry1', 'task_success', 'probe', 1_800_000, 61_000, 38_800, 833_800, 794, 21)];
  const sum = (k: keyof UsageTotals) => actual.reduce((a, r) => a + r[k], 0);
  const totals = usageTotals(sum('prompt_tokens'), sum('completion_tokens'), sum('reasoning_tokens'), sum('cached_tokens'), sum('requests'), sum('requests_unknown_usage'));
  return { totals, actual, attributed: actual };
}

// Episodes of the main task (C2 2.0, design doc 17): every selected module judges every episode (D57). ep 18 a
// fragment; five episodes task success judged failed, ten it could not decide (a person is asked); two whose model
// request kept timing out (held for a retry); ep 44 a byte copy of ep 43, reported only (D87). The first run (r0001)
// lost task success to the model's quota: every episode but the fragment is held there.
export const TS_REJECTS = [6, 11, 23, 38, 45];
export const HELD = [7, 31];
/** Episodes task success could not decide (the task_verdict question): the cameras disagreed with a failure … */
export const SPLIT_REVIEW = [4, 13, 22, 29, 36];
/** … or the model could not tell. */
export const VERDICT_REVIEW = [...SPLIT_REVIEW, 9, 16, 33, 40, 47].sort((a, b) => a - b);
/** ep 44 is a copy of ep 43: the duplicate check names each in the other's record and rejects neither (D87). */
export const SAME_AS: Record<number, number[]> = { 43: [44], 44: [43] };

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

export function taskText(ep: number): { text: string; source: '原始标注' } {
  return { text: TASK_TEXTS[ep % TASK_TEXTS.length], source: '原始标注' };
}

/** The episode's list; `quotaRun`: the first run, which lost task success to the quota. */
export function episodeList(ep: number, quotaRun = false): EpisodeView['list'] {
  if (ep === 18) return 'reject';
  if (quotaRun || HELD.includes(ep)) return 'held';
  if (TS_REJECTS.includes(ep)) return 'reject';
  return 'passed';
}

export const CAMERAS = ['exterior_image_1_left', 'exterior_image_2_left', 'wrist_image_left'];

const moduleSpec = (module: string): ModuleSpec => registry.modules.find((m) => m.id === module)!;
const codeOf = (module: string, code: string) => moduleSpec(module).codes.find((c) => c.code === code)!;

function finding(module: string, code: string, message: string, extra: Partial<Finding> = {}): Finding {
  const c = codeOf(module, code);
  return { code, item: c.item ?? null, severity: c.severity, message_zh: message, ...extra };
}

/** A record of the story (C2 2.0): what the module found, the items it assessed and those it could not. */
function record(ep: number, module: string, details: Record<string, unknown>, opts: { findings?: Finding[]; readings?: Record<string, unknown>; unassessable?: ResultRecord['unassessable']; evidence?: string[] } = {}): ResultRecord {
  const unassessable = opts.unassessable ?? [];
  return {
    episode_index: ep,
    module,
    status: 'ok',
    findings: opts.findings ?? [],
    assessed: moduleSpec(module).covers.filter((i) => !unassessable.some((u) => u.item === i)),
    unassessable,
    readings: opts.readings ?? {},
    details,
    evidence: opts.evidence ?? [],
    elapsed_s: 0.4 + (ep % 7) * 0.3,
    error: null,
  };
}

/** A module that could not judge the episode properly (D33): no findings, nothing assessed. */
function errorRecord(ep: number, module: string, incident: { step: string; cause: string; call_kind?: string; attempts?: number }): ResultRecord {
  return { episode_index: ep, module, status: 'error', findings: [], assessed: [], unassessable: [], readings: {}, details: {}, evidence: [], elapsed_s: 60.2, error: { kind: 'execution', incidents: [incident] } };
}

// The main task's per-episode readings, shaped like the details the checks write (the Episode tab renders them):
// four episodes with a stuck actuator, six with a sync reading worth a look.
export const STUCK = [8, 21, 34, 46];
export const SYNC_NOTE: Record<number, 'annotated' | 'no_motion' | 'suspect' | 'undecidable'> = { 12: 'annotated', 20: 'no_motion', 26: 'suspect', 35: 'undecidable', 5: 'undecidable', 41: 'undecidable' };

/** A small deterministic generator (the mockups' rng). */
function rng(seed: number): () => number {
  let x = seed * 9301 + 49297;
  return () => {
    x = (x * 9301 + 49297) % 233280;
    return x / 233280;
  };
}

const round = (v: number, nd = 4) => Number(v.toFixed(nd));

/** Episode duration in seconds (ep 18 is the 0.5-second fragment). */
export function episodeDuration(ep: number): number {
  return ep === 18 ? 0.5 : round(11 + rng(ep + 11)() * 21, 1);
}

function timestampDetails(ep: number): Record<string, unknown> {
  const dur = episodeDuration(ep);
  if (ep === 18) return { n: 8, duration_s: 0.5, reason: '全长只有 0.50 秒(不足 1 秒,疑似采集中断的碎片)' };
  return { n: Math.round(dur * 15) + 1, duration_s: dur, dt_nominal: 0.066667, max_dt: round(0.0667 + (ep % 5) * 0.001, 4), jitter_ratio: round((ep % 4) * 0.005, 4) };
}

function motionDetails(ep: number): { score: number; details: Record<string, unknown> } {
  const r = rng(ep + 3);
  const base = 0.74 + r() * 0.2;
  const stuck = STUCK.includes(ep);
  const smoothness = round(Math.min(0.99, base + 0.02));
  const spike = round(Math.min(0.99, base + 0.06));
  const jitter = round(base - 0.07);
  const dur = episodeDuration(ep);
  const head = ep % 3 === 0 ? round(0.8 + r() * 2, 2) : 0;
  const tail = ep % 2 === 0 ? round(0.6 + r() * 1.5, 2) : 0;
  const details: Record<string, unknown> = {
    smoothness,
    spike,
    gripper_jitter: jitter,
    actuator_saturation: null,
    saturation_reason: '速度型指令和位置读数含义不同,值直比无意义',
    path_efficiency: round(0.6 + r() * 0.25),
    joint_stability: round(0.55 + r() * 0.3),
    fluency: round(0.85 + r() * 0.14),
    stuck: stuck ? 0 : 1,
    stuck_strategy: 'velocity_dual_scale',
    active_ratio: round(Math.max(0.5, 1 - (head + tail) / Math.max(dur, 1)), 4),
    idle_head_s: head,
    idle_tail_s: tail,
    idle_mid_count: ep % 8 === 1 ? 1 : 0,
    idle_mid_total_s: ep % 8 === 1 ? 1.2 : 0,
    gripper_flips: [2 + (ep % 3)],
    spike_isolation: round(1 + r() * 2, 2),
  };
  if (stuck) {
    const start = Math.round(dur * 0.4 * 15);
    details.stuck_joints = [{ joint: 2, axis: 'z', segment: 0, max_dead_run: 26, freeze_start_frame: start, freeze_end_frame: start + 26, envelope_start_frame: start - 4, envelope_frames: 34 }];
  }
  const score = round((smoothness + spike + jitter) / 3 - (stuck ? 0.05 : 0));
  return { score, details };
}

function visualDetails(ep: number): { score: number; details: Record<string, unknown> } {
  const per: Record<string, Record<string, number>> = {};
  CAMERAS.forEach((cam, k) => {
    const r = rng(ep * 3 + k + 5);
    let s = round(Math.min(0.99, 0.8 + r() * 0.17), 4);
    if ((ep === 20 && k === 1) || (ep === 35 && k === 2) || (ep === 47 && k === 2)) s = 0.57;
    per[`observation.images.${cam}`] = { score: s, sharpness: round(s - 0.04 + r() * 0.08, 4), exposure: round(0.85 + r() * 0.14, 4), integrity: 1, frozen_ratio: round(r() * 0.02, 4), blur_var_median: round(80 + r() * 900, 2) };
  });
  const scores = Object.values(per).map((d) => d.score);
  const worst = Object.entries(per).sort((a, b) => a[1].score - b[1].score)[0][0];
  return {
    score: round(scores.reduce((a, b) => a + b, 0) / scores.length),
    details: {
      ...per[worst],
      per_camera: Object.fromEntries(Object.entries(per).map(([k, d]) => [k, d.score])),
      per_camera_detail: per,
      camera_weights: Object.fromEntries(Object.keys(per).map((k) => [k, 1])),
      worst_camera: worst,
      padded_channels: [],
      camera_liveness: { live: Object.keys(per), dead_or_padded: [] },
      params: { blur_ref_var: 100, frame_max_side: 448 },
    },
  };
}

type Reading = Record<string, unknown>;

function reading(lag: number | null, peak: number, code: string, trusted: boolean, label: string, text: string): Reading {
  return {
    lag_s: lag,
    corr_peak: peak,
    corr_at_zero: round(peak - Math.abs(lag ?? 0) * 0.6, 2),
    peak_ratio: trusted ? 1.8 : 1.1,
    peak_width_s: trusted ? 0.3 : 1.4,
    at_scan_edge: false,
    trusted,
    code,
    note: text.slice(0, 60),
    diagnosis: { cause: code === 'aligned' ? 'aligned' : code, label, text, advice: trusted ? '' : '结论以其它相机为准。' },
  };
}

function syncDetails(ep: number): Record<string, unknown> {
  const per: Record<string, Reading> = {};
  CAMERAS.forEach((cam, k) => {
    const r = rng(ep * 5 + k + 1);
    const lag = round((r() - 0.45) * 0.14 + [0.03, 0.07, -0.01][k], 2);
    const peak = round(0.7 + r() * 0.25, 2);
    per[cam] = reading(lag, peak, 'aligned', true, '对齐', `画面与动作对得上:相似度最高的位置就在零点附近(${lag >= 0 ? '+' : ''}${lag.toFixed(2)}s),且结论清晰可靠(相似度 ${peak.toFixed(2)})。`);
  });
  const note = SYNC_NOTE[ep];
  const out: Record<string, unknown> = { verdict: 'aligned', flagged_cameras: [], suspect_cameras: [], noisy_cameras: [], abstained_cameras: [], consensus_lag_s: null, n_cameras: 3, n_trusted: 3, reason: '3/3 路可信相机全部对齐(|lag| ≤ 0.25s)' };
  if (note === 'annotated') {
    per[CAMERAS[1]] = reading(0.38, 0.72, 'misaligned', true, '错位', '这一路可靠地测出画面与动作错开了 +0.38s(超出容差):错开这么多时相似度明显最高(0.72),其它错开量都明显更差。');
    Object.assign(out, { verdict: 'annotated', flagged_cameras: [CAMERAS[1]], reason: `相机间矛盾:2 路读对齐(${CAMERAS[0]}, ${CAMERAS[2]}),1 路读滞后(${CAMERAS[1]} +0.38s) → 只标注异常路,不判废(证据不一致时不定罪)` });
  } else if (note === 'no_motion') {
    per[CAMERAS[1]] = reading(null, 0.08, 'no_motion', false, '无信号', '这一路几乎没有运动信号(静止段占满或画面冻结)。');
    Object.assign(out, { verdict: 'annotated', flagged_cameras: [CAMERAS[1]], n_trusted: 2, reason: '2/3 路可信相机对齐;1 路无信号,已标注' });
  } else if (note === 'suspect') {
    per[CAMERAS[0]] = reading(0.41, 0.46, 'flat_peak', false, '测不准 · 画面不锐利', '这一路定位时间差的精度不够:相似程度最高的位置在 +0.41s,但附近约 1.4 秒范围内的相似程度彼此接近,不足以把误差压进容差内。');
    Object.assign(out, { verdict: 'annotated', suspect_cameras: [CAMERAS[0]], abstained_cameras: [CAMERAS[0]], n_trusted: 2, reason: `2/3 路可信相机对齐,但另有 1 路疑似错位(${CAMERAS[0]} +0.41s)——峰形不够可信,不足以定论,已标注该路;不判废、不进人工队列` });
  } else if (note === 'undecidable') {
    for (const cam of CAMERAS) per[cam] = reading(0.1, 0.21, 'low_corr', false, '测不准 · 信号弱', '这一路的画面运动与机械臂动作对不上号(整体相似度只有 0.21),给不出可靠的时间差读数。');
    Object.assign(out, { verdict: 'undecidable', abstained_cameras: [...CAMERAS], n_trusted: 0, reason: '3 路相机均未给出可信读数(测不准/无信号),同步不下结论,不影响判决' });
  }
  out.per_camera = per;
  return out;
}

const VOTE: Record<string, string> = { success: 'yes', failure: 'no', uncertain: 'unclear' };

/** Task success's one request (video-task/2, D71): the verdict for the episode and every camera's own answer. */
function taskDetails(ep: number): Record<string, unknown> {
  const text = taskText(ep);
  const cams = (verdicts: string[]) => Object.fromEntries(CAMERAS.map((c, k) => [c, { verdict: verdicts[k], defects: [] }]));
  const votes = (verdicts: string[]) => Object.fromEntries(CAMERAS.map((c, k) => [c, VOTE[verdicts[k]]]));
  const answer = (init: string, verdicts: string[], review: string, completion: number, reason: string, evidence: { camera: string; start_s: number; end_s: number; observation: string }[]) => ({
    input_mode: 'video',
    protocol: 'video-task/2',
    task_desc: text.text,
    task_desc_source: text.source,
    cams: [...CAMERAS],
    init_verdict: init,
    task_type: 'persistent',
    task_type_source: 'video',
    video_completion: completion,
    reason,
    video_evidence: evidence,
    cameras: cams(verdicts),
    cam_votes: votes(verdicts),
    review,
  });
  if (TS_REJECTS.includes(ep)) {
    return {
      ...answer('failure', ['failure', 'failure', 'uncertain'], 'no', 0.12, '抓起后在半空松手，物体掉回桌面，任务没有完成', [{ camera: CAMERAS[0], start_s: 9.5, end_s: 13, observation: '夹爪在半空张开，物体掉落' }]),
      verdict: 'failure',
      rules: ['video_single_pass_failure'],
    };
  }
  if (SPLIT_REVIEW.includes(ep)) {
    return {
      ...answer('failure', ['failure', 'failure', 'success'], 'split', 0.4, '视频判定=failure，逐机位=split，结论不够确定，转人工', [{ camera: CAMERAS[2], start_s: 8, end_s: 11, observation: '腕部画面里物体已放进容器' }]),
      verdict: 'uncertain',
      rules: ['video_single_pass_undecided'],
    };
  }
  if (VERDICT_REVIEW.includes(ep)) {
    return {
      ...answer('uncertain', ['uncertain', 'uncertain', 'uncertain'], 'abstain', 0.38, '视频判定=uncertain，逐机位=abstain，结论不够确定，转人工', []),
      verdict: 'uncertain',
      rules: ['video_single_pass_undecided'],
    };
  }
  return {
    ...answer('success', ['success', 'success', 'success'], 'yes', 0.97, '物体放进了目标位置，夹爪松开后退回', [{ camera: CAMERAS[0], start_s: 10, end_s: 12.5, observation: '物体落在目标位置' }]),
    verdict: 'success',
    rules: ['video_single_pass_success'],
  };
}

const sameText = (others: number[]) => `与 ${others.map((e) => `ep${String(e).padStart(6, '0')}`).join('、')} 完全相同（动作与各路画面字节一致）`;
const evidenceOf = (module: string, ep: number) => [0, 1].map((k) => `details/evidence/${module}/ep${String(ep).padStart(6, '0')}_${k}.jpg`);

/** Every selected module's record of the episode; `quotaRun`: the first run, task success failing on every one. */
function recordsOf(ep: number, quotaRun = false): Record<string, ResultRecord> {
  const motion = motionDetails(ep);
  const vis = visualDetails(ep);
  const sync = SYNC_NOTE[ep];
  const motionFindings: Finding[] = [];
  if (STUCK.includes(ep)) motionFindings.push(finding('motion_quality', 'stuck', '关节 3 从第 40 帧到第 95 帧卡住不动', { frames: [40, 95], scope: { channel: 'joint_3' } }));
  if (ep % 3 === 0) motionFindings.push(finding('motion_quality', 'idle_opening', '开头空转 1.2 秒才开始动作', { time_s: [0, 1.2] }));
  const syncFindings: Finding[] =
    sync === 'annotated'
      ? [finding('video_action_sync', 'camera_misaligned', `${CAMERAS[1]} 比动作晚 0.42 秒`, { scope: { camera: CAMERAS[1] } })]
      : sync === 'suspect'
        ? [finding('video_action_sync', 'suspect', `${CAMERAS[0]} 疑似错位，证据不足`, { scope: { camera: CAMERAS[0] } })]
        : sync === 'undecidable' || sync === 'no_motion'
          ? [finding('video_action_sync', 'undecidable', '画面里动作太少，测不准滞后')]
          : [];
  const same = SAME_AS[ep];
  const records: Record<string, ResultRecord> = {
    timestamp_check: record(ep, 'timestamp_check', timestampDetails(ep), {
      findings: ep === 18 ? [finding('timestamp_check', 'fragment', '全长只有 0.50 秒（不足 1 秒，疑似采集中断的碎片）', { time_s: [0, 0.5] })] : [],
    }),
    motion_quality: record(ep, 'motion_quality', motion.details, {
      findings: motionFindings,
      readings: {
        score: motion.score,
        // 8.11: the rest stretches the player's motion strip draws
        idle_head_s: ep % 3 === 0 ? 1.2 : 0,
        idle_tail_s: ep % 5 === 1 ? 0.8 : 0,
        idle_mid_segments: ep % 4 === 2 ? [{ start_frame: 50, start_s: 5, dur_s: 1.5 }] : [],
        still_segments: ep % 3 === 0 ? [{ start_s: 0, end_s: 1.2 }] : [],
      },
      unassessable: [{ item: 'ACT-4', reason: 'not_applicable', message_zh: '速度型指令和位置读数含义不同，执行器饱和无从比较' }],
    }),
    visual_quality: record(ep, 'visual_quality', vis.details, {
      findings: ep % 7 === 2 ? [finding('visual_quality', 'exposure_low', `${CAMERAS[ep % 3]} 画面偏暗，曝光分 0.48`, { scope: { camera: CAMERAS[ep % 3] } })] : [],
      readings: { score: vis.score },
    }),
    video_action_sync: record(ep, 'video_action_sync', syncDetails(ep), { findings: syncFindings }),
    // 8.11: the steps the dataset declares (every third episode has none)
    declared_steps:
      ep % 3 === 2
        ? record(ep, 'declared_steps', { sources: [], reason: '数据集没有逐帧的分步或模式标注' }, { unassessable: [{ item: 'TASK-1', reason: 'not_applicable', message_zh: '数据集没有逐帧的分步或模式标注' }] })
        : record(ep, 'declared_steps', { sources: ['index_table:low_level_task_index'] }, {
            findings: [
              finding('declared_steps', 'step_declared', '第 1 步「伸手」：第 1–60 帧', { frames: [0, 59], time_s: [0, 6], readings: { source: 'declared', track: 'low_level_task_index', step: 1, n_steps: 2, label: '伸手' } }),
              finding('declared_steps', 'step_declared', '第 2 步「放下」：第 61–120 帧', { frames: [60, 119], time_s: [6, 12], readings: { source: 'declared', track: 'low_level_task_index', step: 2, n_steps: 2, label: '放下' } }),
            ],
            readings: { tracks: [{ key: 'low_level_task_index', name: '机器人分步', format: 'index_table', primary: true, n_steps: 2, segments: [{ start_s: 0, end_s: 6, frames: [0, 59], label: '伸手' }, { start_s: 6, end_s: 12, frames: [60, 119], label: '放下' }] }] },
          }),
    // 8.12: the boundaries measured from the numbers (the detected track): a pause, a gripper event
    step_detection: record(ep, 'step_detection', { min_score: 0.3, pause_ref_s: 3, fps: 10, gripper_dims: [6], stream: 'state', n_frames: 120 }, {
      findings: [
        finding('step_detection', 'step_candidate', '候选边界 1：5.8 秒（停了 1.2 秒）', { frames: [58, 58], time_s: [5.8, 5.8], readings: { source: 'detected', cue: 'pause', score: 0.4, dur_s: 1.2 } }),
        finding('step_detection', 'step_candidate', '候选边界 2：9.0 秒（夹爪松开）', { frames: [90, 90], time_s: [9, 9], readings: { source: 'detected', cue: 'release', score: 0.8 } }),
      ],
      readings: { n_candidates: 3, n_reported: 2, stream: 'state', cues: { pause: 2, release: 1 }, candidates: [{ frame: 58, t_s: 5.8, cue: 'pause', score: 0.4, dur_s: 1.2 }, { frame: 90, t_s: 9, cue: 'release', score: 0.8, column: 6 }, { frame: 101, t_s: 10.1, cue: 'pause', score: 0.1, dur_s: 0.3 }] },
    }),
  };
  if (quotaRun) {
    records.task_success = errorRecord(ep, 'task_success', { step: 'probe', cause: '429 QuotaExceeded', call_kind: 'probe', attempts: 3 });
  } else if (HELD.includes(ep)) {
    records.task_success = errorRecord(ep, 'task_success', { step: 'probe', cause: 'timeout 60s', call_kind: 'probe', attempts: 3 });
  } else {
    const d = taskDetails(ep);
    const findings = TS_REJECTS.includes(ep)
      ? [finding('task_success', 'failure', String(d.reason), { time_s: [9.5, 13], evidence: [evidenceOf('task_success', ep)[0]] })]
      : VERDICT_REVIEW.includes(ep)
        ? [finding('task_success', 'uncertain', SPLIT_REVIEW.includes(ep) ? '整条判失败，但机位之间分歧，转人工' : '模型拿不准这一条做没做成，转人工')]
        : [];
    records.task_success = record(ep, 'task_success', d, { findings, evidence: findings.length ? evidenceOf('task_success', ep) : [] });
  }
  records.duplicate_check = record(ep, 'duplicate_check', same ? { same_as: same, group_id: Math.min(ep, ...same), reason: sameText(same) } : {}, {
    findings: same ? [finding('duplicate_check', 'duplicate', sameText(same), { readings: { same_as: same, group_id: Math.min(ep, ...same) } })] : [],
  });
  return records;
}

/** The findings graded by the default policy (the registry's levels, P18), in module order. */
function graded(records: Record<string, ResultRecord>): EpisodeFinding[] {
  const out: EpisodeFinding[] = [];
  for (const m of MAIN_MODULES) {
    for (const f of records[m]?.findings ?? []) {
      const c = codeOf(m, f.code);
      out.push({ module: m, level: c.level, ...(c.level === 'review' && c.review_line ? { line: c.review_line } : {}), appealable: c.level === 'blocking' && c.appealable, finding: f });
    }
  }
  return out;
}

const ITEM_ORDER = new Map(registry.taxonomy.items.map((it, i) => [it.id, i]));
const LEVEL_ORDER = ['blocking', 'review', 'info'] as const;

function itemsAndLevels(findings: EpisodeFinding[]): { items: string[]; levels: EpisodeFinding['level'][] } {
  const items = [...new Set(findings.map((f) => f.finding.item).filter((i): i is string => Boolean(i)))].sort((a, b) => (ITEM_ORDER.get(a) ?? 999) - (ITEM_ORDER.get(b) ?? 999));
  return { items, levels: LEVEL_ORDER.filter((lv) => findings.some((f) => f.level === lv)) };
}

/**
 * One episode across all modules (C4 EpisodeView): the records, every finding with its level, the reasons no finding
 * stands for, the questions. `quotaRun`: the main task's first run (r0001), task success failing on every episode.
 */
export function episodeView(ep: number, revision: number, quotaRun = false): EpisodeView {
  const list = episodeList(ep, quotaRun);
  const modules = recordsOf(ep, quotaRun);
  const findings = graded(modules);
  const reasons: NonNullable<EpisodeView['reasons']> = findings
    .filter((f) => f.level === 'blocking')
    .map((f) => ({ module: f.module, kind: 'finding', text: f.finding.message_zh, code: f.finding.code, item: f.finding.item, appealable: f.appealable }));
  const error = modules.task_success.error;
  if (error) reasons.push({ module: 'task_success', kind: 'execution_error', text: `「任务成败判定」执行出错：${error.incidents.map((i) => `${i.step} ${i.cause ?? ''}（${i.attempts ?? 1} 次）`).join('；')}，待补跑` });
  const review: NonNullable<EpisodeView['review']> = [];
  const asked = findings.find((f) => f.line === 'task_verdict');
  if (asked) review.push({ module: 'task_success', kind: 'task_verdict', text: asked.finding.message_zh });
  // like review.json: a reject whose blocking findings may all be appealed is asked on the appeals tab
  const blocking = findings.filter((f) => f.level === 'blocking');
  if (blocking.length && blocking.every((f) => f.appealable)) review.push({ module: blocking[0].module, kind: 'reject_appeal', text: blocking[0].finding.message_zh });
  return {
    episode_index: ep,
    revision,
    list,
    reasons,
    review,
    task_text: taskText(ep),
    modules,
    findings,
    evidence: modules.task_success.evidence.map((path) => ({ module: 'task_success', path, kind: 'frame' })),
    videos: CAMERAS.map((camera) => ({
      camera,
      scope: 'input' as const,
      origin: 'source_dataset' as const,
      path: `videos/chunk-000/${camera}/file-000.mp4`,
      from_ts: ep * 14,
      to_ts: ep * 14 + 14,
    })),
  };
}

const EPISODES = Array.from({ length: 50 }, (_, ep) => ep);

/** The episodes of the main task in index order (C4 listTaskEpisodes items), with the items and levels of their findings. */
export function mainEpisodes(openQuestions: Map<number, string[]>, revision = 2): TaskEpisode[] {
  const quotaRun = revision === 1;
  return EPISODES.map((ep) => {
    const view = episodeView(ep, revision, quotaRun);
    const { items, levels } = itemsAndLevels(view.findings ?? []);
    const reason = view.list === 'reject' ? [...new Set((view.reasons ?? []).filter((r) => r.kind === 'finding').map((r) => r.module))] : view.list === 'held' ? ['task_success'] : [];
    return { episode_index: ep, list: view.list, review: openQuestions.has(ep), reason_modules: reason, review_modules: openQuestions.get(ep) ?? [], items, levels };
  });
}

/** Another task's episodes: its summary's rejects first, then its held ones, the rest passed. */
export function genericEpisodes(total: number, rejected: number, held: number, openQuestions: Map<number, string[]>): TaskEpisode[] {
  return Array.from({ length: total }, (_, ep) => {
    const list: TaskEpisode['list'] = ep % 17 === 3 && ep / 17 < rejected ? 'reject' : ep % 23 === 5 && ep / 23 < held ? 'held' : 'passed';
    return { episode_index: ep, list, review: openQuestions.has(ep), reason_modules: list === 'reject' ? ['timestamp_check'] : list === 'held' ? ['task_success'] : [], review_modules: openQuestions.get(ep) ?? [] };
  });
}

/** A row of the live episode page (C4 PipelineEpisode): every stage's state, the time in each, the verdict - final
 * once the run is over (C4 5.3.2), provisional while it runs. */
export function pipelineRow(ep: number, revision: number, finished: boolean, quotaRun = false): PipelineEpisode {
  const view = episodeView(ep, revision, quotaRun);
  const verdict = view.list === 'reject' ? 'drop' : view.list === 'held' ? 'held' : 'keep';
  const took = { numeric: 0.18, frame: 2.9 + (ep % 7) * 0.1, duplicate: 0.02, vlm: 11.2 + (ep % 5) };
  return {
    episode_index: ep,
    reason: null,
    verdict,
    provisional: !finished,
    stages: { numeric: 'done', frame: 'done', duplicate: 'done', vlm: verdict === 'held' ? 'error' : 'done' },
    stage_processing_s: took,
    processing_s: Object.values(took).reduce((a, x) => a + x, 0),
    verdict_reason: verdict === 'drop' ? view.reasons?.[0]?.text ?? null : verdict === 'held' ? '待补跑：「任务成败判定」执行出错' : null,
  };
}

/** A record its module wrote without judging the episode (D94): why, or null. */
const skippedOf = (r: ResultRecord): string | null => (typeof r.details?.skipped === 'string' ? r.details.skipped : null);

/** The live module counts of the main task (C4 listPipelineEpisodes `modules`): per module the episodes judged,
 * failed on, skipped without judging (D94) and, of the judged, the ones with a finding. */
export function mainModuleCounts(): { id: string; judged: number; error: number; skipped: number; flagged: number }[] {
  const per = EPISODES.map((ep) => recordsOf(ep));
  return MAIN_MODULES.map((m) => {
    const recs = per.map((r) => r[m]).filter(Boolean);
    const ok = recs.filter((r) => r.status === 'ok' && !skippedOf(r));
    const skipped = recs.filter((r) => r.status === 'ok' && skippedOf(r)).length;
    return { id: m, judged: ok.length, error: recs.length - ok.length - skipped, skipped, flagged: ok.filter((r) => r.findings.length).length };
  });
}

/**
 * Sync curves of the main task (C4 1.9.0 `getEpisodeSyncCurves`): kept only for the episodes worth
 * a look (pipeline.sync_plots = flagged), like the frame stage does; null for the others.
 */
export function mainSyncCurves(ep: number, revision: number): SyncCurves | null {
  if (!(ep in SYNC_NOTE)) return null;
  const det = recordsOf(ep).video_action_sync.details as { verdict: string; per_camera: Record<string, Reading> };
  const T = episodeDuration(ep);
  const n = Math.round(T * 15);
  const cameras = CAMERAS.map((cam, ci) => {
    const r = rng(ep * 7 + ci);
    const rd = det.per_camera[cam];
    const lag = typeof rd.lag_s === 'number' ? rd.lag_s : 0;
    const peak = typeof rd.corr_peak === 'number' ? rd.corr_peak : 0.2;
    const bumps = [3 + r() * 3, 9 + r() * 3, 15 + r() * 2].map((c) => c % T);
    const t: number[] = [];
    const speed: number[] = [];
    const flow: number[] = [];
    for (let i = 0; i < n; i += 1) {
      const x = i / 15;
      const s = bumps.reduce((a, c) => a + Math.exp(-((x - c) ** 2) / 1.2), 0);
      const f = bumps.reduce((a, c) => a + Math.exp(-((x - lag - c) ** 2) / 1.2), 0);
      t.push(round(x, 3));
      speed.push(round(s * 0.4, 6));
      flow.push(round((f * peak + (1 - peak) * r() * 0.8) * 3, 5));
    }
    const lags: number[] = [];
    const xcorr: number[] = [];
    const wide = rd.code === 'flat_peak' ? 0.5 : 0.18;
    for (let j = 0; j <= 60; j += 1) {
      const s = -2 + (j / 60) * 4;
      lags.push(round(s, 3));
      xcorr.push(round(Math.max(0, Math.min(1, peak * Math.exp(-((s - lag) ** 2) / wide) + 0.12 * Math.cos(s * 3 + ci))), 4));
    }
    const hasLag = typeof rd.lag_s === 'number';
    const at = hasLag ? xcorr[Math.round(((lag + 2) / 4) * 60)] : null;
    // the windowed readings (registry 8.1): 2 s windows every second, around the camera's lag, trusted when the peak is
    const points = [];
    for (let t0 = 0; t0 + 2 <= T; t0 += 1) {
      const trusted = peak >= 0.5 && r() > 0.2;
      points.push({ t_s: round(t0 + 1, 3), lag_s: round(lag + (r() - 0.5) * (trusted ? 0.06 : 0.6), 4), corr_peak: round(trusted ? peak : peak * 0.5, 4), trusted });
    }
    const tl = points.filter((p) => p.trusted).map((p) => p.lag_s).sort((a, b) => a - b);
    const q = (f: number) => (tl.length ? tl[Math.min(tl.length - 1, Math.floor(f * tl.length))] : null);
    const windows = { win_s: 2, hop_s: 1, lag_s: tl.length ? round(tl[tl.length >> 1], 4) : null, iqr_s: tl.length >= 2 ? round((q(0.75) as number) - (q(0.25) as number), 4) : null, n_trusted: tl.length, n_windows: points.length, points };
    return { camera: cam, t, flow, speed, lags, xcorr, lag_s: hasLag ? lag : null, corr_peak: peak, code: String(rd.code), trusted: Boolean(rd.trusted), peak: hasLag && at !== null ? { lag_s: lag, corr: at } : null, windows };
  });
  return { episode_index: ep, revision, verdict: det.verdict, consensus_lag_s: null, lag_tol_s: 0.25, window_s: 2, cameras };
}

const MAIN_CAMERA_HIST: Record<string, number[]> = {
  exterior_image_1_left: [0, 0, 0, 0, 0, 0, 1, 7, 19, 23],
  exterior_image_2_left: [0, 0, 0, 0, 0, 1, 2, 9, 21, 17],
  wrist_image_left: [0, 0, 0, 0, 0, 2, 3, 10, 19, 16],
};

const series = (pairs: [string, number][]) => pairs.map(([name, count]) => ({ name, count }));
const hist = (counts: number[]) => counts.map((count, i) => ({ name: `${(i / 10).toFixed(1)}–${((i + 1) / 10).toFixed(1)}`, count }));
const counts = (total: number, c: Partial<Record<'pass' | 'fail' | 'abstain' | 'scored' | 'error', number>>) => ({ total, pass: 0, fail: 0, abstain: 0, scored: 0, error: 0, ...c });

/**
 * A module's own summary in the shape `curation report` writes (06 §6.2: the keys of the module's own view, its
 * chart-ready aggregates; the composite score's distribution as the `score` reading's), scaled to `total` episodes,
 * for the tasks other than the main one.
 */
export function sampleSummary(id: string, total: number): Record<string, unknown> {
  const n = Math.max(total, 1);
  const part = (share: number) => Math.round(n * share);
  switch (id) {
    case 'timestamp_check':
      return { counts: counts(n, { pass: n - part(0.02), fail: part(0.02) }), fail_kinds: { fragment: part(0.01), gap: part(0.01) }, fail_reasons: series([['out_of_order', 0], ['gap', part(0.01)], ['fragment', part(0.01)], ['jitter', 0]]), duration_total_s: round(n * 18.2, 1), duration_median_s: 17.9, duration_min_s: 0.6, duration_max_s: 41.2, duration_hist: series([['0–10', part(0.08)], ['10–20', part(0.52)], ['20–30', part(0.3)], ['30–40', part(0.08)], ['40–50', part(0.02)]]) };
    case 'kinematic_limits':
      return { counts: counts(n, { pass: n - part(0.03) - part(0.01), fail: part(0.03), abstain: part(0.01) }), violation_episodes: part(0.03), violations_by_type: series([['velocity_limit', part(0.02)], ['joint_limit', part(0.01)]]), violations_by_joint: series([['1', part(0.01)], ['3', part(0.02)], ['5', part(0.01)]]), limits_profile: 'so101', abstain_reason_counts: series([['单位疑似错配', part(0.01)]]) };
    case 'motion_quality':
      return { counts: counts(n, { scored: n }), mean_score: 0.82, score_hist: { score: hist([0, 0, 0, 0, part(0.02), part(0.05), part(0.13), part(0.3), part(0.32), n - part(0.02) - part(0.05) - part(0.13) - part(0.3) - part(0.32)]) }, subscores: [{ name: 'smoothness', mean: 0.84, n, na: 0, in_total: true }, { name: 'spike', mean: 0.88, n, na: 0, in_total: true }, { name: 'gripper_jitter', mean: 0.79, n, na: 0, in_total: true }, { name: 'actuator_saturation', mean: 0.74, n, na: 0, in_total: true }, { name: 'path_efficiency', mean: 0.69, n, na: 0, in_total: false }, { name: 'joint_stability', mean: 0.63, n, na: 0, in_total: false }], stuck_episodes: part(0.04), idle_episodes: series([['head', part(0.3)], ['mid', part(0.1)], ['tail', part(0.35)]]), active_ratio_mean: 0.86 };
    case 'visual_quality':
      return { counts: counts(n, { scored: n }), mean_score: 0.88, score_hist: { score: hist([0, 0, 0, 0, 0, part(0.02), part(0.05), part(0.18), part(0.4), n - part(0.02) - part(0.05) - part(0.18) - part(0.4)]) }, cameras: [{ camera: 'front', n, mean: 0.9, low: part(0.01), placeholder: 0, hist: [0, 0, 0, 0, 0, part(0.01), part(0.04), part(0.15), part(0.4), n - part(0.01) - part(0.04) - part(0.15) - part(0.4)], weight: 1 }, { camera: 'wrist', n, mean: 0.86, low: part(0.02), placeholder: 0, hist: [0, 0, 0, 0, 0, part(0.02), part(0.06), part(0.2), part(0.4), n - part(0.02) - part(0.06) - part(0.2) - part(0.4)], weight: 1 }], low_camera_readings: part(0.03), placeholder_readings: 0, blur_ref_var: 100, frame_max_side: 448 };
    case 'video_action_sync':
      return { counts: counts(n, { pass: n - part(0.01), fail: part(0.01) }), verdicts: series([['aligned', n - part(0.01) - part(0.04) - part(0.02) - part(0.03)], ['annotated', part(0.04)], ['suspect', part(0.02)], ['undecidable', part(0.03)], ['misaligned', part(0.01)]]), flagged_camera_readings: part(0.05), lag_tol_s: 0.25, cameras: [{ camera: 'front', readings: n, n: part(0.95), median_lag_s: 0.04, iqr_s: 0.06, n_flagged: part(0.03), n_suspect: part(0.01), n_noisy: 0, n_abstained: part(0.02), n_window_stable: part(0.7) }, { camera: 'wrist', readings: n, n: part(0.93), median_lag_s: 0.02, iqr_s: 0.05, n_flagged: part(0.02), n_suspect: part(0.01), n_noisy: part(0.01), n_abstained: part(0.03), n_window_stable: part(0.85) }], sync_advice: '全库逐相机中位滞后均在容差内(|median| ≤ 0.25s),未见系统性错位;分窗规则可比对 ' + part(0.65) + '/' + n + ' 条(两路以上相机逐窗读数都稳)', negative_lag_episodes: 0, window_comparable_episodes: part(0.65), episodes_with_readings: n };
    case 'task_success':
      return { counts: counts(n, { pass: n - part(0.04) - part(0.03), fail: part(0.04), abstain: part(0.03) }), abstain_reasons: { '视频判定=uncertain，结论不够确定，转人工': part(0.03) }, abstain_reason_counts: series([['视频判定 … 结论不够确定，转人工', part(0.03)]]), judgements: series([['success', n - part(0.04) - part(0.03)], ['failure', part(0.04)], ['uncertain', part(0.03)]]), abstain_by_judgement: series([['uncertain', part(0.03)]]), text_sources: series([['原始标注', n]]), layers: series([['probe', n], ['endstate', n]]) };
    case 'duplicate_check':
      return { counts: counts(n, { pass: n }), groups: 1, duplicated: 2, group_sizes: series([['2', 1]]) };
    default:
      return { counts: counts(n, { pass: n }) };
  }
}

/** The taxonomy's check items a set of modules covers, and those it does not (the report's coverage, design doc 17 §5.2). */
export function coverageOf(modules: readonly string[]): { taxonomy_version: string; covered: string[]; not_covered: string[] } {
  const covers = new Set(modules.flatMap((id) => registry.modules.find((m) => m.id === id)?.covers ?? []));
  const checkable = registry.taxonomy.items.filter((i) => i.kind !== 'control').map((i) => i.id);
  return { taxonomy_version: registry.taxonomy_version, covered: checkable.filter((i) => covers.has(i)), not_covered: checkable.filter((i) => !covers.has(i)) };
}

type Section = Report['modules'][number];

/** The statistics every module section has (design doc 17 §5.1), from the records of the episodes. */
function findingsStats(module: string, records: ResultRecord[]): Record<string, unknown> {
  const judged = records.filter((r) => r.status === 'ok' && !skippedOf(r));
  const skipped = new Map<string, number>();
  for (const r of records) if (r.status === 'ok' && skippedOf(r)) skipped.set(skippedOf(r)!, (skipped.get(skippedOf(r)!) ?? 0) + 1);
  const byCode = new Map<string, { item: string | null; level: string; eps: Set<number>; cams: Map<string, Set<number>> }>();
  const levels = { blocking: new Set<number>(), review: new Set<number>(), info: new Set<number>() };
  for (const r of judged) {
    for (const f of r.findings) {
      const c = codeOf(module, f.code);
      const e = byCode.get(f.code) ?? { item: f.item, level: c.level, eps: new Set<number>(), cams: new Map<string, Set<number>>() };
      e.eps.add(r.episode_index);
      if (f.scope?.camera) e.cams.set(f.scope.camera, (e.cams.get(f.scope.camera) ?? new Set<number>()).add(r.episode_index));
      byCode.set(f.code, e);
      levels[c.level].add(r.episode_index);
    }
  }
  const n = judged.length;
  const why = new Map<string, number>();
  for (const r of judged) for (const u of r.unassessable) why.set(`${u.item}|${u.reason}`, (why.get(`${u.item}|${u.reason}`) ?? 0) + 1);
  return {
    assessed_episodes: n,
    skipped_episodes: [...skipped.values()].reduce((a, x) => a + x, 0),
    skipped_reasons: [...skipped.entries()].sort((a, b) => b[1] - a[1]).map(([name, count]) => ({ name, count })),
    flagged_episodes: new Set([...levels.blocking, ...levels.review, ...levels.info]).size,
    levels: { blocking: levels.blocking.size, review: levels.review.size, info: levels.info.size },
    items: [...byCode.entries()].map(([code, e]) => ({
      item: e.item,
      code,
      level: e.level,
      episodes: e.eps.size,
      share: n ? Number((e.eps.size / n).toFixed(4)) : null,
      ...(e.cams.size ? { by_camera: [...e.cams.entries()].map(([camera, eps]) => ({ camera, episodes: eps.size })) } : {}),
    })),
    unassessable: [...why.entries()].map(([key, count]) => ({ item: key.split('|')[0], reason: key.split('|')[1], count })),
  };
}

/** The main task's own summaries per module (06 §6.2's keys); `quotaRun`: r0001, task success failed as a whole. */
function mainOwnSummary(id: string, quotaRun: boolean): Record<string, unknown> {
  switch (id) {
    case 'timestamp_check':
      return {
        counts: counts(50, { pass: 49, fail: 1 }),
        fail_kinds: { fragment: 1 },
        fail_reasons: series([['out_of_order', 0], ['gap', 0], ['fragment', 1], ['jitter', 0]]),
        duration_total_s: 1047.8,
        duration_median_s: 21.4,
        duration_min_s: 0.5,
        duration_max_s: 31.9,
        duration_hist: series([['0–5', 1], ['5–10', 0], ['10–15', 9], ['15–20', 12], ['20–25', 14], ['25–30', 10], ['30–35', 4]]),
      };
    case 'motion_quality':
      return {
        counts: counts(50, { scored: 50 }),
        mean_score: 0.84,
        score_hist: { score: hist([0, 0, 0, 0, 0, 1, 4, 14, 19, 12]) },
        subscores: [
          { name: 'smoothness', mean: 0.86, n: 50, na: 0, in_total: true },
          { name: 'spike', mean: 0.9, n: 50, na: 0, in_total: true },
          { name: 'gripper_jitter', mean: 0.77, n: 50, na: 0, in_total: true },
          { name: 'actuator_saturation', mean: null, n: 0, na: 50, in_total: true, na_reason: '速度型指令和位置读数含义不同,值直比无意义' },
          { name: 'path_efficiency', mean: 0.71, n: 50, na: 0, in_total: false },
          { name: 'joint_stability', mean: 0.68, n: 50, na: 0, in_total: false },
          { name: 'fluency', mean: 0.92, n: 50, na: 0, in_total: false },
        ],
        stuck_episodes: 4,
        idle_episodes: series([['head', 17], ['mid', 7], ['tail', 25]]),
        active_ratio_mean: 0.9,
      };
    case 'visual_quality':
      return {
        counts: counts(50, { scored: 50 }),
        mean_score: 0.87,
        score_hist: { score: hist([0, 0, 0, 0, 0, 1, 2, 9, 21, 17]) },
        cameras: CAMERAS.map((camera, k) => ({ camera, n: 50, mean: [0.89, 0.86, 0.85][k], low: [0, 1, 2][k], placeholder: 0, hist: MAIN_CAMERA_HIST[camera], weight: 1 })),
        low_camera_readings: 3,
        placeholder_readings: 0,
        blur_ref_var: 100,
        frame_max_side: 448,
      };
    case 'video_action_sync':
      return {
        counts: counts(50, { pass: 50 }),
        verdicts: series([['aligned', 44], ['annotated', 2], ['suspect', 1], ['undecidable', 3], ['misaligned', 0]]),
        flagged_camera_readings: 2,
        lag_tol_s: 0.25,
        cameras: [
          { camera: CAMERAS[0], readings: 50, n: 47, median_lag_s: 0.03, iqr_s: 0.05, n_flagged: 0, n_suspect: 1, n_noisy: 0, n_abstained: 4, n_window_stable: 31 },
          { camera: CAMERAS[1], readings: 50, n: 45, median_lag_s: 0.07, iqr_s: 0.09, n_flagged: 2, n_suspect: 0, n_noisy: 0, n_abstained: 3, n_window_stable: 22 },
          { camera: CAMERAS[2], readings: 50, n: 47, median_lag_s: -0.01, iqr_s: 0.04, n_flagged: 0, n_suspect: 0, n_noisy: 0, n_abstained: 3, n_window_stable: 44 },
        ],
        window_comparable_episodes: 26,
        episodes_with_readings: 50,
        sync_advice: '全库逐相机中位滞后均在容差内(|median| ≤ 0.25s),未见系统性错位;另有 1 条存在疑似错位但证据不足的相机(最多的是 exterior_image_1_left,1 条)——不判废也不进人工队列,但做逐帧对齐敏感的训练时建议对该路降权',
        negative_lag_episodes: 0,
      };
    case 'task_success':
      return quotaRun
        ? { counts: counts(0, {}) }
        : {
            counts: counts(50, { pass: 33, fail: 5, abstain: 10, error: 2 }),
            abstain_reasons: { '视频判定=failure，逐机位=split，结论不够确定，转人工': 5, '视频判定=uncertain，逐机位=abstain，结论不够确定，转人工': 5 },
            abstain_reason_counts: series([['整条判失败，机位之间分歧', 5], ['模型拿不准', 5]]),
            error_steps: series([['probe', 2]]),
            judgements: series([['success', 33], ['failure', 5], ['uncertain', 10]]),
            abstain_by_judgement: series([['uncertain', 10]]),
            text_sources: series([['原始标注', 48]]),
            layers: series([['probe', 48], ['endstate', 48]]),
          };
    case 'duplicate_check':
      return { counts: counts(50, { pass: 50 }), groups: 1, duplicated: 2, group_sizes: series([['2', 1]]) };
    default:
      return { counts: counts(50, { pass: 50 }) };
  }
}

const TABLES: Record<string, Section['tables']> = {
  timestamp_check: [{ id: 'timestamp_check', rows: 50, file: 'tables/timestamp_check.parquet' }],
  motion_quality: [{ id: 'motion_quality', rows: 50, file: 'tables/motion_quality.parquet' }],
  declared_steps: [],
  step_detection: [],
  visual_quality: [{ id: 'visual_quality', rows: 150, file: 'tables/visual_quality.parquet' }],
  video_action_sync: [{ id: 'video_action_sync', rows: 150, file: 'tables/video_action_sync.parquet' }],
  task_success: [{ id: 'task_success', rows: 50, file: 'tables/task_success.parquet' }],
  duplicate_check: [{ id: 'duplicate_groups', rows: 2, file: 'tables/duplicate_groups.parquet' }],
};

/** The main task's report (C2 2.0): r0001 is the first run, which lost task success to the quota; r0002 the retry's. */
export function mainReport(revision: 1 | 2): Report {
  const quotaRun = revision === 1;
  const all = EPISODES.map((ep) => recordsOf(ep, quotaRun));
  const perEp = all.map((records, ep) => ({ ep, findings: graded(records) }));
  const byItem = new Map<string, { episodes: Set<number>; blocking: Set<number>; review: Set<number>; info: Set<number> }>();
  for (const { ep, findings } of perEp) {
    for (const f of findings) {
      if (!f.finding.item) continue;
      const e = byItem.get(f.finding.item) ?? { episodes: new Set<number>(), blocking: new Set<number>(), review: new Set<number>(), info: new Set<number>() };
      e.episodes.add(ep);
      e[f.level].add(ep);
      byItem.set(f.finding.item, e);
    }
  }
  const sections: Section[] = MAIN_MODULES.map((id) => {
    const failed = quotaRun && id === 'task_success';
    const own = mainOwnSummary(id, quotaRun);
    const summary = {
      ...own,
      ...findingsStats(id, failed ? [] : all.map((r) => r[id])),
      ...(id === 'timestamp_check' ? { dataset_findings: [finding('timestamp_check', 'duration_outlier', '3 条时长远离全库四分位', { unit: 'dataset' })] } : {}),
    } as unknown as Section['summary'];
    if (failed) return { id, state: 'failed', summary, tables: [], adjudication: null, error: 'task_success: 429 QuotaExceeded x20, circuit open, exit 4' };
    if (id === 'task_success') {
      // 10 questions to decide; its 5 rejects may be appealed (not pending, D42); the results came from the retry
      return { id, state: 'completed_with_errors', summary, tables: TABLES[id], adjudication: { pending: 10, appealable: 5 }, episodes_error: 2, fingerprints: { prompt: 'sha256:4be1…c02d', from_subtask: 'sub_retry1' } };
    }
    return { id, state: 'succeeded', summary, tables: TABLES[id], adjudication: null };
  });
  const rejects = quotaRun ? [{ module: 'timestamp_check', kind: 'finding' as const, code: 'fragment', item: 'STRM-5', count: 1 }] : [
    { module: 'task_success', kind: 'finding' as const, code: 'failure', item: 'TASK-4', count: 5 },
    { module: 'timestamp_check', kind: 'finding' as const, code: 'fragment', item: 'STRM-5', count: 1 },
  ];
  return {
    schema_version: '2.0',
    revision,
    overview: {
      dataset: { input: 'tos://pai-kit-datasets/lerobot/droid_100', source_digest: DIGEST2, episode_count: 100, cameras: CAMERAS, fps: 15, robot_type: null },
      run: { run_dir: '20260920-130514', revision, modules: MAIN_MODULES, adjudications_applied: 0 },
      counts: quotaRun ? { total: 50, passed: 0, rejected: 1, held: 49, review: 0, skipped: 0 } : { total: 50, passed: 42, rejected: 6, held: 2, review: 10, skipped: 0 },
      pass_rate: quotaRun ? 0 : 0.84,
      reject_reasons: rejects,
      reject_items: rejects.map((r) => ({ item: r.item, count: r.count })),
      token_usage: { prompt: 2_010_000, completion: 67_500, reasoning: 42_900, cached: 931_800, requests: 886, requests_unknown_usage: 41 },
      // The CLI cannot know the wall time of the runs (07 §5: the page derives it from the timeline).
      duration_s: null,
      policy: { preset: 'default', version: '2' },
      coverage: { ...coverageOf(MAIN_MODULES), unassessable: [{ item: 'ACT-4', reason: 'not_applicable', count: 50 }] },
      findings_by_item: [...byItem.entries()]
        .sort(([a], [b]) => (ITEM_ORDER.get(a) ?? 999) - (ITEM_ORDER.get(b) ?? 999))
        .map(([item, e]) => ({ item, episodes: e.episodes.size, blocking: e.blocking.size, review: e.review.size, info: e.info.size })),
    },
    modules: sections,
    skipped_modules: [{ id: 'kinematic_limits', reason: '预检没读到机器人型号（robot_type 为 unknown），创建任务时选择跳过；其余模块照常' }],
    integrity: {
      format: { kind: 'lerobot', version: 'v3', supported: true, detail: 'LeRobot v3.0, 100 episodes, 3 cameras' },
      validation: [],
      warnings: [],
      labels: { with_task: 50, without_task: 0 },
      profile: { matched: 'droid_100', by: 'repo_id' },
      robot_type: null,
      skipped_episodes: [],
    },
    perf: { vlm_requests: 886, vlm_wall_s: 900, effective_concurrency: 19.2, redone_after_interruption: 3 },
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
      return eps.flatMap((ep) => CAMERAS.map((camera, k) => ({ episode_index: ep, camera, score: Number((0.62 + ((ep + k * 3) % 9) * 0.04).toFixed(2)), sharpness: 100 + ((ep * 7 + k) % 60), exposure: 'ok' })));
    case 'video_action_sync':
      return eps.flatMap((ep) => CAMERAS.map((camera, k) => ({ episode_index: ep, camera, lag_s: Number((((ep + k) % 7) * 0.02 - 0.05).toFixed(2)), corr_peak: Number((0.55 + ((ep * 3 + k) % 8) * 0.05).toFixed(2)), status: ep === 3 && k === 1 ? 'flagged' : 'aligned' })));
    case 'task_success':
      return eps.map((ep) => ({ episode_index: ep, verdict: HELD.includes(ep) ? 'error' : TS_REJECTS.includes(ep) ? 'fail' : VERDICT_REVIEW.includes(ep) ? 'abstain' : 'pass', text_source: taskText(ep).source }));
    case 'duplicate_groups':
      return [
        { episode_index: 43, same_as: '44' },
        { episode_index: 44, same_as: '43' },
      ];
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
    latency: [lat('probe', 392, 3.1, 6.4, 11.2, 402, 4, 9)],
    effective_concurrency: 11.4,
    stages: [
      { id: 'numeric', wall_s: Math.round(9 * k), share: 0.01 },
      { id: 'frame', wall_s: Math.round(144 * k), share: 0.21 },
      { id: 'duplicate', wall_s: Math.round(140 * k), share: 0.2 },
      { id: 'vlm', wall_s: Math.round(542 * k), share: 0.78 },
      { id: 'final', wall_s: Math.round(1 * k), share: 0.001 },
      { id: 'report', wall_s: Math.round(6 * k), share: 0.01 },
      { id: 'verify', wall_s: Math.round(9 * k), share: 0.01 },
    ],
    retries: { outer_attempts: Math.round(12 * k), rescued: Math.round(9 * k) },
    merge: { requests: 0, estimated_unmerged: 0 },
    redone_after_interruption: scope === 'subtask' ? 0 : 3,
  };
}

// ------------------------------------------------------------------ adjudication

function decision(id: number, ep: number, line: Decision['line'], value: Decision['decision'], at: number, applied = false, newLabel: string | null = null): Decision {
  return { id, episode_index: ep, line, decision: value, new_label: newLabel, note: null, decided_by: 'galbot', decided_at: at, applied };
}

/** The task text a person wrote for ep 4 (what the episode really does). */
export const EP4_RELABEL = 'fold the cloth and put it on the table';

export function seedDecisions(now: number): Decision[] {
  return [
    // ep 4: the task text rewritten, the verdict left to the model (it is judged again, D39)
    decision(1, 4, 'task_verdict', 'unsure', now - 40 * MIN, false, EP4_RELABEL),
    decision(2, 13, 'task_verdict', 'success', now - 38 * MIN),
    decision(3, 9, 'task_verdict', 'success', now - 35 * MIN),
    decision(4, 16, 'task_verdict', 'unsure', now - 33 * MIN),
  ];
}

/**
 * The adjudication questions of the main task, before decisions are attached: one task verdict per episode task
 * success could not decide, naming the finding it asks about (C4 2.3.0).
 */
export function baseQuestions(): Map<number, AdjudicationCard['questions']> {
  const out = new Map<number, AdjudicationCard['questions']>();
  for (const ep of VERDICT_REVIEW) {
    out.set(ep, [
      {
        line: 'task_verdict' as const,
        source_module: 'task_success',
        reason: SPLIT_REVIEW.includes(ep) ? '整条判失败，但有机位说做成了：机位之间分歧，转人工' : '模型拿不准这一条做没做成，转人工',
        annotation: taskText(ep).text,
        latest_decision: null,
        codes: ['uncertain'],
        items: ['TASK-4'],
      },
    ]);
  }
  return out;
}

export function appealQuestions(): Map<number, AdjudicationCard['questions']> {
  const out = new Map<number, AdjudicationCard['questions']>();
  for (const ep of TS_REJECTS) {
    out.set(ep, [
      {
        line: 'reject_appeal',
        source_module: 'task_success',
        reason: '整条判失败，两路机位说失败、没有机位说做成：抓起后在半空松手，物体掉回桌面',
        annotation: taskText(ep).text,
        latest_decision: null,
        codes: ['failure'],
        items: ['TASK-4'],
      },
    ]);
  }
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
  push(3000, 'numeric', 'info', 'check --modules timestamp_check,motion_quality on 50 episodes, handed on one by one');
  for (const ep of EPISODES) push(4000 + ep * 180, 'numeric', ep === 18 ? 'warn' : 'info', ep === 18 ? 'episode 18 too short (0.5s); finding fragment' : `timestamp_check + motion_quality episode ${ep}: done`, ep);
  for (const ep of EPISODES) push(14_000 + ep * 2900, 'frame', 'info', `decode + visual_quality + video_action_sync episode ${ep}`, ep);
  push(160_000, 'duplicate', 'info', 'duplicate_check: 1 group of identical episodes (43, 44), reported only');
  for (let ep = 0; ep < 20; ep += 1) push(5000 + ep * 9000, 'vlm', 'debug', `episode ${ep}: probe request sent`, ep);
  push(186_000, 'vlm', 'error', 'task_success: probe requests answered 429 QuotaExceeded for 20 episodes in a row; circuit open, exit 4');
  push(6 * MIN, 'system', 'warn', 'system pause: daemon upgrade v2.0.3 -> v2.0.4; stopping after in-flight episodes');
  push(8 * MIN, 'system', 'info', 'system resume: continuing from checkpoint (3 in-flight episodes will be re-requested)');
  push(17 * MIN, 'system', 'info', 'main run finished: completed_with_errors (revision 1)');
  push(62 * MIN, 'system', 'info', 'subtask retry #1 started', null, 'sub_retry1');
  for (const ep of EPISODES) {
    push(62 * MIN + 3000 + ep * 4000, 'vlm', 'debug', `episode ${ep}: probe request sent`, ep, 'sub_retry1');
    push(62 * MIN + 5000 + ep * 4000, 'vlm', HELD.includes(ep) ? 'error' : 'info', HELD.includes(ep) ? `episode ${ep}: probe request timed out after 60s (attempt 3/3); marked as error` : `task_success episode ${ep}: done`, ep, 'sub_retry1');
  }
  push(69 * MIN, 'system', 'info', 'subtask retry #1 finished (revision 2)', null, 'sub_retry1');
  return out.sort((a, b) => a.ts - b.ts);
}

export function episodePreviews(uri: string, count: number): EpisodePreview[] {
  const p = profileFor(uri);
  if (p.format.kind === 'mcap' || p.format.kind === 'lance') {
    // D44: their videos are inside the files / tables - no camera URLs; an mcap task topic
    // is not read by the preview
    return Array.from({ length: count }, (_, i) => ({
      index: i,
      length_s: Number((8 + ((i * 7) % 17) + 0.4).toFixed(1)),
      task: p.format.kind === 'lance' && i < p.withTask ? TASK_TEXTS[i % TASK_TEXTS.length] : '',
      task_source: p.format.kind === 'lance' && i < p.withTask ? ('原始标注' as const) : ('无' as const),
      cameras: [],
      ...(p.format.kind === 'mcap' && i < p.withTask ? { task_unread: true } : {}),
    }));
  }
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
