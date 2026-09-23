import { Button, Card, Collapse, Descriptions, Empty, Popover, Progress, Radio, Space, Spin, Table, Tag, Typography } from '@arco-design/web-react';
import type { ColumnProps } from '@arco-design/web-react/es/Table';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { errorMessage, isApiError } from '../../api/errors';
import { moduleName, qk, useModules } from '../../api/queries';
import type { ModuleState, Plan, Subtask, Task, TimelineEntry, UsageRow } from '../../api/types';
import { RelTime } from '../../components/RelTime';
import { regionLabel } from '../../components/RegionSelect';
import { MODULE_STATE_COLOR } from '../../features/tasks/ModuleSummary';
import { confirmModuleRetry } from '../../features/tasks/retryModule';
import { absoluteTime, bytes, compactNumber, percent } from '../../lib/format';
import { subtaskName } from '../../lib/reportView';
import { summaryDigest } from '../../lib/summary';
import { activeSubtask, groupStages, isTerminalState, progressStages, stageLabel, subtaskLabel } from '../../lib/taskView';
import { zh } from '../../locales/zh';
import { PipelineEpisodesCard } from './PipelineEpisodesCard';
import { PipelineActivity } from './PipelineActivity';

function Stat({ label, value, foot }: { label: string; value: string | number; foot?: string }) {
  return (
    <div className="stat-cell">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {foot ? <div className="stat-foot">{foot}</div> : null}
    </div>
  );
}

function ReportSummary({ task }: { task: Task }) {
  const s = task.summary;
  return (
    <Card
      title={
        <Space>
          {zh.taskDetail.reportOverview}
          {task.result_rev ? <Tag size="small">{zh.taskDetail.resultRev(task.result_rev)}</Tag> : null}
        </Space>
      }
    >
      {/* The header carries 查看报告 and 人工裁决: no second set of links here (requester item 10). */}
      {s ? (
        <>
          <div className="stat-grid" data-testid="report-summary">
            <Stat label={zh.taskDetail.total} value={s.total} />
            <Stat label={zh.taskDetail.passed} value={s.passed} foot={s.review ? zh.taskDetail.passedFoot(s.review) : undefined} />
            <Stat label={zh.taskDetail.rejected} value={s.rejected} />
            <Stat label={zh.taskDetail.held} value={s.held} foot={zh.taskDetail.heldFoot} />
            <Stat label={zh.taskDetail.review} value={task.pending_adjudication} />
            <Stat label={zh.taskDetail.passRate} value={percent(s.pass_rate)} foot={zh.taskDetail.passRateFoot} />
            {s.skipped ? <Stat label={zh.taskDetail.skipped} value={s.skipped} foot={zh.taskDetail.skippedFoot} /> : null}
          </div>
          <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0, fontSize: 12 }}>
            {zh.taskDetail.summaryNote}
          </Typography.Paragraph>
        </>
      ) : (
        <Typography.Text type="secondary">{zh.taskDetail.noReport}</Typography.Text>
      )}
    </Card>
  );
}

/**
 * 分档进度 (07 §4.2): one bar per stage, 终判 + 报告 and 导出 + 交付核验 each shown as one
 * (requester item 11), with counts and time used; no time estimate (item 20). While a subtask
 * runs, its own stages (Subtask.progress) replace the finished main run's (D46). When the funnel
 * stages report streaming-pipeline activity, they are drawn as one PipelineActivity chart and the
 * other stages keep their bars.
 */
