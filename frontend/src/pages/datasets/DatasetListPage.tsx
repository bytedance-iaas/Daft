import { Button, Card, Dropdown, Menu, Select, Space, Table, Tag, Tooltip, Typography } from '@arco-design/web-react';
import type { ColumnProps } from '@arco-design/web-react/es/Table';
import { IconDown, IconPlus } from '@arco-design/web-react/icon';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { qk } from '../../api/queries';
import type { DatasetFormat, DatasetItem } from '../../api/types';
import { PageError } from '../../components/PageError';
import { OneLine } from '../../components/OneLine';
import { PageHeader } from '../../components/PageHeader';
import { RelTime } from '../../components/RelTime';
import { SearchInput } from '../../components/SearchInput';
import { AddDatasetDrawer } from '../../features/datasets/AddDatasetDrawer';
import { McapConfigDrawer } from '../../features/datasets/McapConfigDrawer';
import { useDatasetActions } from '../../features/datasets/useDatasetActions';
import { VisualizeButton } from '../../features/datasets/VisualizeButton';
import { grouped } from '../../lib/format';
import { PAGE_SIZES, readPageSize, writePageSize } from '../../lib/prefs';
import { rerunViewerUrl } from '../../lib/rerun';
import { zh } from '../../locales/zh';

/** The fingerprint state; ``short`` (the list's narrow column) says 有变化 and keeps the rest for the tooltip. */
export function CheckTag({ d, short = false }: { d: Pick<DatasetItem, 'check_state' | 'checked_at'>; short?: boolean }) {
  if (d.check_state === 'changed')
    return short ? (
      <Tooltip content={zh.checkState.changed}>
        <Tag color="orange">{zh.checkState.changedShort}</Tag>
      </Tooltip>
    ) : (
      <Tag color="orange">{zh.checkState.changed}</Tag>
    );
  if (!d.checked_at) return <Tag>{zh.checkState.unchecked}</Tag>;
  return <Tag color="green">{zh.checkState.ok}</Tag>;
}

export function FormatTag({ format }: { format: DatasetFormat }) {
  return <Tag color={format === 'unsupported' ? 'red' : 'arcoblue'}>{zh.format[format] ?? format}</Tag>;
}

/** Under an mcap dataset's format: the confirmed mapping's name, or 待确认 (design doc 18 §6.4 step 7). */
export function MappingLine({ d }: { d: Pick<DatasetItem, 'viz_mapping'> }) {
  const m = d.viz_mapping;
  if (!m) return null;
  return m.state === 'confirmed' ? (
    <div className="muted" style={{ fontSize: 12 }} data-testid="mapping-line">
      <OneLine text={zh.mcap.listConfirmed(m.name, m.version)} />
    </div>
  ) : (
    <div style={{ fontSize: 12, color: 'var(--c-warning)' }} data-testid="mapping-line">
      {zh.mcap.listPending}
    </div>
  );
}

/**
 * A row's 「更多」 (requester, 2026-10-04: a row shows only 可视化, 新建任务 and 更多): 可视化（旧） opens the
 * ReRun viewer in a new tab (disabled for a locally mounted dataset, saying why), 「mcap 配置」 for an
 * mcap dataset, and a red 删除.
 */
function MoreOps({ d, onMapping, onDelete }: { d: DatasetItem; onMapping: () => void; onDelete: () => void }) {
  const legacy = rerunViewerUrl(d);
  return (
    <Dropdown
      trigger="click"
      position="br"
      droplist={
        <Menu onClickMenuItem={(key) => (key === 'mcap' ? onMapping() : key === 'delete' ? onDelete() : undefined)}>
          <Menu.Item key="legacy" disabled={!legacy}>
            {legacy ? (
              <a href={legacy} target="_blank" rel="noopener noreferrer" title={zh.datasets.visualizeLegacyTitle}>
                {zh.datasets.visualizeLegacy}
              </a>
            ) : (
              <span title={zh.datasets.visualizeLocal}>{zh.datasets.visualizeLegacy}</span>
            )}
          </Menu.Item>
          {d.viz_mapping ? <Menu.Item key="mcap">{zh.mcap.entry}</Menu.Item> : null}
          <Menu.Item key="delete">
            <span style={{ color: 'var(--c-danger)' }}>{zh.datasets.delete}</span>
          </Menu.Item>
        </Menu>
      }
    >
      <Button type="text" size="small" aria-label={zh.datasets.moreOf(d.name)}>
        {zh.common.more} <IconDown />
      </Button>
    </Dropdown>
  );
}

