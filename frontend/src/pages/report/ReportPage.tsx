import { Alert, Button, Card, Descriptions, Select, Space, Spin, Table, Tabs, Tag, Tooltip, Typography } from '@arco-design/web-react';
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { moduleById, moduleName, qk, useModules, useTask } from '../../api/queries';
import type { Report, ReportModuleSection, Subtask, Task } from '../../api/types';
import { Chart, barOption, chartSummary } from '../../components/Chart';
import { LazyVisible } from '../../components/LazyVisible';
import { PageError } from '../../components/PageError';
import { PageHeader } from '../../components/PageHeader';
import { EpisodeDrawer } from '../../features/report/EpisodeDrawer';
import { confirmModuleRetry } from '../../features/tasks/retryModule';
import { useTaskActions } from '../../features/tasks/useTaskActions';
import { compactNumber, percent } from '../../lib/format';
import { fieldLabel, integrityValue, revisionOptions } from '../../lib/reportView';
import { summaryDigest } from '../../lib/summary';
import { isTerminalState } from '../../lib/taskView';
import { zh } from '../../locales/zh';
import { DetailTables } from './DetailTables';
import { ModuleSection, SECTION_STATE_COLOR } from './ModuleSection';
import { PerfTab } from './PerfTab';

function positiveInt(v: string | null): number | null {
  if (!v || !/^\d+$/.test(v)) return null;
  return Number(v);
}

function Stat({ label, value, foot, testId }: { label: string; value: string | number; foot?: string; testId?: string }) {
  return (
    <div className="stat-cell" data-testid={testId}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {foot ? <div className="stat-foot">{foot}</div> : null}
    </div>
  );
}

/** Why a new subtask cannot start now (one active subtask per task, and only once it has stopped running). */
function retryBlocked(task: Task): string | null {
  if (task.active_subtask || !isTerminalState(task.state)) return zh.report.retryBusy;
  return null;
}

function OverviewCard({ task, report, readOnly, onRetryHeld }: { task: Task; report: Report; readOnly: boolean; onRetryHeld: () => void }) {
  const reg = useModules();
  const o = report.overview;
  const c = o.counts;
  const t = o.token_usage;
  const reasons = o.reject_reasons.map((r) => ({ name: moduleName(reg.data, r.module), value: r.count }));
  const blocked = readOnly ? zh.report.historyDisabled : retryBlocked(task);
  const skipped = skippedOf(report).count;
  return (
    <Card title={zh.report.overview} extra={<span className="muted">{zh.report.overviewDesc}</span>}>
      <div className="equation" data-testid="report-equation">
        <Stat label={zh.report.input} value={c.total} testId="eq-total" />
        <span className="op">=</span>
        <Stat label={zh.report.rejected} value={c.rejected} testId="eq-rejected" />
        <span className="op">+</span>
        <Stat label={zh.report.delivered} value={c.passed} foot={c.review ? zh.report.deliveredFoot(c.review) : undefined} testId="eq-passed" />
        <span className="op">+</span>
        <Stat label={zh.report.held} value={c.held} foot={zh.report.heldFoot} testId="eq-held" />
      </div>
      <div className="stat-grid" style={{ marginTop: 12 }}>
        <Stat label={zh.report.passRate} value={percent(o.pass_rate, 0)} />
        <Stat label={zh.report.duration} value={zh.time.duration(o.duration_s)} />
        <Stat label={zh.report.tokens} value={compactNumber(t.prompt + t.completion)} foot={zh.report.tokensFoot(compactNumber(t.prompt), compactNumber(t.completion))} />
      </div>
      {skipped ? (
        // D40: not part of the equation above, not in any list, and no retry - the source is frozen.
        <Alert type="info" style={{ marginTop: 12 }} data-testid="skipped-note" title={zh.report.skippedNote(skipped)} content={zh.report.skippedNoteDesc} />
      ) : null}
      {c.held ? (
        <Alert
          type="warning"
          style={{ marginTop: 12 }}
          title={zh.report.heldTitle(c.held)}
          content={zh.report.heldDesc}
          action={
            blocked ? (
              <Tooltip content={blocked}>
                <Button size="small" disabled>
                  {zh.report.retryHeld(c.held)}
                </Button>
              </Tooltip>
            ) : (
              <Button size="small" type="primary" onClick={onRetryHeld}>
                {zh.report.retryHeld(c.held)}
              </Button>
            )
          }
        />
      ) : null}
      <Typography.Title heading={6} style={{ margin: '16px 0 4px' }}>
        {zh.report.rejectReasons}
        <span className="muted" style={{ fontWeight: 'normal', fontSize: 12, marginLeft: 8 }}>
          {zh.report.rejectReasonsDesc(c.rejected)}
        </span>
      </Typography.Title>
      {reasons.length ? (
        <LazyVisible>
          <Chart option={barOption(reasons, { horizontal: true })} summary={`${zh.report.rejectReasons}：${chartSummary(reasons)}`} height={Math.max(120, reasons.length * 36)} />
        </LazyVisible>
      ) : (
        <Typography.Text type="secondary">{zh.report.rejectNone}</Typography.Text>
      )}
    </Card>
  );
}