function StagesCard({ task, subtasks }: { task: Task; subtasks: readonly Subtask[] }) {
  const active = activeSubtask(task);
  const listed = active ? subtasks.find((s) => s.id === active.id) : undefined;
  const raw = active ? (progressStages(listed?.progress).length ? progressStages(listed?.progress) : progressStages(active.progress)) : task.progress.stages;
  const lanes = raw.filter((s) => ['numeric', 'frame', 'vlm'].includes(s.id));
  const showPipeline = lanes.some((s) => s.pipeline);
  const stages = groupStages(showPipeline ? raw.filter((s) => !lanes.includes(s)) : raw);
  return (
    <Card
      title={
        <Space>
          {zh.taskDetail.stages}
          {active ? (
            <Tag size="small" color="arcoblue" data-testid="stages-subtask">
              {zh.taskDetail.stagesOfSubtask(subtaskLabel(active, subtasks))}
            </Tag>
          ) : null}
        </Space>
      }
    >
      {!stages.length && !showPipeline ? (
        <Typography.Text type="secondary">{active ? zh.taskDetail.subtaskNoStages : zh.taskDetail.noStages}</Typography.Text>
      ) : (
        <div data-testid="stages">
          {showPipeline ? <PipelineActivity task={task} stages={lanes} /> : null}
          {stages.map((s) => {
            const status = s.state === 'failed' ? 'error' : s.state === 'completed_with_errors' ? 'warning' : s.state === 'succeeded' ? 'success' : 'normal';
            return (
              <div key={s.key} style={{ marginBottom: 12 }} data-testid={`stage-${s.key}`}>
                <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                  <span>
                    <b>{s.label}</b> <span className="muted">{zh.stageState[s.state] ?? s.state}</span>
                  </span>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {s.total ? `${s.done} / ${s.total}` : ''}
                    {s.elapsed_s !== null ? ` · ${zh.taskDetail.stageTime(zh.time.duration(s.elapsed_s))}` : ''}
                  </span>
                </Space>
                <Progress percent={s.percent} status={status} showText={false} size="small" />
                {s.note ? (
                  <div className="muted" style={{ fontSize: 12 }}>
                    {s.note}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

interface UsageSum {
  key: string;
  prompt: number;
  completion: number;
  reasoning: number;
  cached: number;
  requests: number;
  unknown: number;
}

/** Usage rows added up by `key`, in the order the keys first appear. */
function sumRows(rows: readonly UsageRow[], key: (r: UsageRow) => string): UsageSum[] {
  const out = new Map<string, UsageSum>();
  for (const r of rows) {
    const k = key(r);
    const cur = out.get(k) ?? { key: k, prompt: 0, completion: 0, reasoning: 0, cached: 0, requests: 0, unknown: 0 };
    cur.prompt += r.prompt_tokens;
    cur.completion += r.completion_tokens;
    cur.reasoning += r.reasoning_tokens;
    cur.cached += r.cached_tokens;
    cur.requests += r.requests;
    cur.unknown += r.requests_unknown_usage;
    out.set(k, cur);
  }
  return [...out.values()];
}

type TokenView = 'module' | 'subtask';

/**
 * Token 消耗 (07 §4.2, D12: usage only, never money). The totals, then a 明细 fold (requester
 * item 13, as in the task-detail mockup) by module or by main run / subtask, with a 合计 row.
 * Only the actual-call ledger is summed (01 §2.6).
 */
function TokensCard({ task, subtasks }: { task: Task; subtasks: Subtask[] }) {
  const reg = useModules();
  const usage = useQuery({ queryKey: qk.usage(task.id), queryFn: () => unwrap(api().GET('/tasks/{id}/usage', { params: { path: { id: task.id } } })) });
  const [view, setView] = useState<TokenView>('module');
  const u = task.usage;
  const moduleLabel = (id: string) =>
    id === 'autolabel' ? zh.stage.autolabel : id.includes('+') ? zh.taskDetail.mergedModules(id.split('+').map((m) => moduleName(reg.data, m)).join('、')) : moduleName(reg.data, id);
  const rows = usage.data?.actual ?? [];
  const data = sumRows(rows, view === 'module' ? (r) => moduleLabel(r.module_id) : (r) => subtaskName(subtasks, r.subtask_id));
  const total = sumRows(rows, () => zh.taskDetail.tokenTotal)[0];
  const tokens = (v: number) => compactNumber(v);
  const count = (v: number) => v.toLocaleString('en-US');
  const numbers: { key: keyof UsageSum; title: string; fmt: (v: number) => string }[] = [
    { key: 'requests', title: zh.taskDetail.tokenColRequests, fmt: count },
    { key: 'prompt', title: zh.taskDetail.tokenInput, fmt: tokens },
    { key: 'completion', title: zh.taskDetail.tokenOutput, fmt: tokens },
    { key: 'reasoning', title: zh.taskDetail.tokenReasoning, fmt: tokens },
    { key: 'cached', title: zh.taskDetail.tokenCached, fmt: tokens },
  ];
  const columns: ColumnProps<UsageSum>[] = [
    // Room for a six-character module name on one line; merged-request names may wrap.
    { title: view === 'module' ? zh.taskDetail.colModule : zh.taskDetail.tokenColRun, dataIndex: 'key', width: 136 },
    ...numbers.map((n) => ({ title: n.title, dataIndex: n.key, align: 'right' as const, render: (v: number) => <span className="nowrap">{n.fmt(v)}</span> })),
  ];
  return (
    <Card title={zh.taskDetail.tokens}>
      <div className="stat-grid" data-testid="token-totals">
        <Stat label={zh.taskDetail.tokenInput} value={compactNumber(u.prompt_tokens)} foot={zh.taskDetail.tokenInputFoot} />
        <Stat label={zh.taskDetail.tokenOutput} value={compactNumber(u.completion_tokens)} foot={zh.taskDetail.tokenOutputFoot} />
        <Stat label={zh.taskDetail.tokenReasoning} value={compactNumber(u.reasoning_tokens)} foot={u.completion_tokens ? zh.taskDetail.tokenShareOf(zh.taskDetail.tokenOutput, percent(u.reasoning_tokens / u.completion_tokens, 0)) : undefined} />
        <Stat label={zh.taskDetail.tokenCached} value={compactNumber(u.cached_tokens)} foot={u.prompt_tokens ? zh.taskDetail.tokenShareOf(zh.taskDetail.tokenInput, percent(u.cached_tokens / u.prompt_tokens, 0)) : undefined} />
        <Stat label={zh.taskDetail.tokenRequests} value={u.requests.toLocaleString('en-US')} foot={u.requests_unknown_usage ? zh.taskDetail.tokenUnknown(u.requests_unknown_usage) : undefined} />
      </div>
      <Collapse className="token-fold" bordered={false} defaultActiveKey={['detail']}>
        <Collapse.Item
          name="detail"
          header={zh.taskDetail.tokenDetail}
          extra={
            <Radio.Group type="button" size="mini" value={view} onChange={(v: TokenView) => setView(v)} aria-label={zh.taskDetail.tokenDetail}>
              <Radio value="module">{zh.taskDetail.tokenByModule}</Radio>
              <Radio value="subtask">{zh.taskDetail.tokenBySubtask}</Radio>
            </Radio.Group>
          }
        >
          {rows.length ? (
            <Table
              rowKey="key"
              size="small"
              pagination={false}
              columns={columns}
              data={data}
              scroll={{ x: 520 }}
              data-testid="token-table"
              summary={() =>
                total ? (
                  <Table.Summary.Row>
                    <Table.Summary.Cell>
                      <b>{zh.taskDetail.tokenTotal}</b>
                    </Table.Summary.Cell>
                    {numbers.map((n) => (
                      <Table.Summary.Cell key={n.key} style={{ textAlign: 'right' }}>
                        <b>{n.fmt(total[n.key] as number)}</b>
                      </Table.Summary.Cell>
                    ))}
                  </Table.Summary.Row>
                ) : null
              }
            />
          ) : (
            <Typography.Text type="secondary">{zh.taskDetail.tokenNone}</Typography.Text>
          )}
          <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
            {zh.taskDetail.tokenNote}
          </div>
        </Collapse.Item>
      </Collapse>
    </Card>
  );
}

/** Which log stage a module runs in: from the plan, else its registry stage. */
function logStageOf(plan: Plan | undefined, moduleId: string, registryStage: string | undefined): string {
  const s = plan?.stages.find((x) => x.modules?.includes(moduleId));
  return s?.id ?? registryStage ?? '';
}

function ErrorEpisodes({ taskId, stage }: { taskId: string; stage: string }) {
  const q = useQuery({
    queryKey: qk.logs(taskId, { stage, level: 'error', errors: true }),
    queryFn: () => unwrap(api().GET('/tasks/{id}/logs', { params: { path: { id: taskId }, query: { stage, level: 'error', limit: 200 } } })),
  });
  if (q.isLoading) return <Spin size={14} tip={zh.taskDetail.errorsLoading} />;
  const lines = (q.data?.items ?? []).filter((l) => l.episode_index !== null && l.episode_index !== undefined);
  const byEp = new Map<number, string>();
  for (const l of lines) byEp.set(l.episode_index!, l.msg);
  if (!byEp.size) return <Typography.Text type="secondary">{zh.taskDetail.errorsNone}</Typography.Text>;
  return (
    <ul style={{ margin: 0, paddingLeft: 18 }} data-testid="error-episodes">
      {[...byEp.entries()].map(([ep, msg]) => (
        <li key={ep}>
          <b>ep {ep}</b> <span className="mono">{msg}</span>
        </li>
      ))}
    </ul>
  );
}

function ModulesCard({ task, plan, digest }: { task: Task; plan: Plan | undefined; digest: Record<string, string> }) {
  const reg = useModules();
  const qc = useQueryClient();
  const [open, setOpen] = useState<string[]>([]);
  const selected = task.modules.filter((m) => m.selected);
  const skipped = task.modules.filter((m) => !m.selected && m.availability !== 'available');
  const terminal = isTerminalState(task.state);
  const retry = (m: ModuleState) =>
    confirmModuleRetry({ taskId: task.id, moduleId: m.id, name: moduleName(reg.data, m.id) || m.name, qc, onDone: () => void qc.invalidateQueries({ queryKey: ['task', task.id] }) });
  // Widths (07 §4.2): the state, the count and the duration read on one line at any window size;
  // only the summary column gives way.
  const columns: ColumnProps<ModuleState>[] = [
    { title: zh.taskDetail.colModule, dataIndex: 'name', width: 140, render: (_: unknown, m) => <b>{moduleName(reg.data, m.id) || m.name}</b> },
    { title: zh.taskDetail.colStage, key: 'stage', dataIndex: 'id', width: 100, render: (_: unknown, m) => stageLabel(reg.data?.modules.find((x) => x.id === m.id)?.stage ?? '') },
    {
      title: zh.taskDetail.colState,
      dataIndex: 'state',
      width: 110,
      render: (_: unknown, m) => (
        <span className="nowrap">
          <span className="dot" style={{ background: MODULE_STATE_COLOR[m.selected ? m.state : 'skipped'] }} />
          {m.selected ? zh.moduleState[m.state] ?? m.state : zh.moduleState.skipped}
        </span>
      ),
    },
    { title: zh.taskDetail.colTotal, dataIndex: 'episodes_total', width: 100, render: (_: unknown, m) => (m.selected ? m.episodes_total : '—') },
    {
      title: zh.taskDetail.processingElapsed,
      dataIndex: 'elapsed_s',
      width: 150,
      render: (_: unknown, m) => {
        const stage = reg.data?.modules.find((x) => x.id === m.id)?.stage;
        const mean = task.progress.stages.find((s) => s.id === stage)?.pipeline?.processing?.mean_s;
        if (stage && ['numeric', 'frame', 'vlm'].includes(stage)) {
          return <span className="nowrap">{mean != null ? zh.taskDetail.perEpisode(mean.toFixed(2)) : '—'}</span>;
        }
        return <span className="nowrap">{m.elapsed_s != null ? zh.taskDetail.wholeStage(zh.time.duration(m.elapsed_s)) : '—'}</span>;
      },
    },
    {
      title: zh.taskDetail.colSummary,
      dataIndex: 'error',
      render: (_: unknown, m) =>
        !m.selected ? <span className="muted">{m.unavailable_reason ?? '—'}</span> : m.error ? <span style={{ color: 'var(--c-danger)' }}>{m.error}</span> : <span className="muted">{digest[m.id] ?? '—'}</span>,
    },
    {
      title: zh.taskDetail.colErrors,
      dataIndex: 'episodes_error',
      width: 120,
      render: (_: unknown, m) =>
        m.episodes_error ? (
          <Button type="text" size="mini" status="warning" onClick={() => setOpen((o) => (o.includes(m.id) ? o.filter((x) => x !== m.id) : [...o, m.id]))}>
            {zh.taskDetail.errorsCount(m.episodes_error)} {open.includes(m.id) ? '▴' : '▾'}
          </Button>
        ) : (
          '—'
        ),
    },
    {
      title: zh.taskDetail.colOps,
      key: 'ops',
      dataIndex: 'id',
      width: 90,
      render: (_: unknown, m) =>
        m.selected && terminal && (m.episodes_error > 0 || m.state === 'failed') ? (
          <Button size="mini" type="text" disabled={Boolean(task.active_subtask)} onClick={() => retry(m)}>
            {zh.taskDetail.retryModule}
          </Button>
        ) : null,
    },
  ];
  return (
    <Card title={zh.taskDetail.modulesTitle}>
      <Table
        rowKey="id"
        size="small"
        pagination={false}
        columns={columns}
        data={[...selected, ...skipped]}
        scroll={{ x: 980 }}
        expandedRowKeys={open}
        expandedRowRender={(m) => (m.episodes_error ? <ErrorEpisodes taskId={task.id} stage={logStageOf(plan, m.id, reg.data?.modules.find((x) => x.id === m.id)?.stage)} /> : null)}
        onExpand={(m) => setOpen((o) => (o.includes(m.id) ? o.filter((x) => x !== m.id) : [...o, m.id]))}
        expandProps={{ icon: () => null, width: 0, rowExpandable: (m) => m.episodes_error > 0 }}
        data-testid="modules-table"
      />
    </Card>
  );
}

/** An apply_adjudication subtask records how relabels were judged again (D39, `scope.relabel_rerun`). */
function relabelRerunOf(subtasks: readonly Subtask[], id: string | null | undefined): 'v1' | 'full' | null {
  const s = id ? subtasks.find((x) => x.id === id) : undefined;
  return s?.kind === 'apply_adjudication' ? s.scope.relabel_rerun ?? null : null;
}

/** A timeline node's title: subtask entries name the subtask (「子任务 · 重试 #1」「重试 #1 结束」). */
function timelineTitle(e: TimelineEntry, subtasks: readonly Subtask[]): string {
  const named = e.subtask_id && subtasks.some((s) => s.id === e.subtask_id) ? subtaskName(subtasks, e.subtask_id) : null;
  if (named && e.kind === 'subtask_started') return zh.taskDetail.subtaskStartedTitle(named);
  if (named && e.kind === 'subtask_finished') return zh.taskDetail.subtaskFinishedTitle(named);
  return zh.taskDetail.timelineKind[e.kind] ?? e.kind;
}

function timelineColor(e: TimelineEntry): string {
  if (e.kind === 'failed' || e.state === 'failed') return 'var(--c-danger)';
  if (e.kind === 'system_pause' || e.state === 'completed_with_errors') return 'var(--c-warning)';
  if (e.state === 'succeeded') return 'var(--c-success)';
  return 'var(--c-primary)';
}

/**
 * 执行时间线 (07 §4.2), laid out horizontally (requester item 21): one node per entry in a row
 * that scrolls sideways when it does not fit, opened at the newest end. Each node shows its title
 * and time, the relabel tag, the revision's report link and 当前版本 on the node itself; the entry's
 * text is clamped to two lines, the full text in a popover.
 */
function TimelineCard({ task, entries, subtasks }: { task: Task; entries: TimelineEntry[]; subtasks: readonly Subtask[] }) {
  const row = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (row.current) row.current.scrollLeft = row.current.scrollWidth;
  }, [entries.length]);
  return (
    <Card title={zh.taskDetail.timeline}>
      {!entries.length ? (
        <Empty />
      ) : (
        <div className="htl" ref={row} data-testid="timeline">
          <ol className="htl-track">
            {entries.map((e, i) => {
              const rerun = e.kind === 'subtask_started' || e.kind === 'subtask_finished' ? relabelRerunOf(subtasks, e.subtask_id) : null;
              const revision = e.kind === 'revision' && e.revision ? e.revision : null;
              return (
                <li key={i} className="htl-node" data-kind={e.kind}>
                  <div className="htl-axis" aria-hidden>
                    <span className="htl-dot" style={{ borderColor: timelineColor(e) }} />
                    <span className="htl-line" />
                  </div>
                  <div className="htl-title">{timelineTitle(e, subtasks)}</div>
                  <div className="htl-time">
                    <RelTime ms={e.at} />
                  </div>
                  {rerun || revision ? (
                    <div className="htl-extra">
                      {rerun ? (
                        <Tag size="small" color={rerun === 'full' ? 'orangered' : 'arcoblue'} data-testid="relabel-rerun-tag">
                          {zh.taskDetail.relabelRerun[rerun]}
                        </Tag>
                      ) : null}
                      {revision ? <Link to={`/tasks/${task.id}/report?rev=${revision}`}>{zh.taskDetail.openRevision(revision)}</Link> : null}
                      {revision && revision === task.result_rev ? (
                        <Tag size="small" color="green">
                          {zh.taskDetail.currentRevision}
                        </Tag>
                      ) : null}
                    </div>
                  ) : null}
                  <Popover content={<div className="htl-popover">{e.text}</div>} position="bottom">
                    <div className="htl-text" tabIndex={0}>
                      {e.text}
                    </div>
                  </Popover>
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </Card>
  );
}

function episodesText(t: Task): string {
  const e = t.episodes;
  if (e.mode === 'all') return zh.taskDetail.episodesAll;
  if (e.mode === 'head') return zh.taskDetail.episodesHead(e.n);
  return zh.taskDetail.episodesExplicit(e.expr);
}

function PlanView({ taskId, started }: { taskId: string; started: boolean }) {
  const reg = useModules();
  const plan = useQuery({
    queryKey: qk.plan(taskId),
    queryFn: () => unwrap(api().GET('/tasks/{id}/plan', { params: { path: { id: taskId } } })),
    enabled: started,
    retry: false,
  });
  if (!started || (plan.error && isApiError(plan.error, 'not_found'))) return <Typography.Text type="secondary">{zh.taskDetail.planNone}</Typography.Text>;
  if (plan.isLoading) return <Spin />;
  if (!plan.data) return <Typography.Text type="error">{errorMessage(plan.error)}</Typography.Text>;
  const p = plan.data;
  const limit = (l: { value: number; bound_by: string }) => `${l.value}（${zh.taskDetail.boundBy[l.bound_by] ?? l.bound_by}）`;
  const episodes = (ref: string | undefined) => {
    if (!ref) return '—';
    if (ref.startsWith('survivors:')) return zh.taskDetail.planSurvivors(stageLabel(ref.slice('survivors:'.length)));
    return zh.taskDetail.planEpisodes[ref] ?? ref;
  };
  return (
    <div data-testid="plan">
      <Typography.Paragraph>{zh.taskDetail.planLimits(p.vlm_parallelism, limit(p.limits.cpu_concurrency), limit(p.limits.vlm_parallelism))}</Typography.Paragraph>
      <Table
        rowKey="id"
        size="small"
        pagination={false}
        data={p.stages}
        columns={[
          { title: zh.taskDetail.planCols.stage, dataIndex: 'id', render: (v: string) => stageLabel(v) },
          { title: zh.taskDetail.planCols.modules, dataIndex: 'modules', render: (v?: string[]) => (v?.length ? v.map((m) => moduleName(reg.data, m)).join('、') : '—') },
          { title: zh.taskDetail.planCols.episodes, dataIndex: 'episodes', render: (v?: string) => episodes(v) },
          {
            title: zh.taskDetail.planCols.concurrency,
            dataIndex: 'concurrency',
            render: (_: unknown, s: Plan['stages'][number]) =>
              [s.concurrency ? `CPU ${s.concurrency}` : '', s.gates ? Object.entries(s.gates).map(([k, n]) => `${k} ${n}`).join(' · ') : '', s.hard_gates?.length ? zh.taskDetail.planHardGates(s.hard_gates.map((m) => moduleName(reg.data, m)).join('、')) : '', s.merge ? `merge = ${s.merge.strategy}` : '']
                .filter(Boolean)
                .join(' · ') || '—',
          },
        ]}
      />
      <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
        {zh.taskDetail.planEstimate(p.estimates.vlm_requests, zh.time.duration(p.estimates.wall_clock_s))}
        {p.estimates.notes.length ? `。${p.estimates.notes.join('；')}` : ''}
      </Typography.Paragraph>
    </div>
  );
}

function MoreInfo({ task }: { task: Task }) {
  const reg = useModules();
  const p = task.params ?? {};
  const selected = task.modules.filter((m) => m.selected).map((m) => moduleName(reg.data, m.id));
  const data = [
    { label: zh.taskDetail.cfgDataset, value: <span className="mono">{task.input.uri}</span> },
    { label: zh.taskDetail.cfgRegion, value: task.input.source === 'public' ? zh.taskForm.publicRegion : regionLabel(task.input.region) },
    // C4 1.2 sends null once the key was deleted (rebind-credentials gives the task another one).
    { label: zh.taskDetail.cfgKey, value: task.input.source === 'public' ? zh.taskForm.publicKey : task.input.credential ?? zh.datasets.credentialGone },
    { label: zh.taskDetail.cfgOutput, value: <span className="mono">{task.output.uri}</span> },
    { label: zh.taskDetail.cfgRunId, value: task.run_id ? <span className="mono">{task.run_id}/</span> : '—' },
    { label: zh.taskDetail.cfgOutputKey, value: task.output.credential ?? zh.datasets.credentialGone },
    { label: zh.taskDetail.cfgEpisodes, value: episodesText(task) },
    { label: zh.taskDetail.cfgSource, value: task.source ? zh.taskDetail.cfgSourceValue(task.source.objects ?? 0, bytes(task.source.bytes)) : '—' },
    { label: zh.taskDetail.cfgModules, value: selected.join('、') },
    { label: zh.taskDetail.cfgRobot, value: task.embodiment_id ?? '—' },
    { label: zh.taskDetail.cfgVlm, value: task.vlm ? `${task.vlm.backend} · ${task.vlm.model}` : '—' },
    { label: zh.taskDetail.cfgEffort, value: task.vlm ? task.vlm.reasoning_effort ?? zh.taskDetail.cfgEffortDefault : '—' },
    { label: zh.taskDetail.cfgRetry, value: task.vlm ? zh.taskDetail.cfgRetryValue(p.vlm_retry ?? 3, p.vlm_hedge !== false) : '—' },
    { label: zh.taskDetail.cfgLimits, value: `CPU ${p.limits?.cpu_concurrency ?? zh.common.unlimited} · VLM ${p.limits?.vlm_parallelism ?? zh.common.unlimited}` },
    { label: zh.taskDetail.cfgExport, value: `${p.export === false ? zh.taskDetail.noExport : zh.taskDetail.yesExport}${p.clips ? ` · ${zh.taskDetail.clipsOn}` : ''}` },
  ];
  return (
    <Collapse>
      <Collapse.Item
        name="more"
        header={<b>{zh.taskDetail.more}</b>}
      >
        <Typography.Title heading={6}>{zh.taskDetail.config}</Typography.Title>
        <Descriptions column={2} data={data} style={{ marginTop: 8 }} />
        <Typography.Title heading={6} style={{ marginTop: 16 }}>
          {zh.taskDetail.plan}
        </Typography.Title>
        <div style={{ marginTop: 8 }}>
          <PlanView taskId={task.id} started={Boolean(task.started_at)} />
        </div>
      </Collapse.Item>
    </Collapse>
  );
}

export function OverviewTab({ task, subtasks, timeline }: { task: Task; subtasks: Subtask[]; timeline: TimelineEntry[] }) {
  const plan = useQuery({
    queryKey: qk.plan(task.id),
    queryFn: () => unwrap(api().GET('/tasks/{id}/plan', { params: { path: { id: task.id } } })),
    enabled: Boolean(task.started_at),
    retry: false,
  });
  const report = useQuery({
    queryKey: qk.report(task.id, null),
    queryFn: () => unwrap(api().GET('/tasks/{id}/report', { params: { path: { id: task.id } } })),
    enabled: task.result_rev > 0,
    retry: false,
  });
  const digest: Record<string, string> = {};
  for (const m of report.data?.report.modules ?? []) digest[m.id] = summaryDigest(m.summary);
  return (
    <div className="card-gap">
      <ReportSummary task={task} />
      {task.started_at ? <PipelineEpisodesCard task={task} /> : null}
      {/* Side by side, equal height (requester item 13). */}
      <div className="grid-2">
        <StagesCard task={task} subtasks={subtasks} />
        <TokensCard task={task} subtasks={subtasks} />
      </div>
      <ModulesCard task={task} plan={plan.data} digest={digest} />
      <TimelineCard task={task} entries={timeline} subtasks={subtasks} />
      <MoreInfo task={task} />
      <div className="muted" style={{ fontSize: 12 }}>
        {absoluteTime(task.updated_at)}
      </div>
    </div>
  );
}
