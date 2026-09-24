import { Button, Card, Radio, Space, Table, Tag, Tooltip } from '@arco-design/web-react';
import type { ColumnProps } from '@arco-design/web-react/es/Table';
import { IconRefresh } from '@arco-design/web-react/icon';
import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { EVENTS_CONFIG } from '../../api/events';
import { qk } from '../../api/queries';
import type { TaskListItem } from '../../api/types';
import { PageError } from '../../components/PageError';
import { PageHeader } from '../../components/PageHeader';
import { RelTime } from '../../components/RelTime';
import { SearchInput } from '../../components/SearchInput';
import { TaskStateTag } from '../../components/StateTag';
import { percent } from '../../lib/format';
import { PAGE_SIZES, readPageSize, writePageSize } from '../../lib/prefs';
import { zh } from '../../locales/zh';

type Kind = 'reports' | 'adjudication';

const Z = () => zh.results;

/**
 * 质检报告 and 人工裁决 under 质检 in the sidebar (requester, third round): the tasks with a
 * result, across tasks (C4 1.15 `has_result`), each opening its report or its adjudication page.
 * 人工裁决 lists the tasks with pending items first (`pending_adjudication`); 全部 adds the other
 * tasks with a result, whose rejects may still be appealed (D47).
 */
function ResultListPage({ kind }: { kind: Kind }) {
  const [params, setParams] = useSearchParams();
  const page = Math.max(1, Number(params.get('page') ?? 1) || 1);
  const pageSize = Number(params.get('page_size')) || readPageSize(kind);
  const q = params.get('q') ?? '';
  const pendingOnly = kind === 'adjudication' && params.get('show') !== 'all';

  const query = useQuery({
    queryKey: qk.tasks({ page, pageSize, q, hasResult: true, pendingOnly }),
    queryFn: () =>
      unwrap(
        api().GET('/tasks', {
          params: {
            query: {
              page,
              page_size: pageSize as 10 | 20 | 50 | 100,
              has_result: true,
              ...(pendingOnly ? { pending_adjudication: true } : {}),
              ...(q ? { q } : {}),
            },
          },
        }),
      ),
    // A subtask under way (an adjudication run, a retry) changes the counts on its own.
    refetchInterval: (qr) => (qr.state.data?.items.some((t) => Boolean(t.active_subtask)) ? EVENTS_CONFIG.pollMs : false),
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

  const title = kind === 'reports' ? Z().reportsTitle : Z().adjudicationTitle;
  const open = (t: TaskListItem) => (kind === 'reports' ? `/tasks/${t.id}/report` : `/tasks/${t.id}/adjudication`);
  const columns: ColumnProps<TaskListItem>[] = [
    {
      title: zh.taskList.colName,
      dataIndex: 'name',
      width: 260,
      render: (_: unknown, t) => (
        <div style={{ minWidth: 160 }}>
          <Link to={open(t)}>{t.name}</Link>
          <div className="muted mono">{t.id}</div>
        </div>
      ),
    },
    { title: zh.taskList.colState, dataIndex: 'state', width: 130, render: (_: unknown, t) => <TaskStateTag task={t} /> },
    {
      title: zh.taskList.colDataset,
      dataIndex: 'dataset',
      width: 170,
      render: (_: unknown, t) => (t.dataset_id ? <Link to={`/datasets/${t.dataset_id}`}>{t.dataset}</Link> : t.dataset),
    },
    {
      title: Z().colResult,
      dataIndex: 'summary',
      width: 240,
      render: (_: unknown, t) =>
        t.summary ? (
          <div>
            <div>{zh.taskList.resultLine(t.summary.passed, t.summary.rejected, t.summary.held)}</div>
            <div className="muted" style={{ fontSize: 12 }}>
              {zh.taskList.passRate(percent(t.summary.pass_rate))}
            </div>
          </div>
        ) : (
          <span className="muted">—</span>
        ),
    },
    {
      title: Z().colPending,
      dataIndex: 'pending_adjudication',
      width: 120,
      render: (v: number) =>
        v ? (
          <Tag size="small" color="arcoblue">
            {zh.taskList.pendingBadge(v)}
          </Tag>
        ) : (
          <span className="muted">—</span>
        ),
    },
    { title: zh.taskList.colCreated, dataIndex: 'created_at', width: 120, render: (v: number) => <RelTime ms={v} /> },
    {
      title: zh.taskList.colActions,
      dataIndex: 'id',
      fixed: 'right',
      width: 260,
      // 查看报告 first, then 人工裁决 (requester item 14); the page's own one is blue
      render: (_: unknown, t) => (
        <Space size={4}>
          <Link to={`/tasks/${t.id}/report`}>
            <Button size="small" type={kind === 'reports' ? 'primary' : 'text'}>
              {zh.actions.report}
            </Button>
          </Link>
          <Link to={`/tasks/${t.id}/adjudication`}>
            <Button size="small" type={kind === 'adjudication' ? 'primary' : 'text'}>
              {t.pending_adjudication ? zh.actions.adjudicateCount(t.pending_adjudication) : zh.actions.adjudicate}
            </Button>
          </Link>
        </Space>
      ),
    },
  ];
  const empty = q ? zh.taskList.emptyFiltered : kind === 'reports' ? Z().emptyReports : pendingOnly ? Z().emptyPending : Z().emptyReports;

  return (
    <div>
      <PageHeader crumbs={[{ label: title }]} title={title} />
      <Card>
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center', marginBottom: 12 }}>
          <SearchInput value={q} placeholder={zh.taskList.searchPlaceholder} onSearch={(v) => update({ q: v || null })} />
          {kind === 'adjudication' ? (
            <Radio.Group
              type="button"
              value={pendingOnly ? 'pending' : 'all'}
              onChange={(v: string) => update({ show: v === 'all' ? 'all' : null })}
              options={[
                { label: Z().pendingOnly, value: 'pending' },
                { label: Z().withResult, value: 'all' },
              ]}
            />
          ) : null}
          <div style={{ flex: 1 }} />
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
            scroll={{ x: 1300 }}
            data-testid={`${kind}-list`}
            noDataElement={<div className="muted" style={{ padding: 24 }}>{empty}</div>}
            pagination={{
              current: page,
              pageSize,
              total: query.data?.total ?? 0,
              showTotal: (total: number) => zh.common.total(total),
              sizeCanChange: true,
              sizeOptions: [...PAGE_SIZES],
              onChange: (p: number, size: number) => {
                if (size !== pageSize) writePageSize(kind, size);
                update({ page: String(p), page_size: String(size) }, false);
              },
            }}
          />
        )}
      </Card>
    </div>
  );
}

export function ReportListPage() {
  return <ResultListPage kind="reports" />;
}

export function AdjudicationListPage() {
  return <ResultListPage kind="adjudication" />;
}