type SkippedEpisode = NonNullable<Report['integrity']['skipped_episodes']>[number];

/** Episodes left out because source files are missing (D40): the count, and the list when the report has it. */
export function skippedOf(report: Report): { count: number; list: SkippedEpisode[] } {
  const list = report.integrity?.skipped_episodes ?? [];
  return { count: report.overview.counts.skipped ?? list.length, list };
}

function IntegrityCard({ report }: { report: Report }) {
  const entries = Object.entries(report.integrity ?? {}).filter(([k]) => k !== 'skipped_episodes');
  const skipped = skippedOf(report);
  return (
    <Card title={zh.report.integrity} extra={<span className="muted">{zh.report.integrityDesc}</span>}>
      {entries.length ? (
        <div data-testid="integrity">
          <Descriptions column={1} data={entries.map(([k, v]) => ({ label: fieldLabel(k), value: integrityValue(k, v) }))} />
        </div>
      ) : (
        <Typography.Text type="secondary">—</Typography.Text>
      )}
      {skipped.list.length ? (
        <div style={{ marginTop: 16 }} data-testid="skipped-episodes">
          <Typography.Title heading={6} style={{ margin: '0 0 4px' }}>
            {zh.report.skippedTitle(skipped.list.length)}
          </Typography.Title>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
            {zh.report.skippedNoteDesc}
          </Typography.Paragraph>
          <Table
            rowKey="episode_index"
            size="small"
            pagination={false}
            data={skipped.list}
            columns={[
              { title: zh.report.columns.episode_index, dataIndex: 'episode_index', width: 110, render: (ep: number) => <span className="mono">{zh.report.episode(ep)}</span> },
              {
                title: zh.report.skippedMissing,
                dataIndex: 'missing',
                render: (files: string[]) => (
                  <div className="mono">
                    {files.map((f) => (
                      <div key={f}>{f}</div>
                    ))}
                  </div>
                ),
              },
            ]}
          />
        </div>
      ) : null}
    </Card>
  );
}

interface ScopeRow {
  key: string;
  order: number | null;
  id: string;
  gate: string;
  state: string;
  note: string;
  failed: boolean;
}

