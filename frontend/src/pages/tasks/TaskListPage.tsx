import { Button, Card, Progress, Select, Space, Table, Tag, Tooltip, Typography } from '@arco-design/web-react';
import type { ColumnProps } from '@arco-design/web-react/es/Table';
import { IconPlus, IconRefresh } from '@arco-design/web-react/icon';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { qk, useModules } from '../../api/queries';
import type { TaskListItem, TaskState } from '../../api/types';
import { PageError } from '../../components/PageError';
import { PageHeader } from '../../components/PageHeader';
import { RelTime } from '../../components/RelTime';
import { SearchInput } from '../../components/SearchInput';
import { StateTag } from '../../components/StateTag';
import { ModuleSummaryCell } from '../../features/tasks/ModuleSummary';
import { TaskActionButtons } from '../../features/tasks/TaskActionButtons';
import { useTaskActions } from '../../features/tasks/useTaskActions';
import { compactNumber, percent, totalTokens } from '../../lib/format';
import { PAGE_SIZES, readPageSize, writePageSize } from '../../lib/prefs';
import { actionsFor, currentStage, exportedBefore, isTerminalState, stageLabel, stagePercent } from '../../lib/taskView';
import { zh } from '../../locales/zh';

const STATES: TaskState[] = ['created', 'queued', 'running', 'pausing', 'paused', 'stopping', 'stopped', 'succeeded', 'completed_with_errors', 'failed'];

function ProgressCell({ t }: { t: TaskListItem }) {
  const stages = t.progress.stages;
  if (t.state === 'created') return <span className="muted">{zh.taskList.created}</span>;
  if (t.state === 'queued') return <span className="muted">{zh.taskList.queued}</span>;
  const cur = currentStage(stages);
  if (!isTerminalState(t.state)) {
    const pct = stagePercent(cur);
    const label = cur ? stageLabel(cur.id) : '';
    const paused = t.state === 'paused' || t.state === 'pausing';
    return (
      <div style={{ minWidth: 160 }}>
        <Progress percent={pct} size="small" status={paused ? 'normal' : undefined} color={paused ? 'var(--c-text-4)' : undefined} />
        <div className="muted" style={{ fontSize: 12 }}>
          {paused && cur ? zh.taskList.pausedAt(label, cur.done, cur.total) : label}
          {!paused && cur?.eta_s ? ` · ${zh.taskList.etaShort(zh.time.duration(cur.eta_s))}` : ''}
        </div>
        {paused ? <div className="muted" style={{ fontSize: 12 }}>{t.pause_reason === 'system' ? zh.taskList.systemResumeHint : zh.taskList.resumeHint}</div> : null}
      </div>
    );
  }
  if (t.summary) {
    const s = t.summary;
    return (
      <div>
        <div>
          {zh.taskList.resultLine(s.passed, s.rejected, s.held)}
          {/* D40: episodes left out for missing source files are not part of total */}
          {s.skipped ? <span className="muted"> · {zh.taskList.skippedPart(s.skipped)}</span> : null}
        </div>
        <div className="muted" style={{ fontSize: 12 }}>
          {zh.taskList.passRate(percent(s.pass_rate))}
        </div>
      </div>
    );
  }
  if (cur) return <span className="muted">{zh.taskList.stoppedAt(cur.done, cur.total)}</span>;
  return <span className="muted">—</span>;
}

