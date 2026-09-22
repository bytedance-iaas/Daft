import { Button, Card, Collapse, Descriptions, Empty, Progress, Space, Spin, Table, Tabs, Tag, Timeline, Typography } from '@arco-design/web-react';
import type { ColumnProps } from '@arco-design/web-react/es/Table';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
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
import { isTerminalState, stageLabel, stagePercent } from '../../lib/taskView';
import { zh } from '../../locales/zh';

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
      extra={
        s ? (
          <Space>
            {task.pending_adjudication ? (
              <Link to={`/tasks/${task.id}/adjudication`}>
                <Button size="small">{zh.taskDetail.goAdjudicate(task.pending_adjudication)}</Button>
              </Link>
            ) : null}
            <Link to={`/tasks/${task.id}/report`}>
              <Button size="small" type="primary">
                {zh.taskDetail.openReport}
              </Button>
            </Link>
          </Space>
        ) : null
      }
    >
      {s ? (
        <>
          <div className="stat-grid" data-testid="report-summary">
            <Stat label={zh.taskDetail.total} value={s.total} />
            <Stat label={zh.taskDetail.passed} value={s.passed} foot={s.review ? zh.taskDetail.passedFoot(s.review) : undefined} />
            <Stat label={zh.taskDetail.rejected} value={s.rejected} />
            <Stat label={zh.taskDetail.held} value={s.held} foot={zh.taskDetail.heldFoot} />
            <Stat label={zh.taskDetail.review} value={task.pending_adjudication} />
            <Stat label={zh.taskDetail.passRate} value={percent(s.pass_rate)} foot={zh.taskDetail.passRateFoot} />
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

function StagesCard({ task }: { task: Task }) {
  const stages = task.progress.stages;
  return (
    <Card title={zh.taskDetail.stages}>
      {!stages.length ? (
        <Typography.Text type="secondary">{zh.taskDetail.noStages}</Typography.Text>
      ) : (
        <div data-testid="stages">
          {stages.map((s) => {
            const status = s.state === 'failed' ? 'error' : s.state === 'completed_with_errors' ? 'warning' : s.state === 'succeeded' ? 'success' : 'normal';
            return (
              <div key={s.id} style={{ marginBottom: 12 }} data-testid={`stage-${s.id}`}>
                <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                  <span>
                    <b>{stageLabel(s.id)}</b> <span className="muted">{zh.stageState[s.state] ?? s.state}</span>
                  </span>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {s.total ? `${s.done} / ${s.total}` : ''}
                    {s.elapsed_s !== null && s.elapsed_s !== undefined ? ` · ${zh.taskDetail.stageTime(zh.time.duration(s.elapsed_s))}` : ''}
                    {s.state === 'running' && s.eta_s ? ` · ${zh.taskDetail.stageEta(zh.time.duration(s.eta_s))}` : ''}
                  </span>
                </Space>
                <Progress percent={s.state === 'skipped' ? 0 : stagePercent(s)} status={status} showText={false} size="small" />
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

function sumRows(rows: UsageRow[], key: (r: UsageRow) => string) {
  const out = new Map<string, { key: string; prompt: number; completion: number; reasoning: number; cached: number; requests: number; unknown: number }>();
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

function TokensCard({ task, subtasks }: { task: Task; subtasks: Subtask[] }) {
  const reg = useModules();
  const usage = useQuery({ queryKey: qk.usage(task.id), queryFn: () => unwrap(api().GET('/tasks/{id}/usage', { params: { path: { id: task.id } } })) });
  const u = task.usage;
  const subtaskLabel = (id: string) => subtaskName(subtasks, id);
  const moduleLabel = (id: string) => (id.includes('+') ? `合并请求（${id.split('+').map((m) => moduleName(reg.data, m)).join('、')}）` : moduleName(reg.data, id));
  const cols = (label: string) => [
    { title: label, dataIndex: 'key' },
    { title: zh.taskDetail.tokenInput, dataIndex: 'prompt', render: (v: number) => compactNumber(v) },
    { title: zh.taskDetail.tokenOutput, dataIndex: 'completion', render: (v: number) => compactNumber(v) },
    { title: zh.taskDetail.tokenReasoning, dataIndex: 'reasoning', render: (v: number) => compactNumber(v) },
    { title: zh.taskDetail.tokenCached, dataIndex: 'cached', render: (v: number) => compactNumber(v) },
    { title: zh.taskDetail.tokenRequests, dataIndex: 'requests', render: (v: number) => v.toLocaleString('en-US') },
  ];
  const rows = usage.data?.actual ?? [];
  return (
    <Card title={zh.taskDetail.tokens} extra={<span className="muted">{zh.taskDetail.tokensDesc}</span>}>
      <div className="stat-grid" data-testid="token-totals">
        <Stat label={zh.taskDetail.tokenInput} value={compactNumber(u.prompt_tokens)} foot={zh.taskDetail.tokenInputFoot} />
        <Stat label={zh.taskDetail.tokenOutput} value={compactNumber(u.completion_tokens)} foot={zh.taskDetail.tokenOutputFoot} />
        <Stat label={zh.taskDetail.tokenReasoning} value={compactNumber(u.reasoning_tokens)} foot={u.completion_tokens ? zh.taskDetail.tokenShare(`输出 ${percent(u.reasoning_tokens / u.completion_tokens, 0)}`) : undefined} />
        <Stat label={zh.taskDetail.tokenCached} value={compactNumber(u.cached_tokens)} foot={u.prompt_tokens ? zh.taskDetail.tokenShare(`输入 ${percent(u.cached_tokens / u.prompt_tokens, 0)}`) : undefined} />
        <Stat label={zh.taskDetail.tokenRequests} value={u.requests.toLocaleString('en-US')} foot={u.requests_unknown_usage ? zh.taskDetail.tokenUnknown(u.requests_unknown_usage) : undefined} />
      </div>
      {rows.length ? (
        <Tabs defaultActiveTab="module" size="small" style={{ marginTop: 12 }}>
          <Tabs.TabPane key="module" title={zh.taskDetail.tokenByModule}>
            <Table rowKey="key" size="small" pagination={false} columns={cols(zh.taskDetail.colModule)} data={sumRows(rows, (r) => moduleLabel(r.module_id))} />
          </Tabs.TabPane>
          <Tabs.TabPane key="subtask" title={zh.taskDetail.tokenBySubtask}>
            <Table rowKey="key" size="small" pagination={false} columns={cols(zh.taskDetail.tokenBySubtask)} data={sumRows(rows, (r) => subtaskLabel(r.subtask_id))} />
          </Tabs.TabPane>
        </Tabs>
      ) : null}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
        {zh.taskDetail.tokenNote}
      </Typography.Paragraph>
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
    confirmModuleRetry({ taskId: task.id, moduleId: m.id, name: moduleName(reg.data, m.id) || m.name, onDone: () => void qc.invalidateQueries({ queryKey: ['task', task.id] }) });
  const columns: ColumnProps<ModuleState>[] = [
    { title: zh.taskDetail.colModule, dataIndex: 'name', render: (_: unknown, m) => <b>{moduleName(reg.data, m.id) || m.name}</b> },
    { title: zh.taskDetail.colStage, dataIndex: 'id', render: (_: unknown, m) => stageLabel(reg.data?.modules.find((x) => x.id === m.id)?.stage ?? '') },
    {
      title: zh.taskDetail.colState,
      dataIndex: 'state',
      render: (_: unknown, m) => (
        <span>
          <span className="dot" style={{ background: MODULE_STATE_COLOR[m.selected ? m.state : 'skipped'] }} />
          {m.selected ? zh.moduleState[m.state] ?? m.state : zh.moduleState.skipped}
        </span>
      ),
    },
    { title: zh.taskDetail.colTotal, dataIndex: 'episodes_total', render: (_: unknown, m) => (m.selected ? m.episodes_total : '—') },
    { title: zh.taskDetail.colElapsed, dataIndex: 'elapsed_s', render: (_: unknown, m) => (m.elapsed_s !== null && m.elapsed_s !== undefined ? zh.time.duration(m.elapsed_s) : '—') },
    {
      title: zh.taskDetail.colSummary,
      dataIndex: 'error',
      render: (_: unknown, m) =>
        !m.selected ? <span className="muted">{m.unavailable_reason ?? '—'}</span> : m.error ? <span style={{ color: 'var(--c-danger)' }}>{m.error}</span> : <span className="muted">{digest[m.id] ?? '—'}</span>,
    },
    {
      title: zh.taskDetail.colErrors,
      dataIndex: 'episodes_error',
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
      dataIndex: 'id',
      render: (_: unknown, m) =>
        m.selected && terminal && (m.episodes_error > 0 || m.state === 'failed') ? (
          <Button size="mini" type="text" disabled={Boolean(task.active_subtask)} onClick={() => retry(m)}>
            {zh.taskDetail.retryModule}
          </Button>
        ) : null,
    },
  ];
  return (
    <Card title={zh.taskDetail.modulesTitle} extra={<span className="muted">{zh.taskDetail.modulesDesc(selected.length, skipped.map((m) => m.name).join('、'))}</span>}>
      <Table
        rowKey="id"
        size="small"
        pagination={false}
        columns={columns}
        data={[...selected, ...skipped]}
        expandedRowKeys={open}
        expandedRowRender={(m) => (m.episodes_error ? <ErrorEpisodes taskId={task.id} stage={logStageOf(plan, m.id, reg.data?.modules.find((x) => x.id === m.id)?.stage)} /> : null)}
        onExpand={(m) => setOpen((o) => (o.includes(m.id) ? o.filter((x) => x !== m.id) : [...o, m.id]))}
        expandProps={{ icon: () => null, width: 0, rowExpandable: (m) => m.episodes_error > 0 }}
        data-testid="modules-table"
      />
    </Card>
  );
}

function TimelineCard({ task, entries }: { task: Task; entries: TimelineEntry[] }) {
  return (
    <Card title={zh.taskDetail.timeline} extra={<span className="muted">{zh.taskDetail.timelineDesc}</span>}>
      {!entries.length ? (
        <Empty />
      ) : (
        <Timeline data-testid="timeline">
          {entries.map((e, i) => (
            <Timeline.Item key={i} label={<RelTime ms={e.at} />} dotColor={e.kind === 'failed' ? 'var(--c-danger)' : e.kind === 'system_pause' ? 'var(--c-warning)' : undefined}>
              <b>{zh.taskDetail.timelineKind[e.kind] ?? e.kind}</b>
              {e.revision && e.kind === 'revision' ? (
                <Space style={{ marginLeft: 8 }}>
                  <Link to={`/tasks/${task.id}/report?rev=${e.revision}`}>{zh.taskDetail.openRevision(e.revision)}</Link>
                  {e.revision === task.result_rev ? <Tag size="small" color="green">{zh.taskDetail.currentRevision}</Tag> : null}
                </Space>
              ) : null}
              <div className="muted" style={{ fontSize: 12 }}>
                {e.text}
              </div>
            </Timeline.Item>
          ))}
        </Timeline>
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
              [s.concurrency ? `CPU ${s.concurrency}` : '', s.gates ? Object.entries(s.gates).map(([k, n]) => `${k} ${n}`).join(' · ') : '', s.hard_gates?.length ? `硬门：${s.hard_gates.map((m) => moduleName(reg.data, m)).join('、')}` : '', s.merge ? `merge = ${s.merge.strategy}` : '']
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
    { label: zh.taskDetail.cfgRetry, value: task.vlm ? `${p.vlm_retry ?? 3} 次 · 超时对冲${p.vlm_hedge === false ? '关' : '开'}` : '—' },
    { label: zh.taskDetail.cfgLimits, value: `CPU ${p.limits?.cpu_concurrency ?? zh.common.unlimited} · VLM ${p.limits?.vlm_parallelism ?? zh.common.unlimited}` },
    { label: zh.taskDetail.cfgExport, value: `${p.export === false ? zh.taskDetail.noExport : zh.taskDetail.yesExport}${p.clips ? ` · ${zh.taskDetail.clipsOn}` : ''}` },
  ];
  return (
    <Collapse>
      <Collapse.Item
        name="more"
        header={
          <Space>
            <b>{zh.taskDetail.more}</b>
            <span className="muted" style={{ fontSize: 12 }}>
              {zh.taskDetail.moreDesc}
            </span>
          </Space>
        }
      >
        <Typography.Title heading={6}>{zh.taskDetail.config}</Typography.Title>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {zh.taskDetail.configDesc}
        </Typography.Text>
        <Descriptions column={2} data={data} style={{ marginTop: 8 }} />
        <Typography.Title heading={6} style={{ marginTop: 16 }}>
          {zh.taskDetail.plan}
        </Typography.Title>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {zh.taskDetail.planDesc}
        </Typography.Text>
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
      <StagesCard task={task} />
      <TokensCard task={task} subtasks={subtasks} />
      <ModulesCard task={task} plan={plan.data} digest={digest} />
      <TimelineCard task={task} entries={timeline} />
      <MoreInfo task={task} />
      <div className="muted" style={{ fontSize: 12 }}>
        {absoluteTime(task.updated_at)}
      </div>
    </div>
  );
}
