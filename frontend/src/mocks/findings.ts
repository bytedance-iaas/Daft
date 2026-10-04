// The findings views' task (C2 2.0, design doc 17, F12.5): the main task's 50 episodes and story run the way the
// two blocks report them - every module judged every episode (D57), records 2.0 with findings, a report 2.0
// (policy, coverage, findings and rejects by item, every section's findings statistics), the episode list's
// items and levels, adjudication questions naming their codes, a plan and progress of two blocks. The main
// task stays a task made before (C2 1.0, D59): the console opens both.
import type {
  AdjudicationCard,
  EpisodeFinding,
  EpisodeView,
  Finding,
  ModuleSpec,
  PipelineEpisode,
  Plan,
  Report,
  ReportV2,
  ResultRecord,
  ResultRecordV2,
  StageProgress,
  Task,
  TaskEpisode,
} from '../api/types';
import {
  ALL_MODULES,
  CAMERAS,
  DUPLICATE_OF,
  HELD,
  LABEL_REVIEW,
  STUCK,
  SYNC_NOTE,
  TS_REJECTS,
  VERDICT_REVIEW,
  buildTask,
  episodeList,
  episodeView,
  mainReport,
  registry,
  stage,
  usageTotals,
} from './world';

export const FINDINGS_TASK = 'task_01HXT6F2';

const MIN = 60_000;
const EPISODES = Array.from({ length: 50 }, (_, ep) => ep);
const SELECTED = ALL_MODULES.filter((id) => id !== 'kinematic_limits' && id !== 'data_integrity');

const spec = (module: string): ModuleSpec => registry.modules.find((m) => m.id === module)!;
const codeOf = (module: string, code: string) => spec(module).codes.find((c) => c.code === code)!;

function finding(module: string, code: string, message: string, extra: Partial<Finding> = {}): Finding {
  const c = codeOf(module, code);
  return { code, item: c.item ?? null, severity: c.severity, message_zh: message, ...extra };
}

/** What each module of the main task's story finds on an episode, as the module's codes say it. */
function findingsOf(module: string, ep: number, rec: ResultRecord | undefined): Finding[] {
  const details = (rec?.details ?? {}) as Record<string, unknown>;
  switch (module) {
    case 'timestamp_check':
      return ep === 18 ? [finding(module, 'fragment', '全长只有 0.50 秒（不足 1 秒，疑似采集中断的碎片）', { time_s: [0, 0.5] })] : [];
    case 'motion_quality': {
      const out: Finding[] = [];
      if (STUCK.includes(ep)) out.push(finding(module, 'stuck', '关节 3 从第 40 帧到第 95 帧卡住不动', { frames: [40, 95], scope: { channel: 'joint_3' } }));
      if (ep % 3 === 0) out.push(finding(module, 'idle_opening', '开头空转 1.2 秒才开始动作', { time_s: [0, 1.2] }));
      return out;
    }
    case 'visual_quality':
      return ep % 7 === 2 ? [finding(module, 'exposure_low', `${CAMERAS[ep % 3]} 画面偏暗，曝光分 0.48`, { scope: { camera: CAMERAS[ep % 3] } })] : [];
    case 'video_action_sync': {
      const note = SYNC_NOTE[ep];
      if (note === 'annotated') return [finding(module, 'camera_misaligned', `${CAMERAS[1]} 比动作晚 0.42 秒`, { scope: { camera: CAMERAS[1] } })];
      if (note === 'suspect') return [finding(module, 'suspect', `${CAMERAS[0]} 疑似错位，证据不足`, { scope: { camera: CAMERAS[0] } })];
      if (note === 'undecidable' || note === 'no_motion') return [finding(module, 'undecidable', '画面里动作太少，测不准滞后')];
      return [];
    }
    case 'task_success':
      if (TS_REJECTS.includes(ep)) return [finding(module, 'failure', String(details.reason ?? '复核一致判未完成'), { time_s: [9.5, 13], evidence: [`details/evidence/task_success/ep${String(ep).padStart(6, '0')}_0.jpg`] })];
      if (VERDICT_REVIEW.includes(ep)) return [finding(module, 'uncertain', '证据不足，弃权：末态物证在灰区')];
      return [];
    case 'dedup': {
      const dup = DUPLICATE_OF[ep];
      return dup === undefined ? [] : [finding(module, 'duplicate', `与 ep${String(dup).padStart(6, '0')} 字节级完全重复`, { readings: { duplicate_of: dup, group_id: dup } })];
    }
    case 'skill_profile':
      return LABEL_REVIEW.includes(ep) ? [finding(module, 'label_disagreement', '标注与画面描述归入不同技能族')] : [];
    default:
      return [];
  }
}

