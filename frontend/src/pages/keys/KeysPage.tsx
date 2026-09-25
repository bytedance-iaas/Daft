import { Button, Card, Message, Modal, Space, Table, Tabs, Tag } from '@arco-design/web-react';
import type { ColumnProps } from '@arco-design/web-react/es/Table';
import { IconPlus } from '@arco-design/web-react/icon';
import { useQueryClient } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { ApiError, errorMessage } from '../../api/errors';
import { qk, useBackends, useCredentials } from '../../api/queries';
import type { Credential, VerifyResult, VlmBackend } from '../../api/types';
import { PageError } from '../../components/PageError';
import { OneLine } from '../../components/OneLine';
import { PageHeader } from '../../components/PageHeader';
import { regionLabel } from '../../components/RegionSelect';
import { RelTime } from '../../components/RelTime';
import { VerifyTag } from '../../components/VerifyTag';
import { AccessKeyDrawer } from '../../features/keys/AccessKeyDrawer';
import { BackendDrawer } from '../../features/keys/BackendDrawer';
import { zh } from '../../locales/zh';

function verifyMessage(r: VerifyResult): void {
  const text = `${zh.credentials.verifyDone(zh.verify[r.verify_state])}${r.error ? `（${r.error}）` : ''}`;
  if (r.verify_state === 'ok') Message.success(text);
  else Message.warning(text);
}

/** How many tasks use the key, on one line; the unfinished ones named, nothing said when all are done (fourth round). */
function Refs({ c }: { c: Credential }) {
  const active = c.references?.active_tasks ?? 0;
  const total = active + (c.references?.historical_tasks ?? 0);
  if (!total) return <span className="muted">—</span>;
  return (
    <span className="nowrap">
      {zh.credentials.refs(total)}
      {active ? <span className="muted"> · {zh.credentials.refsActive(active)}</span> : null}
    </span>
  );
}

/**
 * 系统和资源配置 (07 §7; the nav item was 密钥与资源 before 2026-09-23): TOS access keys and VLM backends, list + drawer like the VKE secret
 * pages. Only non-secret fields are ever shown; verification states come with their reason, and
 * saving never fails because a verification failed (D30).
 */
