import { Alert, Button, Card, Descriptions, Dropdown, Menu, Space, Spin, Table, Tag, Typography } from '@arco-design/web-react';
import { IconDown } from '@arco-design/web-react/icon';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { qk, useBackends, useModules } from '../../api/queries';
import type { DatasetCheck, TaskRef } from '../../api/types';
import { PageError } from '../../components/PageError';
import { PageHeader } from '../../components/PageHeader';
import { RelTime } from '../../components/RelTime';
import { regionLabel } from '../../components/RegionSelect';
import { StateTag } from '../../components/StateTag';
import { useDatasetActions } from '../../features/datasets/useDatasetActions';
import { VisualizeButton } from '../../features/datasets/VisualizeButton';
import { bytes, grouped } from '../../lib/format';
import { reasonText, withVlmBackends } from '../../lib/preflight';
import { zh } from '../../locales/zh';
import { CheckTag, FormatTag } from './DatasetListPage';

/** 数据集详情 (07 §4.4): preflight result, fingerprints and their history, tasks run on it. */
export function DatasetDetailPage() {
  const { id } = useParams();
  const reg = useModules();
  const backends = useBackends();
  const actions = useDatasetActions();
  const q = useQuery({ queryKey: qk.dataset(id ?? ''), queryFn: () => unwrap(api().GET('/datasets/{id}', { params: { path: { id: id! } } })), enabled: Boolean(id) });
  const crumbs = [{ label: zh.datasets.title, to: '/datasets' }, { label: q.data?.name ?? id ?? '' }];
  if (q.isError && !q.data) {
    return (
      <>
        <PageHeader crumbs={crumbs} title={zh.datasets.title} />
        <PageError error={q.error} onRetry={() => void q.refetch()} />
      </>
    );
  }
  if (!q.data) return <Spin style={{ display: 'block', margin: '80px auto' }} />;
  const d = q.data;
  const pf = d.preflight;
  const ds = pf.dataset;
  const basic = [
    { label: zh.datasets.colSource, value: zh.source[d.source] ?? d.source },
    { label: zh.datasets.colUri, value: <span className="mono">{d.uri}</span> },
    { label: zh.datasets.region, value: d.source === 'public' ? zh.taskForm.publicRegion : regionLabel(d.region) },
    { label: zh.datasets.credential, value: d.source === 'public' ? zh.taskForm.publicKey : d.credential ?? zh.datasets.credentialGone },
    { label: zh.datasets.colCreated, value: <RelTime ms={d.created_at} /> },
    { label: zh.datasets.preflightedAt, value: <RelTime ms={d.preflighted_at} /> },
    { label: zh.datasets.checkedAt, value: <RelTime ms={d.checked_at} /> },
    { label: zh.datasets.fpMeta, value: <span className="mono">{d.meta_fingerprint}</span> },
    { label: zh.datasets.fpListing, value: `${zh.datasets.fpListingValue(d.listing.objects, bytes(d.listing.bytes))} · ${d.listing.digest}` },
    ...(d.note ? [{ label: zh.datasets.note, value: d.note }] : []),
  ];
  const preflight = ds
    ? [
        { label: zh.datasets.colFormat, value: pf.format.kind === 'lerobot' ? `LeRobot ${pf.format.version ?? ''}` : pf.format.kind },
        { label: zh.datasets.colEpisodes, value: grouped(ds.episode_count) },
        { label: zh.datasets.cameras, value: ds.cameras.join('、') || '—' },
        { label: zh.datasets.fps, value: ds.fps ?? '—' },
        { label: zh.datasets.colRobot, value: ds.robot_type ?? zh.common.unknown },
        { label: zh.datasets.labels, value: zh.datasets.labelsValue(ds.labels.with_task, ds.labels.without_task) },
        { label: zh.datasets.profile, value: ds.profile ? zh.taskForm.profileHit(ds.profile.matched, ds.profile.by) : zh.taskForm.profileMiss },
      ]
    : [{ label: zh.datasets.colFormat, value: reasonText(pf.modules[0]) || pf.format.detail }];
  const moduleRows = (reg.data?.modules ?? []).map((m) => {
    const a = pf.modules.find((x) => x.id === m.id);
    const row = withVlmBackends({ availability: pf.format.supported ? a?.availability ?? 'available' : 'unsupported', reason: reasonText(a) }, a, backends.data?.items);
    return { id: m.id, name: m.name_zh, ...row };
  });
  return (
    <div>
      <PageHeader
        crumbs={crumbs}
        title={d.name}
        docTitle={d.name}
        titleExtra={
          <Space>
            <FormatTag format={d.format} />
            <CheckTag d={d} />
          </Space>
        }
        description={<span className="mono">{d.uri}</span>}
        extra={
          <Space>
            <Button type="primary" disabled={d.format === 'unsupported'} onClick={() => actions.newTask(d)}>
              {zh.datasets.newTask}
            </Button>
            <VisualizeButton d={d} type="secondary" size="default" />
            {d.check_state === 'changed' ? <Button onClick={() => actions.repreflight(d)}>{zh.datasets.repreflight}</Button> : null}
            <Button onClick={() => actions.recheck(d)}>{zh.datasets.recheck}</Button>
            <Dropdown
              trigger="click"
              position="br"
              droplist={
                <Menu onClickMenuItem={(k) => (k === 'edit' ? actions.edit(d) : actions.remove(d))}>
                  <Menu.Item key="edit">{zh.datasets.edit}</Menu.Item>
                  <Menu.Item key="delete">
                    <span style={{ color: 'var(--c-danger)' }}>{zh.datasets.delete}</span>
                  </Menu.Item>
                </Menu>
              }
            >
              <Button>
                {zh.common.more} <IconDown />
              </Button>
            </Dropdown>
          </Space>
        }
      />
      <div className="card-gap">
        {d.check_state === 'changed' ? <Alert type="warning" content={zh.datasets.changedAlert} /> : null}
        <Card title={zh.datasets.detailBasic}>
          <Descriptions column={2} data={basic} />
        </Card>
        <Card title={zh.datasets.detailPreflight}>
          <Descriptions column={2} data={preflight} />
          <Typography.Title heading={6} style={{ marginTop: 16 }}>
            {zh.datasets.modules}
          </Typography.Title>
          <Table
            rowKey="id"
            size="small"
            pagination={false}
            data={moduleRows}
            data-testid="dataset-modules"
            columns={[
              { title: zh.taskDetail.colModule, dataIndex: 'name', width: 240 },
              {
                title: zh.datasets.colAvailability,
                dataIndex: 'availability',
                width: 130,
                render: (v: string) => <Tag color={v === 'available' ? 'green' : v === 'needs_input' ? 'orange' : 'gray'}>{zh.datasets.availability[v] ?? v}</Tag>,
              },
              { title: zh.datasets.colReason, dataIndex: 'reason', render: (v: string) => <span className="muted">{v || '—'}</span> },
            ]}
          />
        </Card>
        <Card title={zh.datasets.detailChecks}>
          <Table
            rowKey={(c: DatasetCheck) => `${c.at}-${c.trigger}`}
            size="small"
            pagination={false}
            data={d.checks}
            data-testid="dataset-checks"
            columns={[
              { title: zh.datasets.colCheckAt, dataIndex: 'at', width: 130, render: (v: number) => <RelTime ms={v} /> },
              { title: zh.datasets.colTrigger, dataIndex: 'trigger', width: 130, render: (v: string) => zh.datasets.trigger[v] ?? v },
              { title: zh.datasets.colResult, dataIndex: 'result', width: 110, render: (v: string) => <Tag color={v === 'same' ? 'green' : 'orange'}>{zh.datasets.result[v] ?? v}</Tag> },
              {
                title: zh.datasets.colChange,
                dataIndex: 'change',
                render: (_: unknown, c: DatasetCheck) =>
                  c.change ? (
                    <span>
                      {zh.datasets.changeText(c.change.meta_changed, c.change.added, c.change.removed, c.change.modified)}
                      {c.change.sample_keys.length ? <div className="muted mono" style={{ fontSize: 12 }}>{c.change.sample_keys.slice(0, 3).join('，')}</div> : null}
                    </span>
                  ) : (
                    '—'
                  ),
              },
            ]}
          />
        </Card>
        <Card title={zh.datasets.detailTasks} extra={<Link to={`/tasks?dataset_id=${d.id}`}>{zh.datasets.allTasks}</Link>}>
          {!d.tasks.length ? (
            <Typography.Text type="secondary">{zh.datasets.noTasks}</Typography.Text>
          ) : (
            <Table
              rowKey="id"
              size="small"
              pagination={false}
              data={d.tasks}
              columns={[
                { title: zh.taskList.colName, dataIndex: 'name', render: (_: unknown, t: TaskRef) => <Link to={`/tasks/${t.id}`}>{t.name}</Link> },
                { title: zh.taskList.colState, dataIndex: 'state', width: 130, render: (_: unknown, t: TaskRef) => <StateTag state={t.state} size="small" /> },
                { title: zh.taskList.colCreated, dataIndex: 'created_at', width: 130, render: (v: number) => <RelTime ms={v} /> },
              ]}
            />
          )}
        </Card>
      </div>
      {actions.dialogs}
    </div>
  );
}