/** A record of the story as a record 2.0: what it found, what it assessed (motion quality cannot assess actuator saturation here). */
function recordV2(module: string, ep: number, rec: ResultRecord | undefined): ResultRecordV2 {
  const base = { episode_index: ep, module, details: rec?.details ?? {}, evidence: rec?.evidence ?? [], elapsed_s: rec?.elapsed_s ?? 0.4 };
  if (rec?.verdict === 'error') return { ...base, status: 'error', findings: [], assessed: [], unassessable: [], readings: {}, error: rec.error };
  const unassessable =
    module === 'motion_quality' ? [{ item: 'ACT-4', reason: 'not_applicable', message_zh: '速度型指令和位置读数含义不同，执行器饱和无从比较' }] : [];
  const assessed = spec(module).covers.filter((i) => !unassessable.some((u) => u.item === i));
  return { ...base, status: 'ok', findings: findingsOf(module, ep, rec), assessed, unassessable, readings: rec?.score !== null && rec?.score !== undefined ? { score: rec.score } : {}, error: null };
}

/** Every selected module's record of the episode: the story's, and a clean one where the funnel used to stop it. */
function recordsOf(ep: number): Record<string, ResultRecordV2> {
  const old = episodeView(ep, 1).modules as Record<string, ResultRecord>;
  return Object.fromEntries(SELECTED.map((m) => [m, recordV2(m, ep, old[m])]));
}

/** The findings graded by the default policy (the registry's levels, P18), in module order. */
function graded(records: Record<string, ResultRecordV2>): EpisodeFinding[] {
  const out: EpisodeFinding[] = [];
  for (const m of SELECTED) {
    for (const f of records[m]?.findings ?? []) {
      const c = codeOf(m, f.code);
      out.push({ module: m, level: c.level, ...(c.level === 'review' && c.review_line ? { line: c.review_line } : {}), appealable: c.level === 'blocking' && c.appealable, finding: f });
    }
  }
  return out;
}

const ORDER = new Map(registry.taxonomy.items.map((it, i) => [it.id, i]));
const LEVEL_ORDER = ['blocking', 'review', 'info'] as const;

function itemsAndLevels(findings: EpisodeFinding[]): { items: string[]; levels: EpisodeFinding['level'][] } {
  const items = [...new Set(findings.map((f) => f.finding.item).filter((i): i is string => Boolean(i)))].sort((a, b) => (ORDER.get(a) ?? 999) - (ORDER.get(b) ?? 999));
  return { items, levels: LEVEL_ORDER.filter((lv) => findings.some((f) => f.level === lv)) };
}

/** The episode view (C4 2.3.0): records 2.0, every finding with its level, the reasons with their codes. */
export function findingsView(ep: number, revision: number): EpisodeView {
  const old = episodeView(ep, revision);
  const records = recordsOf(ep);
  const findings = graded(records);
  const reasons = findings
    .filter((f) => f.level === 'blocking')
    .map((f) => ({
      module: f.module,
      kind: f.module === 'dedup' ? 'duplicate' : 'finding',
      text: f.finding.message_zh,
      code: f.finding.code,
      item: f.finding.item,
      appealable: f.appealable,
      ...(f.module === 'dedup' ? { duplicate_of: DUPLICATE_OF[ep] } : {}),
    }));
  for (const r of old.reasons ?? []) if (r.kind === 'execution_error') reasons.push({ ...r, text: '「任务成败判定」执行出错（arbitration: timeout 60s，3 次），待补跑' } as (typeof reasons)[number]);
  // like review.json: a reject whose blocking findings may all be appealed is asked on the appeals tab
  const appeal = findings.filter((f) => f.level === 'blocking');
  const review = [...(old.review ?? [])];
  if (appeal.length && appeal.every((f) => f.appealable)) review.push({ module: appeal[0].module, kind: 'reject_appeal', text: appeal[0].finding.message_zh });
  return { ...old, reasons, review, modules: records, findings };
}

