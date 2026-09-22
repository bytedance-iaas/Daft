// The mocks cannot drift from the contract: every fixture and every mock response is validated
// against docs/contracts/openapi.yaml (C4 1.1.0) and the C2 schemas it references; request bodies
// the UI sends are validated by the hook installed in src/test/setup.ts.
import { describe, expect, it } from 'vitest';
import { contractVersion, formatErrors, loadOpenApi, makeContract, versionBefore, KNOWN_CONTRACT_DEFECTS, type ContractDefect } from '../test/contract';
import { contract } from '../test/setup';
import { cardsOf, db, toListItem } from './db';
import {
  DATASET_PROFILES,
  EMBODIMENTS,
  MAIN_TASK,
  episodeView,
  mainPerf,
  mainPlan,
  mainReport,
  mainTimeline,
  mainUsage,
  preflightFor,
  registry,
} from './world';

function expectValid(ref: string, value: unknown) {
  const v = contract.validator(ref);
  const ok = v(value);
  expect(ok, `${ref}\n${formatErrors(v.errors)}`).toBe(true);
}

const S = (name: string) => contract.schemaRef(name);

describe('fixtures match the contract', () => {
  it('module registry is the contract file itself', () => {
    expectValid(S('ModuleRegistry'), registry);
    expect(registry.modules.map((m) => m.id)).toContain('task_success');
  });

  it('credentials, backends, datasets, tasks', () => {
    db.credentials.forEach((c) => expectValid(S('Credential'), c));
    db.backends.forEach((b) => expectValid(S('VlmBackend'), b));
    db.datasets.forEach((d) => expectValid(S('DatasetDetail'), d));
    db.tasks.forEach((t) => expectValid(S('Task'), t));
    db.tasks.forEach((t) => expectValid(S('TaskListItem'), toListItem(t)));
  });

  it('preflight results for every dataset profile and input combination', () => {
    for (const p of DATASET_PROFILES) {
      for (const opts of [{}, { vlmBackend: 'ark-prod' }, { embodiment: 'franka' }, { embodiment: 'koch' }]) {
        expectValid('https://curator.contracts/cli/preflight.schema.json', preflightFor(p, opts));
      }
    }
    expect(EMBODIMENTS).toHaveLength(9);
  });

  it('report, plan, perf, timeline, usage, episodes and adjudication of the main task', () => {
    expectValid('https://curator.contracts/cli/report.schema.json', mainReport(1));
    expectValid('https://curator.contracts/cli/report.schema.json', mainReport(2));
    expectValid('https://curator.contracts/cli/plan.schema.json', mainPlan());
    for (const scope of ['all', 'main', 'subtask'] as const) expectValid(S('Perf'), mainPerf(scope, scope === 'subtask' ? 'sub_retry1' : null, 2));
    mainTimeline(Date.now()).forEach((e) => expectValid(S('TimelineEntry'), e));
    expectValid(S('UsageReport'), mainUsage());
    for (let ep = 0; ep < 50; ep += 1) expectValid(S('EpisodeView'), episodeView(ep, 2));
    [...cardsOf(MAIN_TASK, 'review'), ...cardsOf(MAIN_TASK, 'appeals')].forEach((c) => expectValid(S('AdjudicationCard'), c));
  });
});

describe('the known contract defects are still there (remove the patch when they are fixed)', () => {
  const doc = loadOpenApi();
  const version = contractVersion(doc);
  const raw = makeContract(doc);
  const instances: Record<ContractDefect['id'], () => [string, unknown]> = {
    DatasetDetail: () => ['DatasetDetail', db.datasets[0]],
    Decision: () => ['Decision', cardsOf(MAIN_TASK, 'review').flatMap((c) => c.questions).find((q) => q.latest_decision)?.latest_decision],
    'Task.vlm': () => ['Task', { ...db.tasks[3], vlm: { backend: 'ark-prod', model: 'm', reasoning_effort: null, snapshot: {} } }],
  };
  for (const d of KNOWN_CONTRACT_DEFECTS) {
    const fixed = d.fixedIn !== undefined && !versionBefore(version, d.fixedIn);
    it(`C4 ${version}: ${d.what} ${fixed ? `is fixed (since ${d.fixedIn})` : 'still rejects valid instances'}`, () => {
      const [schema, instance] = instances[d.id]();
      expect(instance).toBeTruthy();
      // A defect that disappears before its recorded fix means: delete it from KNOWN_CONTRACT_DEFECTS.
      expect(raw.validator(raw.schemaRef(schema))(instance)).toBe(fixed);
    });
  }
});