export function KeysPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const tab = location.hash === '#vlm' ? 'vlm' : 'keys';
  const keys = useCredentials();
  const backends = useBackends();
  const [keyDrawer, setKeyDrawer] = useState<{ open: boolean; editing: Credential | null }>({ open: false, editing: null });
  const [backendDrawer, setBackendDrawer] = useState<{ open: boolean; editing: VlmBackend | null }>({ open: false, editing: null });
  const [verifying, setVerifying] = useState<string | null>(null);

  const verifyKey = async (c: Credential) => {
    setVerifying(c.id);
    try {
      verifyMessage(await unwrap(api().POST('/credentials/{id}/verify', { params: { path: { id: c.id } } })));
      void qc.invalidateQueries({ queryKey: qk.credentials });
    } catch (e) {
      Message.error(errorMessage(e));
    } finally {
      setVerifying(null);
    }
  };

  const verifyBackend = async (b: VlmBackend) => {
    setVerifying(b.id);
    try {
      verifyMessage(await unwrap(api().POST('/vlm-backends/{id}/verify', { params: { path: { id: b.id } } })));
      void qc.invalidateQueries({ queryKey: qk.backends });
    } catch (e) {
      Message.error(errorMessage(e));
    } finally {
      setVerifying(null);
    }
  };

  /**
   * Delete, the W8 way: a resource that only finished tasks reference answers 409 with
   * details.confirm_required; after a second dialog the DELETE is repeated with confirm=true.
   * A reference by an unfinished task is a plain 409 whose message is shown as is.
   */
  const confirmDelete = (o: { title: string; content: ReactNode; again: string; confirmed: boolean; call: (confirm: boolean) => Promise<unknown>; done: () => void }) => {
    const run = async (confirm: boolean) => {
      try {
        await o.call(confirm);
        Message.success(zh.credentials.deleted);
        o.done();
      } catch (e) {
        if (!confirm && e instanceof ApiError && e.details?.confirm_required === true) {
          Modal.confirm({
            title: o.title,
            content: (
              <div data-testid="delete-confirm-again">
                <p style={{ marginTop: 0 }}>{e.message}</p>
                <p style={{ marginBottom: 0 }}>{o.again}</p>
              </div>
            ),
            okText: zh.credentials.deleteAnyway,
            cancelText: zh.common.cancel,
            okButtonProps: { status: 'danger' },
            onOk: () => run(true),
          });
        } else Message.error(errorMessage(e));
      }
    };
    Modal.confirm({ title: o.title, content: o.content, okText: zh.common.delete, cancelText: zh.common.cancel, okButtonProps: { status: 'danger' }, onOk: () => run(o.confirmed) });
  };

  const deleteKey = (c: Credential) => {
    const active = c.references?.active_tasks ?? 0;
    const historical = c.references?.historical_tasks ?? 0;
    if (active) {
      Modal.info({ title: zh.credentials.deleteKeyTitle(c.name), content: zh.credentials.deleteKeyActive(active), okText: zh.common.ok });
      return;
    }
    confirmDelete({
      title: zh.credentials.deleteKeyTitle(c.name),
      content: historical ? zh.credentials.deleteKeyHistorical(historical) : zh.credentials.deleteKeyPlain,
      again: zh.credentials.deleteAgainKey,
      // The reference counts are on the key, so the first dialog already said what confirm=true means.
      confirmed: historical > 0,
      call: (confirm) => unwrap(api().DELETE('/credentials/{id}', { params: { path: { id: c.id }, query: confirm ? { confirm: true } : {} } })),
      done: () => void qc.invalidateQueries({ queryKey: qk.credentials }),
    });
  };

  // C4 has no reference counts for backends: the Daemon tells (confirm_required) after the first DELETE.
  const deleteBackend = (b: VlmBackend) =>
    confirmDelete({
      title: zh.credentials.deleteBackendTitle(b.name),
      content: zh.credentials.deleteBackendContent,
      again: zh.credentials.deleteAgainBackend,
      confirmed: false,
      call: (confirm) => unwrap(api().DELETE('/vlm-backends/{id}', { params: { path: { id: b.id }, query: confirm ? { confirm: true } : {} } })),
      done: () => void qc.invalidateQueries({ queryKey: qk.backends }),
    });

  // Widths (07 §7): names, regions and key hints read on one line; the verify column has room
  // for the reason a verification failed.
  const keyColumns: ColumnProps<Credential>[] = [
    // one line a row (fourth round): widths measured on the longest values
    { title: zh.credentials.colName, dataIndex: 'name', width: 140, render: (v: string) => <b><OneLine text={v} /></b> },
    { title: zh.credentials.colRegion, dataIndex: 'meta', width: 210, render: (_: unknown, c) => <span className="nowrap">{regionLabel(c.meta.region)}</span> },
    { title: zh.credentials.akid, dataIndex: 'id', width: 120, render: (_: unknown, c) => (c.meta.access_key_id_hint ? <span className="mono nowrap">••••{c.meta.access_key_id_hint}</span> : '—') },
    { title: zh.credentials.colVerify, dataIndex: 'verify_state', width: 230, render: (_: unknown, c) => <VerifyTag state={c.verify_state} error={c.last_verify_error} inline /> },
    { title: zh.credentials.colVerifiedAt, dataIndex: 'last_verified_at', width: 100, render: (v: number | null) => (v ? <span className="nowrap"><RelTime ms={v} /></span> : <span className="muted">—</span>) },
    { title: zh.credentials.colRefs, dataIndex: 'references', width: 190, render: (_: unknown, c) => <Refs c={c} /> },
    { title: zh.credentials.colCreated, dataIndex: 'created_at', width: 90, render: (v: number) => <span className="nowrap"><RelTime ms={v} /></span> },
    {
      title: zh.credentials.colActions,
      dataIndex: 'updated_at',
      fixed: 'right',
      width: 235,
      render: (_: unknown, c) => (
        <Space size={4}>
          <Button type="text" size="small" onClick={() => setKeyDrawer({ open: true, editing: c })}>
            {zh.common.edit}
          </Button>
          <Button type="text" size="small" loading={verifying === c.id} onClick={() => void verifyKey(c)}>
            {zh.credentials.verify}
          </Button>
          <Button type="text" size="small" status="danger" onClick={() => deleteKey(c)}>
            {zh.common.delete}
          </Button>
        </Space>
      ),
    },
  ];

  const backendColumns: ColumnProps<VlmBackend>[] = [
    { title: zh.credentials.colName, dataIndex: 'name', width: 150, render: (v: string) => <b>{v}</b> },
    { title: zh.credentials.colKind, dataIndex: 'kind', width: 100, render: (v: string) => (v === 'ark' ? zh.credentials.kindArk : zh.credentials.kindCustomShort) },
    { title: zh.credentials.colEndpoint, dataIndex: 'endpoint', width: 260, render: (v: string) => <span className="mono">{v}</span> },
    {
      title: zh.credentials.colModels,
      dataIndex: 'models',
      width: 130,
      render: (_: unknown, b) => (
        <span>
          {b.models.length}
          {b.models_listed === false ? (
            <Tag size="small" style={{ marginLeft: 6 }}>
              {zh.credentials.modelSource.manual}
            </Tag>
          ) : null}
        </span>
      ),
    },
    { title: zh.credentials.colConcurrency, dataIndex: 'max_concurrency', width: 100 },
    { title: zh.credentials.colVerify, dataIndex: 'verify_state', width: 200, render: (_: unknown, b) => <VerifyTag state={b.verify_state} error={b.last_verify_error} inline /> },
    { title: zh.credentials.colVerifiedAt, dataIndex: 'last_verified_at', width: 110, render: (v: number | null) => (v ? <RelTime ms={v} /> : <span className="muted">—</span>) },
    {
      title: zh.credentials.colActions,
      dataIndex: 'updated_at',
      fixed: 'right',
      width: 250,
      render: (_: unknown, b) => (
        <Space size={4}>
          <Button type="text" size="small" onClick={() => setBackendDrawer({ open: true, editing: b })}>
            {zh.common.edit}
          </Button>
          <Button type="text" size="small" loading={verifying === b.id} onClick={() => void verifyBackend(b)}>
            {zh.credentials.verify}
          </Button>
          <Button type="text" size="small" status="danger" onClick={() => deleteBackend(b)}>
            {zh.common.delete}
          </Button>
        </Space>
      ),
    },
  ];

  const keyItems = keys.data?.items ?? [];
  const backendItems = backends.data?.items ?? [];
  return (
    <div>
      <PageHeader
        crumbs={[{ label: zh.credentials.navTitle }]}
        title={zh.credentials.title}
        extra={
          tab === 'keys' ? (
            <Button type="primary" icon={<IconPlus />} onClick={() => setKeyDrawer({ open: true, editing: null })}>
              {zh.credentials.newKey}
            </Button>
          ) : (
            <Button type="primary" icon={<IconPlus />} onClick={() => setBackendDrawer({ open: true, editing: null })}>
              {zh.credentials.newBackend}
            </Button>
          )
        }
      />
      <Card>
        <Tabs activeTab={tab} onChange={(k) => navigate({ hash: k === 'vlm' ? '#vlm' : '' })}>
          <Tabs.TabPane key="keys" title={`${zh.credentials.tabKeys}（${keyItems.length}）`}>
            {keys.isError && !keys.data ? (
              <PageError error={keys.error} onRetry={() => void keys.refetch()} />
            ) : (
              <Table rowKey="id" loading={keys.isLoading} columns={keyColumns} data={keyItems} pagination={false} scroll={{ x: 1315 }} data-testid="keys-table" noDataElement={<span className="muted">{zh.credentials.noItems}</span>} />
            )}
          </Tabs.TabPane>
          <Tabs.TabPane key="vlm" title={`${zh.credentials.tabBackends}（${backendItems.length}）`}>
            {backends.isError && !backends.data ? (
              <PageError error={backends.error} onRetry={() => void backends.refetch()} />
            ) : (
              <Table
                rowKey="id"
                loading={backends.isLoading}
                columns={backendColumns}
                data={backendItems}
                pagination={false}
                scroll={{ x: 1300 }}
                data-testid="backends-table"
                noDataElement={<span className="muted">{zh.credentials.noItems}</span>}
              />
            )}
          </Tabs.TabPane>
        </Tabs>
      </Card>
      <AccessKeyDrawer visible={keyDrawer.open} editing={keyDrawer.editing} onClose={() => setKeyDrawer({ open: false, editing: null })} />
      <BackendDrawer visible={backendDrawer.open} editing={backendDrawer.editing} onClose={() => setBackendDrawer({ open: false, editing: null })} />
    </div>
  );
}