export function TaskListPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const modules = useModules();
  const actions = useTaskActions();
  const page = Math.max(1, Number(params.get('page') ?? 1) || 1);
  const pageSize = Number(params.get('page_size')) || readPageSize('tasks');
  const state = params.get('state') ?? '';
  const q = params.get('q') ?? '';
  const moduleFilter = (params.get('module') ?? '').split(',').filter(Boolean);
  const datasetId = params.get('dataset_id') ?? '';

  const query = useQuery({
    queryKey: qk.tasks({ page, pageSize, state, q, module: moduleFilter.join(','), datasetId }),
    queryFn: () =>
      unwrap(
        api().GET('/tasks', {
          params: {
            query: {
              page,
              page_size: pageSize as 10 | 20 | 50 | 100,
              ...(state ? { state: state as TaskState | 'deleted' } : {}),
              ...(q ? { q } : {}),
              ...(moduleFilter.length ? { module: moduleFilter.join(',') } : {}),
              ...(datasetId ? { dataset_id: datasetId } : {}),
            },
          },
        }),
      ),
    // Poll every 5 s while the page shows a task that is not finished (07 §4.1, P8).
    refetchInterval: (qr) => (qr.state.data?.items.some((i) => !isTerminalState(i.state) && !i.deleted_at) ? 5000 : false),
  });

  const dataset = useQuery({
    queryKey: qk.dataset(datasetId),
    queryFn: () => unwrap(api().GET('/datasets/{id}', { params: { path: { id: datasetId } } })),
    enabled: Boolean(datasetId),
  });

  const update = (patch: Record<string, string | null>, resetPage = true) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === '') next.delete(k);
      else next.set(k, v);
    }
    if (resetPage) next.delete('page');
    setParams(next);
  };

  const live = query.data?.items.some((i) => !isTerminalState(i.state) && !i.deleted_at);
  const deletedView = state === 'deleted';

  const columns: ColumnProps<TaskListItem>[] = [
    {
      title: zh.taskList.colName,
      dataIndex: 'name',
      render: (_: unknown, t) => (
        <div style={{ minWidth: 160 }}>
          <Link to={`/tasks/${t.id}`}>{t.name}</Link>
          <div style={{ marginTop: 2, display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {t.pending_adjudication > 0 ? (
              <Link to={`/tasks/${t.id}/adjudication`}>
                <Tag size="small" color="arcoblue">
                  {zh.taskList.pendingBadge(t.pending_adjudication)}
                </Tag>
              </Link>
            ) : null}
            {t.delivery_stale ? (
              <Tag size="small" color="orange">
                {exportedBefore(t.progress.stages) ? zh.taskList.deliveryStale : zh.taskList.deliveryNeverExported}
              </Tag>
            ) : null}
          </div>
          <div className="muted mono">{t.id}</div>
        </div>
      ),
    },
    {
      title: zh.taskList.colState,
      dataIndex: 'state',
      render: (_: unknown, t) => <StateTag state={t.state} pauseReason={t.pause_reason} />,
    },
    {
      title: zh.taskList.colDataset,
      dataIndex: 'dataset',
      render: (_: unknown, t) => (
        <div>
          {t.dataset_id ? <Link to={`/datasets/${t.dataset_id}`}>{t.dataset}</Link> : t.dataset}
          {t.summary ? <div className="muted">{zh.common.items(t.summary.total)}</div> : null}
        </div>
      ),
    },
    { title: zh.taskList.colModules, dataIndex: 'modules', render: (_: unknown, t) => <ModuleSummaryCell item={t} /> },
    { title: zh.taskList.colProgress, dataIndex: 'progress', render: (_: unknown, t) => <ProgressCell t={t} /> },
    {
      title: zh.taskList.colTokens,
      dataIndex: 'usage',
      render: (_: unknown, t) => <span className="mono">{totalTokens(t.usage) ? compactNumber(totalTokens(t.usage)) : '—'}</span>,
    },
    {
      title: deletedView ? zh.taskList.deletedAt : zh.taskList.colCreated,
      dataIndex: 'created_at',
      render: (_: unknown, t) => <RelTime ms={deletedView ? t.deleted_at : t.created_at} />,
    },
    {
      title: zh.taskList.colActions,
      dataIndex: 'id',
      fixed: 'right',
      render: (_: unknown, t) => (
        <TaskActionButtons
          plan={actionsFor(t)}
          held={t.summary?.held}
          exported={exportedBefore(t.progress.stages)}
          onAction={(key) => actions.run(key, { id: t.id, name: t.name, held: t.summary?.held, exported: exportedBefore(t.progress.stages) })}
        />
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        crumbs={[{ label: zh.taskList.title }]}
        title={zh.taskList.title}
        description={zh.taskList.desc}
        extra={
          <Button type="primary" icon={<IconPlus />} onClick={() => navigate('/tasks/new')}>
            {zh.taskList.newTask}
          </Button>
        }
      />
      <Card>
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center', marginBottom: 12 }}>
          <SearchInput value={q} placeholder={zh.taskList.searchPlaceholder} onSearch={(v) => update({ q: v || null })} />
          <Select
            style={{ width: 200 }}
            value={state}
            onChange={(v: string) => update({ state: v || null })}
            aria-label={zh.taskList.colState}
            options={[{ label: zh.taskList.allStates, value: '' }, ...STATES.map((s) => ({ label: zh.state[s], value: s })), { label: zh.state.deleted, value: 'deleted' }]}
          />
          <Select
            mode="multiple"
            allowClear
            style={{ minWidth: 240, maxWidth: 420 }}
            placeholder={zh.taskList.modulesFilterPlaceholder}
            value={moduleFilter}
            onChange={(v: string[]) => update({ module: v.join(',') || null })}
            aria-label={zh.taskList.modulesFilter}
            options={(modules.data?.modules ?? []).map((m) => ({ label: m.name_zh, value: m.id }))}
          />
          {datasetId ? (
            <Tag closable onClose={() => update({ dataset_id: null })} color="arcoblue" aria-label={zh.taskList.clearDatasetFilter}>
              {zh.taskList.datasetFilter(dataset.data?.name ?? datasetId)}
            </Tag>
          ) : null}
          <div style={{ flex: 1 }} />
          {live ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {zh.taskList.autoRefresh}
            </Typography.Text>
          ) : null}
          <Tooltip content={zh.common.refresh}>
            <Button icon={<IconRefresh />} aria-label={zh.common.refresh} onClick={() => void query.refetch()} />
          </Tooltip>
        </div>
        {query.isError && !query.data ? (
          <PageError error={query.error} onRetry={() => void query.refetch()} />
        ) : (
          <Table
            rowKey="id"
            loading={query.isLoading}
            columns={columns}
            data={query.data?.items ?? []}
            scroll={{ x: 1200 }}
            noDataElement={<div className="muted" style={{ padding: 24 }}>{q || state || moduleFilter.length || datasetId ? zh.taskList.emptyFiltered : zh.taskList.empty}</div>}
            pagination={{
              current: page,
              pageSize,
              total: query.data?.total ?? 0,
              showTotal: (total: number) => zh.common.total(total),
              sizeCanChange: true,
              sizeOptions: [...PAGE_SIZES],
              showJumper: true,
              onChange: (p: number, size: number) => {
                if (size !== pageSize) writePageSize('tasks', size);
                update({ page: String(p), page_size: String(size) }, false);
              },
            }}
          />
        )}
      </Card>
      <Space>{actions.dialogs}</Space>
    </div>
  );
}