/** 数据集 (07 §4.4, D36): registered datasets, page-number pagination, search and filters. */
export function DatasetListPage() {
  const [params, setParams] = useSearchParams();
  const [adding, setAdding] = useState(false);
  const [mapping, setMapping] = useState<DatasetItem | null>(null);
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
  // Widths (07 §3.1): every row on one line (fourth round) - a long name, address or robot type
  // ends in an ellipsis with the whole of it in a tooltip; the columns keep fixed widths, so a
  // narrow window scrolls the table rather than wrapping it.
  const columns: ColumnProps<DatasetItem>[] = [
    { title: zh.datasets.colName, dataIndex: 'name', width: 150, render: (_: unknown, d) => <Link to={`/datasets/${d.id}`}><OneLine text={d.name} /></Link> },
    { title: zh.datasets.colSource, dataIndex: 'source', width: 165, render: (v: string) => <span className="nowrap">{zh.source[v] ?? v}</span> },
    { title: zh.datasets.colUri, dataIndex: 'uri', width: 220, render: (v: string) => <OneLine text={v} mono /> },
    {
      title: zh.datasets.colFormat,
      dataIndex: 'format',
      width: 160,
      render: (_: unknown, d) => (
        <div>
          <FormatTag format={d.format} />
          <MappingLine d={d} />
        </div>
      ),
    },
    { title: zh.datasets.colEpisodes, dataIndex: 'episode_count', width: 90, render: (v: number | null) => grouped(v) },
    { title: zh.datasets.colRobot, dataIndex: 'robot_type', width: 130, render: (v: string | null) => (v ? <OneLine text={v} /> : <span className="muted">{zh.common.unknown}</span>) },
    { title: zh.datasets.colCheck, dataIndex: 'check_state', width: 100, render: (_: unknown, d) => <span className="nowrap"><CheckTag d={d} short /></span> },
    {
      // the task's name links to it; its state is on the task (fifth round)
      title: zh.datasets.colLastTask,
      dataIndex: 'last_task',
      width: 170,
      render: (_: unknown, d) =>
        d.last_task ? (
          <Link to={`/tasks/${d.last_task.id}`}>
            <OneLine text={d.last_task.name} />
          </Link>
        ) : (
          <span className="muted">—</span>
        ),
    },
    { title: zh.datasets.colCreated, dataIndex: 'created_at', width: 90, render: (v: number) => <span className="nowrap"><RelTime ms={v} /></span> },
    {
      title: zh.datasets.colOps,
      dataIndex: 'id',
      fixed: 'right',
      width: 230,
      // 可视化 / 新建任务 / 更多 (requester, 2026-10-04); 重新检查 stays on the detail page.
      render: (_: unknown, d) => (
        <Space size={4}>
          <VisualizeButton d={d} />
          <Button type="text" size="small" disabled={d.format === 'unsupported'} onClick={() => actions.newTask(d)}>
            {zh.datasets.newTaskShort}
          </Button>
          <MoreOps d={d} onMapping={() => setMapping(d)} onDelete={() => actions.remove(d)} />
        </Space>
      ),
    },
  ];
  return (
    <div>
      <PageHeader
        crumbs={[{ label: zh.datasets.title }]}
        title={zh.datasets.title}
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
            options={[{ label: zh.datasets.allFormats, value: '' }, ...(['lerobot_v2', 'lerobot_v3', 'mcap', 'lance', 'unsupported'] as const).map((f) => ({ label: zh.format[f], value: f }))]}
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
            scroll={{ x: 1525 }}
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
      <McapConfigDrawer dataset={mapping} onClose={() => setMapping(null)} />
      {actions.dialogs}
    </div>
  );
}