// ------------------------------------------------------------------ every operation, over HTTP

interface Call {
  op: string;
  method: string;
  path: string;
  body?: unknown;
  headers?: Record<string, string>;
}

const T = MAIN_TASK;
const calls = (): Call[] => [
  { op: 'listCredentials', method: 'GET', path: '/credentials' },
  { op: 'createCredential', method: 'POST', path: '/credentials', body: { name: 'new-key', access_key_id: 'AKLTnew0001', secret_access_key: 's3cr3t', region: 'cn-beijing' } },
  { op: 'updateCredential', method: 'PUT', path: '/credentials/cred_partner', body: { test_bucket: 'partner-bucket' } },
  { op: 'verifyCredential', method: 'POST', path: '/credentials/cred_prod/verify' },
  { op: 'deleteCredential', method: 'DELETE', path: '/credentials/cred_prod' },
  { op: 'deleteCredential', method: 'DELETE', path: '/credentials/cred_partner' },
  { op: 'listVlmBackends', method: 'GET', path: '/vlm-backends' },
  { op: 'createVlmBackend', method: 'POST', path: '/vlm-backends', body: { name: 'ark-new', kind: 'ark', endpoint: 'https://ark.cn-beijing.volces.com/api/v3', api_key: 'k' } },
  { op: 'createVlmBackend', method: 'POST', path: '/vlm-backends', body: { name: 'ark-nokey', kind: 'custom', endpoint: 'http://x/v1' } },
  { op: 'updateVlmBackend', method: 'PUT', path: '/vlm-backends/vb_vllm', body: { max_concurrency: 16 } },
  { op: 'verifyVlmBackend', method: 'POST', path: '/vlm-backends/vb_ark_prod/verify' },
  { op: 'refreshVlmModels', method: 'POST', path: '/vlm-backends/vb_ark_ep/refresh-models' },
  { op: 'refreshVlmModels', method: 'POST', path: '/vlm-backends/vb_ark_prod/refresh-models' },
  { op: 'addVlmModel', method: 'POST', path: '/vlm-backends/vb_ark_ep/models', body: { model_name: 'ep-20260921000000-abcde' } },
  { op: 'addVlmModel', method: 'POST', path: '/vlm-backends/vb_ark_ep/models', body: { model_name: 'bad-model' } },
  { op: 'updateVlmModel', method: 'PATCH', path: '/vlm-backends/vb_ark_prod/models/vm_pro', body: { reasoning_effort: 'high', max_concurrency: 32 } },
  { op: 'deleteVlmModel', method: 'DELETE', path: '/vlm-backends/vb_ark_prod/models/vm_16' },
  { op: 'deleteVlmBackend', method: 'DELETE', path: '/vlm-backends/vb_ark_prod' },
  { op: 'deleteVlmBackend', method: 'DELETE', path: '/vlm-backends/vb_vllm' },
  { op: 'getModules', method: 'GET', path: '/modules' },
  { op: 'getOverview', method: 'GET', path: '/overview' },
  { op: 'listDatasets', method: 'GET', path: '/datasets?page=1&page_size=10&check_state=changed' },
  { op: 'createDataset', method: 'POST', path: '/datasets', body: { input: { source: 'tos', uri: 'tos://pai-kit-datasets/lerobot/new_set', region: 'cn-beijing', credential: 'readonly-tos' } } },
  { op: 'createDataset', method: 'POST', path: '/datasets', body: { input: { source: 'tos', uri: 'tos://pai-kit-datasets/lerobot/droid_100', region: 'cn-beijing', credential: 'readonly-tos' } } },
  { op: 'browseDatasets', method: 'GET', path: '/datasets/browse?source=public' },
  { op: 'browseDatasets', method: 'GET', path: '/datasets/browse?source=tos&uri=tos://pai-kit-datasets/lerobot&credential=readonly-tos' },
  { op: 'listDatasetEpisodes', method: 'GET', path: '/datasets/episodes?dataset_id=ds_droid100&limit=48' },
  { op: 'getDataset', method: 'GET', path: '/datasets/ds_droid200' },
  { op: 'updateDataset', method: 'PATCH', path: '/datasets/ds_umi', body: { note: 'UMI 无标注基线' } },
  { op: 'recheckDataset', method: 'POST', path: '/datasets/ds_droid200/recheck' },
  { op: 'recheckDataset', method: 'POST', path: '/datasets/ds_umi/recheck' },
  { op: 'repreflightDataset', method: 'POST', path: '/datasets/ds_droid200/repreflight' },
  { op: 'deleteDataset', method: 'DELETE', path: '/datasets/ds_umi' },
  { op: 'deleteDataset', method: 'DELETE', path: '/datasets/ds_mcap' },
  { op: 'preflight', method: 'POST', path: '/preflight', body: { input: { dataset_id: 'ds_droid100' }, vlm_backend: 'ark-prod' } },
  { op: 'preflight', method: 'POST', path: '/preflight', body: { input: { source: 'tos', uri: 'tos://pai-kit-datasets/nope/x', region: 'cn-beijing', credential: 'readonly-tos' } } },
  { op: 'probeDelivery', method: 'POST', path: '/deliveries/probe', body: { uri: 'tos://pai-kit-deliveries/x', region: 'cn-beijing', credential: 'prod-tos' } },
  { op: 'probeDelivery', method: 'POST', path: '/deliveries/probe', body: { uri: 'tos://pai-kit-datasets/deliveries', credential: 'readonly-tos' } },
  { op: 'listTasks', method: 'GET', path: '/tasks?page=2&page_size=10' },
  { op: 'listTasks', method: 'GET', path: '/tasks?state=deleted' },
  { op: 'listTasks', method: 'GET', path: '/tasks?module=task_success,dedup&q=droid' },
  { op: 'getTask', method: 'GET', path: `/tasks/${T}` },
  { op: 'getTask', method: 'GET', path: '/tasks/task_nope' },
  { op: 'updateTask', method: 'PATCH', path: `/tasks/${T}`, body: { name: 'droid 前 50 条质检（复核）' }, headers: { 'If-Match': 'stale' } },
  { op: 'deleteTask', method: 'DELETE', path: '/tasks/task_01HXR4M7' },
  { op: 'deleteTask', method: 'DELETE', path: '/tasks/task_01HX1001' },
  { op: 'restoreTask', method: 'POST', path: '/tasks/task_01HXDEL1/restore' },
  { op: 'purgeTaskArtifacts', method: 'POST', path: `/tasks/${T}/purge-artifacts`, body: { confirm_path: 'tos://pai-kit-deliveries/droid-50/20260920-130514/' } },
  { op: 'purgeTaskArtifacts', method: 'POST', path: `/tasks/${T}/purge-artifacts`, body: { confirm_path: 'tos://wrong/' } },
  { op: 'rebindTaskCredentials', method: 'POST', path: `/tasks/${T}/rebind-credentials`, body: { output_credential: 'prod-tos' } },
  { op: 'taskAction', method: 'POST', path: '/tasks/task_01HXR4M7/actions/pause' },
  { op: 'taskAction', method: 'POST', path: '/tasks/task_01HXR6T3/actions/start' },
  { op: 'taskAction', method: 'POST', path: `/tasks/${T}/actions/pause` },
  { op: 'retryTask', method: 'POST', path: `/tasks/${T}/retry`, body: { modules: ['task_success'] } },
  { op: 'retryTask', method: 'POST', path: `/tasks/${T}/retry`, body: {} },
  { op: 'continueTask', method: 'POST', path: '/tasks/task_01HXPJ3C/continue' },
  { op: 'reexportTask', method: 'POST', path: '/tasks/task_01HXQ5R9/reexport' },
  { op: 'listSubtasks', method: 'GET', path: `/tasks/${T}/subtasks` },
  { op: 'getTaskTimeline', method: 'GET', path: `/tasks/${T}/timeline` },
  { op: 'getTaskTimeline', method: 'GET', path: '/tasks/task_01HXPX4W/timeline' },
  { op: 'getTaskPlan', method: 'GET', path: `/tasks/${T}/plan` },
  { op: 'getTaskPlan', method: 'GET', path: '/tasks/task_01HXR6T3/plan' },
  { op: 'getTaskLogs', method: 'GET', path: `/tasks/${T}/logs?limit=50&level=warn` },
  { op: 'getTaskUsage', method: 'GET', path: `/tasks/${T}/usage` },
  { op: 'getTaskUsage', method: 'GET', path: '/tasks/task_01HXQ5R9/usage' },
  { op: 'getReport', method: 'GET', path: `/tasks/${T}/report` },
  { op: 'getReport', method: 'GET', path: `/tasks/${T}/report?rev=1` },
  { op: 'getReport', method: 'GET', path: '/tasks/task_01HXQ5R9/report' },
  { op: 'getReportTable', method: 'GET', path: `/tasks/${T}/report/tables/visual_quality?limit=100&sort=score&order=desc` },
  { op: 'getReportTable', method: 'GET', path: `/tasks/${T}/report/tables/visual_quality?sort=nope` },
  { op: 'getEpisode', method: 'GET', path: `/tasks/${T}/episodes/29` },
  { op: 'getPerf', method: 'GET', path: `/tasks/${T}/perf?scope=subtask&subtask=sub_retry1` },
  { op: 'listAdjudication', method: 'GET', path: `/tasks/${T}/adjudication?status=all` },
  { op: 'listAdjudication', method: 'GET', path: `/tasks/${T}/adjudication?tab=appeals&status=all&source=task_success` },
  { op: 'submitAdjudication', method: 'POST', path: `/tasks/${T}/adjudication`, body: { decisions: [{ episode_index: 29, line: 'label', decision: 'custom_label', new_label: 'pour rice into the bowl' }] } },
  { op: 'submitAdjudication', method: 'POST', path: `/tasks/${T}/adjudication`, body: { decisions: [{ episode_index: 18, line: 'reject_appeal', decision: 'restore' }] } },
  { op: 'applyAdjudication', method: 'POST', path: `/tasks/${T}/adjudication/apply`, body: { relabel_rerun: 'full' } },
  { op: 'signMedia', method: 'GET', path: `/media/sign?task=${T}&scope=input&path=videos/chunk-000/wrist/file-000.mp4&ttl=600` },
];

