import { Button, Card, Message, Radio, Select, Space, Table, Typography } from '@arco-design/web-react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { isApiError } from '../../api/errors';
import { moduleName, qk, useModules } from '../../api/queries';
import type { ModuleRegistry, ModuleTableSpec, ReportModuleSection } from '../../api/types';
import { PageError } from '../../components/PageError';
import { fieldLabel, formatValue } from '../../lib/reportView';
import { zh } from '../../locales/zh';

export const TABLE_PAGE_SIZE = 100;

export interface TableChoice {
  id: string;
  module: string;
  rows: number;
  spec: ModuleTableSpec | undefined;
}

export function tableChoices(sections: readonly ReportModuleSection[], registry: ModuleRegistry | undefined): TableChoice[] {
  return sections.flatMap((s) =>
    s.tables.map((t) => ({ id: t.id, module: s.id, rows: t.rows, spec: registry?.modules.flatMap((m) => m.tables).find((x) => x.id === t.id) })),
  );
}

/** Links an episode index to the drawer, keeping the other query parameters (?rev=…). */
export function EpisodeLink({ ep }: { ep: number }) {
  const [params] = useSearchParams();
  const next = new URLSearchParams(params);
  next.set('ep', String(ep));
  return (
    <Link to={{ search: `?${next.toString()}`, hash: '' }} className="mono">
      {zh.report.episode(ep)}
    </Link>
  );
}

function cell(column: string, v: unknown) {
  if ((column === 'episode_index' || column === 'duplicate_of') && typeof v === 'number') return <EpisodeLink ep={v} />;
  return formatValue(v);
}

/** Rows of one table page. Also used inline for small tables inside a module section. */
export function useTablePage(taskId: string, rev: number, table: string | undefined, sort: string, order: 'asc' | 'desc', cursor: string | null, enabled = true) {
  return useQuery({
    queryKey: [...qk.reportTable(taskId, table ?? '', rev, sort, order), cursor],
    queryFn: () =>
      unwrap(
        api().GET('/tasks/{id}/report/tables/{table}', {
          params: { path: { id: taskId, table: table! }, query: { rev, limit: TABLE_PAGE_SIZE, sort, order, ...(cursor ? { cursor } : {}) } },
        }),
      ),
    enabled: enabled && Boolean(table),
    placeholderData: keepPreviousData,
    retry: false,
  });
}

export function RowsTable({ columns, items, testId }: { columns: string[]; items: Record<string, unknown>[]; testId?: string }) {
  return (
    <Table
      rowKey="__key"
      size="small"
      pagination={false}
      scroll={{ x: Math.max(600, columns.length * 120) }}
      data={items.map((r, i) => ({ ...r, __key: `${String(r.episode_index ?? '')}-${i}` }))}
      columns={columns.map((c) => ({ title: fieldLabel(c), dataIndex: c, render: (v: unknown) => cell(c, v) }))}
      data-testid={testId}
    />
  );
}

/**
 * 明细表 (07 §5): server-side slices of 100 rows, sort limited to the registry whitelist, cursor
 * pages (the cursor carries the revision; result_changed sends the table back to page one).
 */
export function DetailTables({
  taskId,
  rev,
  sections,
  selected,
  onSelect,
  onResultChanged,
}: {
  taskId: string;
  rev: number;
  sections: readonly ReportModuleSection[];
  selected: string | null;
  onSelect: (table: string) => void;
  onResultChanged: () => void;
}) {
  const reg = useModules();
  const choices = tableChoices(sections, reg.data);
  const current = choices.find((c) => c.id === selected) ?? choices[0];
  const [sortState, setSortState] = useState<{ table: string; sort: string; order: 'asc' | 'desc' } | null>(null);
  const sort = sortState && sortState.table === current?.id ? sortState.sort : current?.spec?.default_sort ?? 'episode_index';
  const order = sortState && sortState.table === current?.id ? sortState.order : 'asc';
  const viewKey = `${current?.id}|${sort}|${order}|${rev}`;
  const [pages, setPages] = useState<{ key: string; cursors: (string | null)[] }>({ key: viewKey, cursors: [null] });
  const cursors = pages.key === viewKey ? pages.cursors : [null];
  const cursor = cursors[cursors.length - 1];
  const q = useTablePage(taskId, rev, current?.id, sort, order, cursor);
  const changed = q.error && isApiError(q.error, 'result_changed');

  useEffect(() => {
    if (!changed) return;
    Message.info(zh.report.resultChanged);
    setPages({ key: viewKey, cursors: [null] });
    onResultChanged();
    // Only react to a new result_changed error.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [changed]);

  if (!choices.length || !current) {
    return (
      <Card title={zh.report.tables} id="detail-tables" className="section-anchor">
        <Typography.Text type="secondary">{zh.report.noTables}</Typography.Text>
      </Card>
    );
  }
  const totalPages = Math.max(1, Math.ceil(current.rows / TABLE_PAGE_SIZE));
  const sortable = current.spec?.sortable ?? ['episode_index'];
  return (
    <Card title={zh.report.tables} id="detail-tables" className="section-anchor" extra={<span className="muted">{zh.report.tablesDesc}</span>}>
      <Space wrap style={{ marginBottom: 12 }}>
        <Select
          style={{ width: 340 }}
          value={current.id}
          onChange={(v: string) => onSelect(v)}
          aria-label={zh.report.tablePick}
          options={choices.map((c) => ({ label: zh.report.tableOption(moduleName(reg.data, c.module), c.spec?.title_zh ?? c.id, c.rows), value: c.id }))}
        />
        <Select
          style={{ width: 180 }}
          value={sort}
          onChange={(v: string) => setSortState({ table: current.id, sort: v, order })}
          aria-label={zh.report.sortBy}
          options={sortable.map((c) => ({ label: fieldLabel(c), value: c }))}
        />
        <Radio.Group type="button" value={order} onChange={(v: 'asc' | 'desc') => setSortState({ table: current.id, sort, order: v })} aria-label={zh.report.sortBy}>
          <Radio value="asc">{zh.report.asc}</Radio>
          <Radio value="desc">{zh.report.desc}</Radio>
        </Radio.Group>
      </Space>
      {q.error && !changed && !q.data ? (
        <PageError error={q.error} onRetry={() => void q.refetch()} />
      ) : (
        <>
          <RowsTable columns={q.data?.columns ?? ['episode_index']} items={(q.data?.items ?? []) as Record<string, unknown>[]} testId="detail-table" />
          <Space style={{ marginTop: 12, justifyContent: 'flex-end', width: '100%' }}>
            <span className="muted" data-testid="table-page">
              {zh.report.pageOf(cursors.length, totalPages)}
            </span>
            <Button size="small" disabled={cursors.length <= 1 || q.isFetching} onClick={() => setPages({ key: viewKey, cursors: cursors.slice(0, -1) })}>
              {zh.report.prev}
            </Button>
            <Button
              size="small"
              disabled={!q.data?.has_more || q.isFetching || q.isPlaceholderData}
              onClick={() => q.data?.next_cursor && setPages({ key: viewKey, cursors: [...cursors, q.data.next_cursor] })}
            >
              {zh.report.next}
            </Button>
          </Space>
        </>
      )}
    </Card>
  );
}