/** The episode list of the findings task: the main task's lists with the items and levels of each one's findings. */
export function findingsEpisodes(openQuestions: Map<number, string[]>): TaskEpisode[] {
  return EPISODES.map((ep) => {
    const { items, levels } = itemsAndLevels(graded(recordsOf(ep)));
    const list = episodeList(ep);
    const reason = list === 'reject' ? [ep === 18 ? 'timestamp_check' : ep === 44 ? 'dedup' : 'task_success'] : list === 'held' ? ['task_success'] : [];
    return { episode_index: ep, list, review: openQuestions.has(ep), reason_modules: reason, review_modules: openQuestions.get(ep) ?? [], items, levels };
  });
}

/** The report 2.0 of the findings task: the main task's sections with the statistics every 2.0 section has. */
export function findingsReport(): ReportV2 {
  const r1: Report = mainReport(2);
  const all = EPISODES.map((ep) => ({ ep, records: recordsOf(ep) }));
  const perEp = all.map(({ ep, records }) => ({ ep, findings: graded(records) }));
  const covered = [...new Set(SELECTED.flatMap((m) => spec(m).covers))];
  const checkable = registry.taxonomy.items.filter((i) => i.kind !== 'control').map((i) => i.id);
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
  const sections = r1.modules.map((s) => {
    const { gate: _gate, summary, ...rest } = s as typeof s & { gate?: string };
    const judged = all.filter((x) => x.records[s.id]?.status === 'ok');
    const codes = new Map<string, { item: string | null; level: string; eps: Set<number>; cams: Map<string, Set<number>> }>();
    const levels = { blocking: new Set<number>(), review: new Set<number>(), info: new Set<number>() };
    for (const { ep, records } of judged) {
      for (const f of records[s.id]?.findings ?? []) {
        const c = codeOf(s.id, f.code);
        const e = codes.get(f.code) ?? { item: f.item, level: c.level, eps: new Set<number>(), cams: new Map<string, Set<number>>() };
        e.eps.add(ep);
        if (f.scope?.camera) e.cams.set(f.scope.camera, (e.cams.get(f.scope.camera) ?? new Set<number>()).add(ep));
        codes.set(f.code, e);
        levels[c.level].add(ep);
      }
    }
    const n = judged.length;
    const { score_hist: hist, ...own } = summary as Record<string, unknown>;
    return {
      ...rest,
      summary: {
        ...own,
        assessed_episodes: n,
        flagged_episodes: new Set([...levels.blocking, ...levels.review, ...levels.info]).size,
        levels: { blocking: levels.blocking.size, review: levels.review.size, info: levels.info.size },
        items: [...codes.entries()].map(([code, e]) => ({
          item: e.item,
          code,
          level: e.level as 'blocking' | 'review' | 'info',
          episodes: e.eps.size,
          share: n ? Number((e.eps.size / n).toFixed(4)) : null,
          ...(e.cams.size ? { by_camera: [...e.cams.entries()].map(([camera, eps]) => ({ camera, episodes: eps.size })) } : {}),
        })),
        unassessable: s.id === 'motion_quality' ? [{ item: 'ACT-4', reason: 'not_applicable', count: n }] : [],
        ...(Array.isArray(hist) ? { score_hist: { score: hist } } : {}),
        ...(s.id === 'timestamp_check' ? { dataset_findings: [finding('timestamp_check', 'duration_outlier', '3 条时长远离全库四分位（ep 6、ep 23、ep 45）', { unit: 'dataset' })] } : {}),
      },
    };
  });
  return {
    ...(r1 as unknown as Omit<ReportV2, 'schema_version' | 'overview' | 'modules'>),
    schema_version: '2.0',
    revision: 1,
    overview: {
      ...r1.overview,
      counts: { total: 50, passed: 41, rejected: 7, held: 2, review: 10, skipped: 0 },
      pass_rate: 0.82,
      run: { ...r1.overview.run, revision: 1 },
      reject_reasons: [
        { module: 'task_success', kind: 'finding', code: 'failure', item: 'TASK-5', count: 5 },
        { module: 'timestamp_check', kind: 'finding', code: 'fragment', item: 'STRM-5', count: 1 },
        { module: 'dedup', kind: 'duplicate', code: 'duplicate', item: 'SET-1', count: 1 },
      ],
      reject_items: [
        { item: 'TASK-5', count: 5 },
        { item: 'SET-1', count: 1 },
        { item: 'STRM-5', count: 1 },
      ],
      policy: { preset: 'default', version: '1' },
      coverage: {
        taxonomy_version: registry.taxonomy_version,
        covered: checkable.filter((i) => covered.includes(i)),
        not_covered: checkable.filter((i) => !covered.includes(i)),
        unassessable: [{ item: 'ACT-4', reason: 'not_applicable', count: 50 }],
      },
      findings_by_item: [...byItem.entries()]
        .sort(([a], [b]) => (ORDER.get(a) ?? 999) - (ORDER.get(b) ?? 999))
        .map(([item, e]) => ({ item, episodes: e.episodes.size, blocking: e.blocking.size, review: e.review.size, info: e.info.size })),
    },
    modules: sections as unknown as ReportV2['modules'],
  };
}

