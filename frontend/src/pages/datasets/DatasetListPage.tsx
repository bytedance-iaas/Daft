import { Button, Card, Select, Space, Table, Tag, Typography } from '@arco-design/web-react';
import type { ColumnProps } from '@arco-design/web-react/es/Table';
import { IconPlus } from '@arco-design/web-react/icon';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { qk } from '../../api/queries';
import type { DatasetFormat, DatasetItem } from '../../api/types';
import { PageError } from '../../components/PageError';
import { PageHeader } from '../../components/PageHeader';
import { RelTime } from '../../components/RelTime';
import { SearchInput } from '../../components/SearchInput';
import { StateTag } from '../../components/StateTag';
import { AddDatasetDrawer } from '../../features/datasets/AddDatasetDrawer';
import { useDatasetActions } from '../../features/datasets/useDatasetActions';
import { grouped } from '../../lib/format';
import { PAGE_SIZES, readPageSize, writePageSize } from '../../lib/prefs';
import { zh } from '../../locales/zh';

export function CheckTag({ d }: { d: Pick<DatasetItem, 'check_state' | 'checked_at'> }) {
  if (d.check_state === 'changed') return <Tag color="orange">{zh.checkState.changed}</Tag>;
  if (!d.checked_at) return <Tag>{zh.checkState.unchecked}</Tag>;
  return <Tag color="green">{zh.checkState.ok}</Tag>;
}

export function FormatTag({ format }: { format: DatasetFormat }) {
  return <Tag color={format === 'unsupported' ? 'red' : 'arcoblue'}>{zh.format[format] ?? format}</Tag>;
}

/** 数据集 (07 §4.4, D36): registered datasets, page-number pagination, search and filters. */
export function DatasetListPage() {
  const [params, setParams] = useSearchParams();
  const [adding, setAdding] = useState(false);
  const actions = useDatasetActions();
  const page = Math.max(1, Number(params.get('page') ?? 1) || 1);
  const pageSize = Number(params.get('page_size')) || readPageSize('datasets');
  const q = params.get('q') ?? '';
  const format = params.get('format') ?? '';
  const check = params.get('check_state') ?? '';
  const list = useQuery({
    queryKey: qk.datasets({ page, pageSize, q, format, check }),
    queryFn: () =>
      unwrap(
        api().GET('/datasets', {
          params: {
            query: {
              page,
              page_size: pageSize as 10 | 20 | 50 | 100,
              ...(q ? { q } : {}),
              ...(format ? { format: format as DatasetFormat } : {}),
              ...(check ? { check_state: check as 'ok' | 'changed' } : {}),
            },
          },
        }),
      ),
  });
  const update = (patch: Record<string, string | null>, resetPage = true) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (!v) next.delete(k);
      else next.set(k, v);
    }
    if (resetPage) next.delete('page');
    setParams(next);
  };
  const columns: ColumnProps<DatasetItem>[] = [
    { title: zh.datasets.colName, dataIndex: 'name', render: (_: unknown, d) => <Link to={`/datasets/${d.id}`}>{d.name}</Link> },
    { title: zh.datasets.colSource, dataIndex: 'source', render: (v: string) => zh.source[v] ?? v },
    { title: zh.datasets.colUri, dataIndex: 'uri', render: (v: string) => <span className="mono">{v}</span> },
    { title: zh.datasets.colFormat, dataIndex: 'format', render: (_: unknown, d) => <FormatTag format={d.format} /> },
    { title: zh.datasets.colEpisodes, dataIndex: 'episode_count', render: (v: number | null) => grouped(v) },
    { title: zh.datasets.colRobot, dataIndex: 'robot_type', render: (v: string | null) => v ?? <span className="muted">{zh.common.unknown}</span> },
    { title: zh.datasets.colCheck, dataIndex: 'check_state', render: (_: unknown, d) => <CheckTag d={d} /> },
    {
      title: zh.datasets.colLastTask,
      dataIndex: 'last_task',
      render: (_: unknown, d) =>
        d.last_task ? (
          <Space size={4}>
            <Link to={`/tasks/${d.last_task.id}`}>{d.last_task.name}</Link>
            <StateTag state={d.last_task.state} size="small" />
          </Space>
        ) : (
          <span className="muted">—</span>
        ),
    },
    { title: zh.datasets.colCreated, dataIndex: 'created_at', render: (v: number) => <RelTime ms={v} /> },
    {
      title: zh.datasets.colOps,
      dataIndex: 'id',
      fixed: 'right',
      render: (_: unknown, d) => (
        <Space size={4}>
          <Button type="text" size="small" disabled={d.format === 'unsupported'} onClick={() => actions.newTask(d)}>
            {zh.datasets.newTask}
          </Button>
          <Button type="text" size="small" onClick={() => actions.recheck(d)}>
            {zh.datasets.recheck}
          </Button>
          <Button type="text" size="small" status="danger" onClick={() => actions.remove(d)}>
            {zh.datasets.delete}
          </Button>
        </Space>
      ),
    },
  ];
  return (
    <div>
      <PageHeader
        crumbs={[{ label: zh.datasets.title }]}
        title={zh.datasets.title}
        description={zh.datasets.desc}
        extra={
          <Button type="primary" icon={<IconPlus />} onClick={() => setAdding(true)}>
            {zh.datasets.add}
          </Button>
        }
      />
      <Card>
        <Space wrap style={{ marginBottom: 12 }}>
          <SearchInput value={q} placeholder={zh.datasets.searchPlaceholder} onSearch={(v) => update({ q: v || null })} />
          <Select
            style={{ width: 160 }}
            value={format}
            onChange={(v: string) => update({ format: v || null })}
            aria-label={zh.datasets.colFormat}
            options={[{ label: zh.datasets.allFormats, value: '' }, ...(['lerobot_v2', 'lerobot_v3', 'unsupported'] as const).map((f) => ({ label: zh.format[f], value: f }))]}
          />
          <Select
            style={{ width: 200 }}
            value={check}
            onChange={(v: string) => update({ check_state: v || null })}
            aria-label={zh.datasets.colCheck}
            options={[
              { label: zh.datasets.allChecks, value: '' },
              { label: zh.checkState.ok, value: 'ok' },
              { label: zh.checkState.changed, value: 'changed' },
            ]}
          />
        </Space>
        {list.isError && !list.data ? (
          <PageError error={list.error} onRetry={() => void list.refetch()} />
        ) : (
          <Table
            rowKey="id"
            loading={list.isLoading}
            columns={columns}
            data={list.data?.items ?? []}
            scroll={{ x: 1200 }}
            noDataElement={<Typography.Text type="secondary">{q || format || check ? zh.datasets.emptyFiltered : zh.datasets.empty}</Typography.Text>}
            pagination={{
              current: page,
              pageSize,
              total: list.data?.total ?? 0,
              showTotal: (total: number) => zh.common.total(total),
              sizeCanChange: true,
              sizeOptions: [...PAGE_SIZES],
              onChange: (p: number, size: number) => {
                if (size !== pageSize) writePageSize('datasets', size);
                update({ page: String(p), page_size: String(size) }, false);
              },
            }}
          />
        )}
      </Card>
      <AddDatasetDrawer visible={adding} onClose={() => setAdding(false)} />
      {actions.dialogs}
    </div>
  );
}