function ScopeCard({ report }: { report: Report }) {
  const reg = useModules();
  const rows: ScopeRow[] = [
    ...report.modules.map((s, i) => ({
      key: s.id,
      order: i + 1,
      id: s.id,
      gate: s.gate,
      state: s.state,
      note: s.state === 'failed' ? s.error ?? '' : summaryDigest(s.summary as Record<string, unknown>),
      failed: s.state === 'failed',
    })),
    ...report.skipped_modules.map((s) => ({ key: `skip-${s.id}`, order: null, id: s.id, gate: moduleById(reg.data, s.id)?.gate ?? '', state: 'skipped', note: s.reason, failed: false })),
  ];
  const jump = (id: string) => document.getElementById(`module-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  return (
    <Card title={zh.report.scope} extra={<span className="muted">{zh.report.scopeDesc}</span>}>
      <Table
        rowKey="key"
        size="small"
        pagination={false}
        data={rows}
        data-testid="report-scope"
        columns={[
          { title: zh.report.colOrder, dataIndex: 'order', width: 64, render: (v: number | null) => v ?? '—' },
          {
            title: zh.report.colModule,
            dataIndex: 'id',
            render: (_: unknown, r: ScopeRow) =>
              r.order ? (
                <Button type="text" size="mini" style={{ padding: 0 }} onClick={() => jump(r.id)}>
                  {moduleName(reg.data, r.id)}
                </Button>
              ) : (
                <span className="muted">{moduleName(reg.data, r.id)}</span>
              ),
          },
          {
            title: zh.report.colGate,
            dataIndex: 'gate',
            render: (_: unknown, r: ScopeRow) => (
              <Space size={4}>
                {zh.gate[r.gate] ?? r.gate}
                {((moduleById(reg.data, r.id)?.needs ?? []) as string[]).includes('vlm') ? (
                  <Tag size="small" color="purple">
                    {zh.report.usesVlm}
                  </Tag>
                ) : null}
              </Space>
            ),
          },
          { title: zh.report.colState, dataIndex: 'state', render: (v: string) => <Tag color={SECTION_STATE_COLOR[v] ?? 'gray'}>{zh.moduleState[v] ?? v}</Tag> },
          { title: zh.report.colNote, dataIndex: 'note', render: (v: string, r: ScopeRow) => <span style={{ color: r.failed ? 'var(--c-danger)' : undefined }}>{v || '—'}</span> },
        ]}
      />
    </Card>
  );
}

function ReportBody({
  task,
  report,
  rev,
  readOnly,
  subtasks,
  table,
  setTable,
}: {
  task: Task;
  report: Report;
  rev: number;
  readOnly: boolean;
  subtasks: readonly Subtask[];
  table: string | null;
  setTable: (table: string) => void;
}) {
  const qc = useQueryClient();
  const actions = useTaskActions();
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: qk.task(task.id) });
  };
  const openTable = (id: string) => {
    setTable(id);
    document.getElementById('detail-tables')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  const blocked = retryBlocked(task);
  return (
    <div className="card-gap">
      <OverviewCard task={task} report={report} readOnly={readOnly} onRetryHeld={() => actions.run('retry', { id: task.id, name: task.name, held: report.overview.counts.held })} />
      <IntegrityCard report={report} />
      <ScopeCard report={report} />
      {report.modules.map((s: ReportModuleSection, i: number) => (
        <ModuleSection
          key={s.id}
          index={i + 1}
          taskId={task.id}
          rev={rev}
          section={s}
          subtasks={subtasks}
          readOnly={readOnly}
          retryBlocked={blocked}
          onRetry={(moduleId, name) => confirmModuleRetry({ taskId: task.id, moduleId, name, onDone: refresh })}
          onTable={openTable}
        />
      ))}
      <LazyVisible placeholder={<Card title={zh.report.tables} id="detail-tables" />}>
        <DetailTables
          taskId={task.id}
          rev={rev}
          sections={report.modules}
          selected={table}
          onSelect={setTable}
          onResultChanged={() => {
            void qc.invalidateQueries({ queryKey: qk.task(task.id) });
            void qc.invalidateQueries({ queryKey: ['task', task.id, 'report'] });
          }}
        />
      </LazyVisible>
      {actions.dialogs}
    </div>
  );
}

/**
 * 质检报告 (07 §5): overview and integrity, one section per selected module in report.json order,
 * detail tables with server paging, the episode drawer (?ep=), history revisions (?rev=, read
 * only) and the performance profile (#perf).
 */
export function ReportPage() {
  const { id = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const task = useTask(id, (q) => {
    const t = q.state.data;
    return t && (t.active_subtask || !isTerminalState(t.state)) ? 5000 : false;
  });
  const t = task.data;
  const current = t?.result_rev ?? 0;
  const revParam = positiveInt(params.get('rev'));
  const rev = revParam ?? current;
  const readOnly = revParam !== null && revParam !== current;
  const ep = positiveInt(params.get('ep'));
  const tab = location.hash === '#perf' ? 'perf' : 'report';
  // The detail table picked at the bottom survives a revision change (the page stays mounted).
  const [table, setTable] = useState<string | null>(null);
  const report = useQuery({
    queryKey: qk.report(id, rev),
    queryFn: () => unwrap(api().GET('/tasks/{id}/report', { params: { path: { id }, query: { rev } } })),
    enabled: Boolean(t) && rev > 0,
    placeholderData: keepPreviousData,
    retry: false,
  });
  const timeline = useQuery({ queryKey: qk.timeline(id), queryFn: () => unwrap(api().GET('/tasks/{id}/timeline', { params: { path: { id } } })), enabled: current > 0 });
  const subtasks = useQuery({ queryKey: qk.subtasks(id), queryFn: () => unwrap(api().GET('/tasks/{id}/subtasks', { params: { path: { id } } })), enabled: current > 0 });
  const subs = subtasks.data?.items ?? [];

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value === null) next.delete(key);
    else next.set(key, value);
    setParams(next);
  };

  const crumbs = [{ label: zh.taskList.title, to: '/tasks' }, { label: t?.name ?? id, to: `/tasks/${id}` }, { label: zh.report.title }];
  if (task.isError && !t) {
    return (
      <>
        <PageHeader crumbs={crumbs} title={zh.report.title} />
        <PageError error={task.error} onRetry={() => void task.refetch()} />
      </>
    );
  }
  if (!t) return <Spin style={{ display: 'block', margin: '80px auto' }} />;

  const options = revisionOptions(timeline.data?.items ?? [], current, subs);
  const header = (
    <PageHeader
      crumbs={crumbs}
      title={zh.report.title}
      docTitle={`${zh.report.title} · ${t.name}`}
      description={
        current ? (
          <Space wrap size={8}>
            <span>{t.name}</span>
            <span>·</span>
            <span>{zh.report.revision}</span>
            <Select
              size="small"
              style={{ width: 280 }}
              value={rev}
              aria-label={zh.report.revision}
              onChange={(v: number) => setParam('rev', v === current ? null : String(v))}
              options={options}
            />
            {report.data ? <span className="muted">· {zh.report.sectionsDesc(report.data.report.modules.length)}</span> : null}
          </Space>
        ) : (
          t.name
        )
      }
      extra={
        <>
          <Link to={`/tasks/${t.id}`}>
            <Button>{zh.report.back}</Button>
          </Link>
          {t.pending_adjudication && !readOnly ? (
            <Link to={`/tasks/${t.id}/adjudication`}>
              <Button type="primary">{zh.report.goAdjudicate(t.pending_adjudication)}</Button>
            </Link>
          ) : null}
        </>
      }
    />
  );

  if (!current) {
    return (
      <div>
        {header}
        <Card>
          <Typography.Text type="secondary">{zh.report.noReport}</Typography.Text>
        </Card>
      </div>
    );
  }

  return (
    <div>
      {header}
      {readOnly ? (
        <Alert
          type="warning"
          style={{ marginBottom: 16 }}
          data-testid="history-banner"
          content={zh.report.history(zh.report.revLabel(rev), zh.report.revLabel(current))}
          action={
            <Button size="small" onClick={() => setParam('rev', null)}>
              {zh.report.historyBack}
            </Button>
          }
        />
      ) : null}
      <Tabs activeTab={tab} onChange={(k) => navigate({ search: location.search, hash: k === 'perf' ? '#perf' : '' })}>
        <Tabs.TabPane key="report" title={zh.report.tabReport}>
          {report.isLoading ? (
            <Spin style={{ display: 'block', margin: '48px auto' }} />
          ) : !report.data ? (
            <PageError error={report.error} onRetry={() => void report.refetch()} />
          ) : (
            <ReportBody task={t} report={report.data.report} rev={report.data.revision} readOnly={readOnly} subtasks={subs} table={table} setTable={setTable} />
          )}
        </Tabs.TabPane>
        <Tabs.TabPane key="perf" title={zh.report.tabPerf}>
          {tab === 'perf' ? <PerfTab taskId={t.id} rev={rev} subtasks={subs} /> : null}
        </Tabs.TabPane>
      </Tabs>
      <EpisodeDrawer taskId={t.id} ep={ep} rev={rev} readOnly={readOnly} onClose={() => setParam('ep', null)} />
    </div>
  );
}