/** The questions of the findings task: the main task's, each naming the finding codes it asks about (C4 2.3.0). */
export function withCodes(questions: Map<number, AdjudicationCard['questions']>): Map<number, AdjudicationCard['questions']> {
  const codes: Record<string, [string, string]> = { label: ['label_disagreement', 'LABEL-5'], task_verdict: ['uncertain', 'TASK-5'] };
  const out = new Map<number, AdjudicationCard['questions']>();
  for (const [ep, qs] of questions) {
    out.set(
      ep,
      qs.map((q) => {
        const pair = q.line === 'reject_appeal' ? (q.source_module === 'dedup' ? ['duplicate', 'SET-1'] : ['failure', 'TASK-5']) : codes[q.line];
        return pair ? { ...q, codes: [pair[0]], items: [pair[1]] } : q;
      }),
    );
  }
  return out;
}

/** A row of the live episode page of a two-block run (C4 2.2.0): every stage done, the provisional verdict. */
export function findingsPipelineRow(ep: number): PipelineEpisode {
  const list = episodeList(ep);
  const view = findingsView(ep, 1);
  const verdict = list === 'reject' ? 'drop' : list === 'held' ? 'held' : 'keep';
  return {
    episode_index: ep,
    reason: null,
    stages: { numeric: 'done', frame: 'done', vlm: HELD.includes(ep) ? 'error' : 'done' },
    provisional: true,
    verdict,
    verdict_reason: verdict === 'drop' ? view.reasons?.[0]?.text ?? null : verdict === 'held' ? '待补跑：「任务成败判定」执行出错' : null,
  };
}

/** The live module counts of the findings task (C4 2.3.0 `modules`). */
export function findingsModuleCounts(): { id: string; judged: number; error: number; flagged: number }[] {
  const per = EPISODES.map(recordsOf);
  return SELECTED.filter((m) => !spec(m).codes.every((c) => c.scope_kind === 'dataset')).map((m) => {
    const recs = per.map((r) => r[m]).filter(Boolean);
    const ok = recs.filter((r) => r.status === 'ok');
    return { id: m, judged: ok.length, error: recs.length - ok.length, flagged: ok.filter((r) => r.findings.length).length };
  });
}

/** The plan of the two blocks (C2 plan 2.0). */
export function findingsPlan(): Plan {
  return {
    schema_version: '2.0',
    vlm_parallelism: 64,
    limits: { cpu_concurrency: { value: 30, bound_by: 'planner' }, vlm_parallelism: { value: 64, bound_by: 'model' } },
    stages: [
      { id: 'numeric', kind: 'cpu', command: 'check', block: 'cpu', concurrency: 30, modules: ['timestamp_check', 'motion_quality'], episodes: 'selected' },
      { id: 'frame', kind: 'cpu', command: 'check', block: 'cpu', after: 'numeric', concurrency: 30, modules: ['visual_quality', 'video_action_sync'], episodes: 'selected' },
      { id: 'dedup', kind: 'cpu', command: 'check', block: 'cpu', after: 'frame', full_set: true, concurrency: 1, modules: ['dedup'], episodes: 'selected' },
      { id: 'autolabel', kind: 'vlm', command: 'autolabel', block: 'vlm', episodes: 'unlabeled', gates: { caption: 32 } },
      { id: 'vlm', kind: 'vlm', command: 'check', block: 'vlm', after: 'autolabel', modules: ['task_success', 'camera_defects'], episodes: 'selected', gates: { episode: 32, probe: 64, endstate: 64, arbitration: 32, guard_caption: 32 }, merge: { strategy: 'none', groups: [] } },
      { id: 'profile', kind: 'vlm', command: 'check', block: 'vlm', after: 'vlm', full_set: true, modules: ['skill_profile'], episodes: 'selected', gates: { caption: 32, llm: 16, audit: 16 }, merge: { strategy: 'none', groups: [] } },
      { id: 'final', kind: 'aggregate', command: 'aggregate', phase: 'final' },
    ],
    estimates: {
      vlm_requests: 800,
      wall_clock_s: 640,
      notes: [
        'rough estimate: every selected episode goes through both blocks, which run side by side; 22.7 s per request at 67% gate use (image-request baseline from v1, 2026-09-07; video latency is not calibrated)',
        'task_success arbitration and label-guard calls depend on the data and are not counted',
        'skill_profile text calls (taxonomy, label audit) are per dataset and not counted',
      ],
    },
  } as Plan;
}