const createAndRepreflight = async (): Promise<Call[]> => {
  // A task on the changed droid-200 dataset: start → 409 source_changed → repreflight.
  const pf = await (await fetch('http://localhost/api/v1/preflight', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ input: { dataset_id: 'ds_droid200' } }) })).json();
  const body = {
    name: 'droid-200 复检',
    input: { dataset_id: 'ds_droid200' },
    output: { uri: 'tos://pai-kit-deliveries/droid-200-recheck', region: 'cn-beijing', credential: 'prod-tos' },
    preflight_id: pf.preflight_id,
    episodes: { mode: 'head', n: 20 },
    modules: ['timestamp_check', { id: 'video_action_sync', params: { sync_plots: 'all' } }],
    params: { start_now: false },
  };
  const created = await (await fetch('http://localhost/api/v1/tasks', { method: 'POST', headers: { 'content-type': 'application/json', 'Idempotency-Key': 'contract-test-0001' }, body: JSON.stringify(body) })).json();
  return [
    { op: 'createTask', method: 'POST', path: '/tasks', body: { ...body, name: 'droid-200 复检 2' } },
    { op: 'createTasksBatch', method: 'POST', path: '/tasks/batch', body: { items: [{ name: 'batch a', input: { dataset_id: 'ds_droid100' }, output: body.output, preflight_id: pf.preflight_id }], shared: { episodes: { mode: 'all' }, modules: ['timestamp_check'], params: { start_now: false } } } },
    { op: 'taskAction', method: 'POST', path: `/tasks/${created.id}/actions/start` },
    { op: 'repreflightTask', method: 'POST', path: `/tasks/${created.id}/repreflight` },
    { op: 'updateTask', method: 'PATCH', path: `/tasks/${created.id}`, body: { note: 'x' }, headers: { 'If-Match': 'stale' } },
  ];
};

describe('every mocked operation answers what the contract says', () => {
  it('responses validate against the operation schema for their status', async () => {
    const all = [...calls(), ...(await createAndRepreflight())];
    const seen = new Set<string>();
    for (const c of all) {
      const res = await fetch(`http://localhost/api/v1${c.path}`, {
        method: c.method,
        headers: { ...(c.body !== undefined ? { 'content-type': 'application/json' } : {}), ...(c.headers ?? {}) },
        body: c.body !== undefined ? JSON.stringify(c.body) : undefined,
      });
      seen.add(c.op);
      if (res.status === 204) continue;
      const json = await res.json();
      const ref = contract.responseRef(c.op, res.status);
      expect(ref, `${c.op} ${res.status}`).toBeTruthy();
      expectValid(ref!, json);
    }
    // Every REST operation of C4 is mocked (SSE and probes aside).
    const expected = contract
      .operations()
      .map((o) => o.operationId)
      .filter((id) => !['taskEvents', 'healthz', 'readyz'].includes(id));
    expect([...new Set(expected)].filter((id) => !seen.has(id))).toEqual([]);
  });
});
