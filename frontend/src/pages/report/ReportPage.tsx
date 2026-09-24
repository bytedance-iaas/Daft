import { Alert, Button, Card, Select, Space, Spin, Table, Tabs, Tag, Tooltip, Typography } from '@arco-design/web-react';
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
import { StatCell } from '../../components/StatCell';
import { confirmModuleRetry } from '../../features/tasks/retryModule';
import { useTaskActions } from '../../features/tasks/useTaskActions';
import { compactNumber, percent } from '../../lib/format';
import { integrityItems } from '../../lib/integrity';
import { revisionOptions, runParts, type RunPart } from '../../lib/reportView';
import { readCollapsedSections, writeCollapsedSections } from '../../lib/prefs';
import { summaryDigest } from '../../lib/summary';
import { isTerminalState } from '../../lib/taskView';
import { zh } from '../../locales/zh';
import { EpisodesTab } from './EpisodesTab';
import { ModuleSection, SECTION_STATE_COLOR } from './ModuleSection';
import { PerfTab } from './PerfTab';

function positiveInt(v: string | null): number | null {
  if (!v || !/^\d+$/.test(v)) return null;
  return Number(v);
}

const Stat = StatCell;

/** An episode number that opens the Episode tab on it (?ep=N#episodes), keeping ?rev. */
export function EpisodeLink({ ep }: { ep: number }) {
  const [params] = useSearchParams();
  const next = new URLSearchParams(params);
  next.set('ep', String(ep));
  return (
    <Link to={{ search: `?${next.toString()}`, hash: '#episodes' }} className="mono">
      {zh.report.episode(ep)}
    </Link>
  );
}

/** The episodes waiting for a retry, as links (a few; «ep 7、ep 31»). */
function HeldEpisodes({ taskId, rev }: { taskId: string; rev: number }) {
  const q = useQuery({
    queryKey: qk.episodes(taskId, rev, { list: 'held', limit: 20 }),
    queryFn: () => unwrap(api().GET('/tasks/{id}/episodes', { params: { path: { id: taskId }, query: { rev, list: 'held', limit: 20 } } })),
    retry: false,
  });
  const items = q.data?.items ?? [];
  if (!items.length) return null;
  return (
    <div data-testid="held-episodes">
      {zh.reportPage.heldList}：
      {items.map((e, i) => (
        <span key={e.episode_index}>
          {i ? '、' : ''}
          <EpisodeLink ep={e.episode_index} />
        </span>
      ))}
      {q.data?.has_more ? '…' : ''}
    </div>
  );
}

/**
 * The standing entry to adjudication (D47): whenever the task has a result, with the number of
 * pending cards (and primary) when there are any; history revisions are read only.
 */
function AdjudicateButton({ task, readOnly }: { task: Task; readOnly: boolean }) {
  const n = task.pending_adjudication;
  const label = n ? zh.reportPage.adjudicateN(n) : zh.reportPage.adjudicate;
  if (readOnly) {
    return (
      <Tooltip content={zh.report.historyDisabled}>
        <Button disabled data-testid="adjudicate-entry">
          {label}
        </Button>
      </Tooltip>
    );
  }
  return (
    <Tooltip content={zh.reportPage.adjudicateHint}>
      <Link to={`/tasks/${task.id}/adjudication`} data-testid="adjudicate-entry">
        <Button type={n ? 'primary' : 'default'}>{label}</Button>
      </Link>
    </Tooltip>
  );
}

/** Why a new subtask cannot start now (one active subtask per task, and only once it has stopped running). */
function retryBlocked(task: Task): string | null {
  if (task.active_subtask || !isTerminalState(task.state)) return zh.report.retryBusy;
  return null;
}