/** A finished stage's streaming activity: the last batch, the time per episode, its run (minutes ago). */
function lane(now: number, last: number, mean: number, from: number, to: number): NonNullable<StageProgress['pipeline']> {
  return {
    inflight: 0, queued: 0, capacity: 8, dispatches: Math.ceil(last / 8),
    recent: [{ number: Math.ceil(last / 8), count: 2, episodes: [last - 2, last - 1], at: now - to * MIN }],
    started_at: now - from * MIN, finished_at: now - to * MIN, updated_at: now - to * MIN,
    processing: { count: last, total_s: Math.round(last * mean), mean_s: mean, min_s: mean / 2, max_s: mean * 3 },
    busy: [{ start: now - from * MIN, end: now - to * MIN }], held_by_downstream: false,
  } as NonNullable<StageProgress['pipeline']>;
}

/** The findings task: the main task's dataset and selection, a finished two-block run (one revision). */
export function findingsTask(now: number): Task {
  const blocks = (s: StageProgress, block: 'cpu' | 'vlm', fullSet = false): StageProgress => ({ ...s, block, ...(fullSet ? { full_set: true } : {}) });
  const t = buildTask({
    id: FINDINGS_TASK,
    name: 'droid 前 50 条质检（两块并行）',
    note: '发现与策略判决的新版报告',
    state: 'completed_with_errors',
    datasetId: 'ds_droid100',
    uri: 'tos://pai-kit-datasets/lerobot/droid_100',
    output: 'tos://pai-kit-deliveries/droid-50-findings',
    // older than the main task: droid_100's last task and the lists' first rows stay the main task's
    created: now - 3 * 60 * MIN,
    selected: SELECTED,
    episodes: { mode: 'head', n: 50 },
    total: 50,
    runId: '20260930-091502',
    stages: [
      // the per-episode stages report their streaming activity, as the Daemon's two blocks do (C4 1.18)
      blocks(stage('numeric', 'succeeded', 50, 50, 9, { pipeline: lane(now, 50, 0.18, 170, 162) }), 'cpu'),
      blocks(stage('frame', 'succeeded', 50, 50, 146, { pipeline: lane(now, 49, 2.9, 170, 146) }), 'cpu'),
      blocks(stage('dedup', 'succeeded', 50, 50, 12), 'cpu', true),
      blocks(stage('autolabel', 'succeeded', 22, 22, 46), 'vlm'),
      blocks(stage('vlm', 'completed_with_errors', 50, 50, 560, { note: '2 条出错（ep 7、ep 31），暂不交付，等待补跑', pipeline: lane(now, 50, 11.2, 169, 158) }), 'vlm'),
      blocks(stage('profile', 'succeeded', 50, 50, 140), 'vlm', true),
      stage('final', 'succeeded', 1, 1, 1),
      stage('export', 'succeeded', 41, 41, 64),
      stage('report', 'succeeded', 1, 1, 6),
      stage('verify', 'succeeded', 1, 1, 9),
    ],
    modules: {
      kinematic_limits: { availability: 'needs_input', unavailable_reason: '预检未读到机器人型号，创建时选择跳过', state: 'skipped' },
      task_success: { state: 'completed_with_errors', episodes_error: 2, elapsed_s: 560, error: null },
    },
    summary: { total: 50, passed: 41, rejected: 7, held: 2, review: 10, pass_rate: 0.82 },
    usage: usageTotals(2_090_000, 70_100, 44_000, 950_000, 912, 12),
    pending: 10,
    resultRev: 1,
  });
  return { ...t, params: { ...t.params, policy: { preset: 'default' } } };
}
