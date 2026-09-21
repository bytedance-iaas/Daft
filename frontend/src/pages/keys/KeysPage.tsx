import { Alert, Button, Card, Message, Modal, Space, Table, Tabs, Tag } from '@arco-design/web-react';
import type { ColumnProps } from '@arco-design/web-react/es/Table';
import { IconPlus } from '@arco-design/web-react/icon';
import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { qk, useBackends, useCredentials } from '../../api/queries';
import type { Credential, VerifyResult, VlmBackend } from '../../api/types';
import { PageError } from '../../components/PageError';
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

function Refs({ c }: { c: Credential }) {
  const active = c.references?.active_tasks ?? 0;
  const total = active + (c.references?.historical_tasks ?? 0);
  if (!total) return <span className="muted">—</span>;
  return (
    <span>
      {zh.credentials.refs(total)}
      <div className="muted" style={{ fontSize: 12 }}>
        {active ? zh.credentials.refsActive(active) : zh.credentials.refsAllDone}
      </div>
    </span>
  );
}

/**
 * 密钥与资源管理 (07 §7): TOS access keys and VLM backends, list + drawer like the VKE secret
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

  const deleteKey = (c: Credential) => {
    const active = c.references?.active_tasks ?? 0;
    const historical = c.references?.historical_tasks ?? 0;
    if (active) {
      Modal.info({ title: zh.credentials.deleteKeyTitle(c.name), content: zh.credentials.deleteKeyActive(active), okText: zh.common.ok });
      return;
    }
    Modal.confirm({
      title: zh.credentials.deleteKeyTitle(c.name),
      content: historical ? zh.credentials.deleteKeyHistorical(historical) : zh.credentials.deleteKeyPlain,
      okText: zh.common.delete,
      cancelText: zh.common.cancel,
      okButtonProps: { status: 'danger' },
      onOk: async () => {
        try {
          // Keys referenced only by finished tasks need confirm=true: the dialog above is that confirmation.
          await unwrap(api().DELETE('/credentials/{id}', { params: { path: { id: c.id }, query: historical ? { confirm: true } : {} } }));
          Message.success(zh.credentials.deleted);
          void qc.invalidateQueries({ queryKey: qk.credentials });
        } catch (e) {
          Message.error(errorMessage(e));
        }
      },
    });
  };

  const deleteBackend = (b: VlmBackend) =>
    Modal.confirm({
      title: zh.credentials.deleteBackendTitle(b.name),
      content: zh.credentials.deleteBackendContent,
      okText: zh.common.delete,
      cancelText: zh.common.cancel,
      okButtonProps: { status: 'danger' },
      onOk: async () => {
        try {
          // C4 has no reference counts for backends, so the dialog always counts as the confirmation.
          await unwrap(api().DELETE('/vlm-backends/{id}', { params: { path: { id: b.id }, query: { confirm: true } } }));
          Message.success(zh.credentials.deleted);
          void qc.invalidateQueries({ queryKey: qk.backends });
        } catch (e) {
          Message.error(errorMessage(e));
        }
      },
    });

  const keyColumns: ColumnProps<Credential>[] = [
    { title: zh.credentials.colName, dataIndex: 'name', render: (v: string) => <b>{v}</b> },
    { title: zh.credentials.colRegion, dataIndex: 'meta', render: (_: unknown, c) => regionLabel(c.meta.region) },
    { title: zh.credentials.akid, dataIndex: 'id', render: (_: unknown, c) => (c.meta.access_key_id_hint ? <span className="mono">••••{c.meta.access_key_id_hint}</span> : '—') },
    { title: zh.credentials.colVerify, dataIndex: 'verify_state', render: (_: unknown, c) => <VerifyTag state={c.verify_state} error={c.last_verify_error} /> },
    { title: zh.credentials.colVerifiedAt, dataIndex: 'last_verified_at', render: (v: number | null) => (v ? <RelTime ms={v} /> : <span className="muted">—</span>) },
    { title: zh.credentials.colRefs, dataIndex: 'references', render: (_: unknown, c) => <Refs c={c} /> },
    { title: zh.credentials.colCreated, dataIndex: 'created_at', render: (v: number) => <RelTime ms={v} /> },
    {
      title: zh.credentials.colActions,
      dataIndex: 'updated_at',
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
    { title: zh.credentials.colName, dataIndex: 'name', render: (v: string) => <b>{v}</b> },
    { title: zh.credentials.colKind, dataIndex: 'kind', render: (v: string) => (v === 'ark' ? zh.credentials.kindArk : zh.credentials.kindCustomShort) },
    { title: zh.credentials.colEndpoint, dataIndex: 'endpoint', render: (v: string) => <span className="mono">{v}</span> },
    {
      title: zh.credentials.colModels,
      dataIndex: 'models',
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
    { title: zh.credentials.colConcurrency, dataIndex: 'max_concurrency' },
    { title: zh.credentials.colVerify, dataIndex: 'verify_state', render: (_: unknown, b) => <VerifyTag state={b.verify_state} error={b.last_verify_error} /> },
    { title: zh.credentials.colVerifiedAt, dataIndex: 'last_verified_at', render: (v: number | null) => (v ? <RelTime ms={v} /> : <span className="muted">—</span>) },
    {
      title: zh.credentials.colActions,
      dataIndex: 'updated_at',
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
        description={zh.credentials.desc}
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
            <Alert type="info" content={zh.credentials.keysIntro} style={{ marginBottom: 12 }} />
            {keys.isError && !keys.data ? (
              <PageError error={keys.error} onRetry={() => void keys.refetch()} />
            ) : (
              <Table rowKey="id" loading={keys.isLoading} columns={keyColumns} data={keyItems} pagination={false} data-testid="keys-table" noDataElement={<span className="muted">{zh.credentials.noItems}</span>} />
            )}
          </Tabs.TabPane>
          <Tabs.TabPane key="vlm" title={`${zh.credentials.tabBackends}（${backendItems.length}）`}>
            <Alert type="info" content={zh.credentials.backendsIntro} style={{ marginBottom: 12 }} />
            {backends.isError && !backends.data ? (
              <PageError error={backends.error} onRetry={() => void backends.refetch()} />
            ) : (
              <Table
                rowKey="id"
                loading={backends.isLoading}
                columns={backendColumns}
                data={backendItems}
                pagination={false}
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