function OverviewCard({ task, report, readOnly, runs, onRetryHeld }: { task: Task; report: Report; readOnly: boolean; runs: RunPart[]; onRetryHeld: () => void }) {
  const reg = useModules();
  const o = report.overview;
  const c = o.counts;
  const t = o.token_usage;
  const reasons = o.reject_reasons.map((r) => ({ name: moduleName(reg.data, r.module), value: r.count }));
  const blocked = readOnly ? zh.report.historyDisabled : retryBlocked(task);
  const skipped = skippedOf(report).count;
  return (
    <Card title={zh.report.overview}>
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
        {o.duration_s !== null && o.duration_s !== undefined ? (
          <Stat label={zh.report.duration} value={zh.time.duration(o.duration_s)} />
        ) : runs.length ? (
          <Stat label={zh.report.duration} value={zh.reportPage.durationParts(runs.map((r) => zh.time.duration(r.seconds)))} foot={zh.reportPage.durationParts(runs.map((r) => r.label))} testId="report-duration" />
        ) : null}
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
          content={
            <>
              <div>{zh.report.heldDesc}</div>
              <HeldEpisodes taskId={task.id} rev={report.revision} />
            </>
          }
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

function IntegrityCard({ task, report }: { task: Task; report: Report }) {
  const items = integrityItems(report, task);
  const skipped = skippedOf(report);
  return (
    <Card title={zh.report.integrity}>
      <dl className="desc-grid" data-testid="integrity">
        {items.map((it) => (
          <div key={it.key} className={`desc-item${it.full ? ' full' : ''}${it.warn ? ' warn' : ''}`}>
            <dt>{it.label}</dt>
            <dd>{it.value}</dd>
          </div>
        ))}
      </dl>
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

function ScopeCard({ report, onJump, onCollapseAll, onExpandAll }: { report: Report; onJump: (id: string) => void; onCollapseAll: () => void; onExpandAll: () => void }) {
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
  return (
    <Card
      title={zh.report.scope}
      extra={
        <Space size={8}>
          <Button size="small" onClick={onCollapseAll}>
            {zh.reportPage.collapseAll}
          </Button>
          <Button size="small" onClick={onExpandAll}>
            {zh.reportPage.expandAll}
          </Button>
        </Space>
      }
    >
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
                <Button type="text" size="mini" style={{ padding: 0 }} onClick={() => onJump(r.id)}>
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
  runs,
}: {
  task: Task;
  report: Report;
  rev: number;
  readOnly: boolean;
  subtasks: readonly Subtask[];
  runs: RunPart[];
}) {
  const qc = useQueryClient();
  const actions = useTaskActions();
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: qk.task(task.id) });
  };
  const blocked = retryBlocked(task);
  // Folded sections are a per-browser preference (F6.2): the same modules stay folded on every report.
  const [collapsed, setCollapsed] = useState<Set<string>>(() => readCollapsedSections());
  const update = (next: Set<string>) => {
    setCollapsed(next);
    writeCollapsedSections(next);
  };
  const toggle = (id: string) => {
    const next = new Set(collapsed);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    update(next);
  };
  const jump = (id: string) => {
    if (collapsed.has(id)) {
      const next = new Set(collapsed);
      next.delete(id);
      update(next);
    }
    requestAnimationFrame(() => document.getElementById(`module-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  };
  const ids = report.modules.map((m) => m.id);
  return (
    <div className="card-gap">
      <OverviewCard task={task} report={report} readOnly={readOnly} runs={runs} onRetryHeld={() => actions.run('retry', { id: task.id, name: task.name, held: report.overview.counts.held })} />
      <IntegrityCard task={task} report={report} />
      <ScopeCard report={report} onJump={jump} onCollapseAll={() => update(new Set([...collapsed, ...ids]))} onExpandAll={() => update(new Set([...collapsed].filter((id) => !ids.includes(id))))} />
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
          collapsed={collapsed.has(s.id)}
          onToggle={() => toggle(s.id)}
          onRetry={(moduleId, name) => confirmModuleRetry({ taskId: task.id, moduleId, name, qc, onDone: refresh })}
        />
      ))}
      {actions.dialogs}
    </div>
  );
}

/**
 * 质检报告 (07 §5, F6.2): three tabs - 报告 (overview, integrity, one section of statistics and
 * charts per selected module in report.json order), Episode 明细 (#episodes, `?ep=N` picks the
 * episode) and 性能剖析 (#perf); history revisions (?rev=) are read only in all of them.
 */
export function ReportPage() {
  const { id = '' } = useParams();
  const [params] = useSearchParams();
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
  const hashTab = location.hash === '#perf' ? 'perf' : location.hash === '#episodes' ? 'episodes' : location.hash === '#report' ? 'report' : null;
  const tab = hashTab ?? (ep !== null ? 'episodes' : 'report');
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

  // Changing ?rev / ?ep keeps the tab (the hash); an episode picked anywhere opens the Episode tab.
  const setParam = (key: string, value: string | null, hash: string = location.hash) => {
    const next = new URLSearchParams(params);
    if (value === null) next.delete(key);
    else next.set(key, value);
    const search = next.toString();
    navigate({ search: search ? `?${search}` : '', hash });
  };
  const selectEpisode = (n: number) => setParam('ep', String(n), '#episodes');
  const onTab = (k: string) => navigate({ search: location.search, hash: k === 'report' ? (ep !== null ? '#report' : '') : `#${k}` });

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
          {current ? <AdjudicateButton task={t} readOnly={readOnly} /> : null}
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
      <Tabs activeTab={tab} onChange={onTab}>
        <Tabs.TabPane key="report" title={zh.report.tabReport}>
          {report.isLoading ? (
            <Spin style={{ display: 'block', margin: '48px auto' }} />
          ) : !report.data ? (
            <PageError error={report.error} onRetry={() => void report.refetch()} />
          ) : (
            <ReportBody task={t} report={report.data.report} rev={report.data.revision} readOnly={readOnly} subtasks={subs} runs={runParts(t, timeline.data?.items ?? [], subs, report.data.revision)} />
          )}
        </Tabs.TabPane>
        <Tabs.TabPane key="episodes" title={zh.reportPage.tabEpisodes}>
          {tab === 'episodes' ? <EpisodesTab taskId={t.id} rev={rev} readOnly={readOnly} report={report.data?.report} ep={ep} onSelect={selectEpisode} /> : null}
        </Tabs.TabPane>
        <Tabs.TabPane key="perf" title={zh.report.tabPerf}>
          {tab === 'perf' ? <PerfTab taskId={t.id} rev={rev} subtasks={subs} /> : null}
        </Tabs.TabPane>
      </Tabs>
    </div>
  );
}
