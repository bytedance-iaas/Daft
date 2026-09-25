import { Button, Divider, Drawer, Form, Input, InputNumber, Message, Popconfirm, Radio, Select, Space, Table, Tag, Typography } from '@arco-design/web-react';
import { IconSearch } from '@arco-design/web-react/icon';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { api, unwrap } from '../../api/client';
import { errorMessage, isApiError } from '../../api/errors';
import { qk, useBackends } from '../../api/queries';
import type { ReasoningLevel, VlmBackend, VlmBackendCreate, VlmBackendUpdate, VlmModel } from '../../api/types';
import { VerifyTag } from '../../components/VerifyTag';
import { fuzzyFilter } from '../../lib/fuzzy';
import { zh } from '../../locales/zh';

/** Models per page in the drawer: a backend can list a hundred of them (07 §7). */
export const MODELS_PAGE_SIZE = 10;

const ALL_LEVELS: ReasoningLevel[] = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];

/** Effective levels for a model's dropdown (08 §4.1): the mapping's levels, or all 7 when unknown. */
export function effortOptions(model: Pick<VlmModel, 'capabilities'> | undefined, kind: VlmBackend['kind'] | undefined): { label: string; value: string }[] {
  const levels = model?.capabilities.reasoning_effort_levels?.length ? model.capabilities.reasoning_effort_levels : ALL_LEVELS;
  return [
    { label: kind === 'custom' ? zh.taskForm.effortNotSent : zh.taskForm.effortDefault, value: '' },
    ...levels.map((l) => ({ label: l === 'minimal' && kind === 'ark' ? zh.taskForm.effortMinimal : l, value: l })),
  ];
}

export function knowsLevels(model: Pick<VlmModel, 'capabilities'> | undefined): boolean {
  const levels = model?.capabilities.reasoning_effort_levels;
  return Boolean(levels && levels.length && levels.length < ALL_LEVELS.length);
}

interface Values {
  kind: 'ark' | 'custom';
  name: string;
  endpoint: string;
  api_key: string;
  max_concurrency: number | undefined;
}

/**
 * The models of a saved backend (07 §7, requester item 9). Above the table, one box whose job
 * depends on the backend: when GET /models listed its models, it filters them as you type (fuzzy,
 * query only); when it could not, it takes a model ID to add (checked with a minimal request).
 * Ten models a page; 设为默认 / 取消默认 and 移除 sit together in one operations column.
 */
function Models({ backend }: { backend: VlmBackend }) {
  const qc = useQueryClient();
  const [filter, setFilter] = useState('');
  const [manual, setManual] = useState('');
  const [manualError, setManualError] = useState('');
  const [page, setPage] = useState(1);
  const refresh = () => qc.invalidateQueries({ queryKey: qk.backends });
  const patch = useMutation({
    mutationFn: (p: { id: string; body: { reasoning_effort?: ReasoningLevel | null; max_concurrency?: number | null; is_default?: boolean } }) =>
      unwrap(api().PATCH('/vlm-backends/{id}/models/{model_id}', { params: { path: { id: backend.id, model_id: p.id } }, body: p.body })),
    onSuccess: () => {
      Message.success(zh.credentials.modelSaved);
      void refresh();
    },
    onError: (e) => Message.error(errorMessage(e)),
  });
  // Every backend's models are refetched: the owner has one default across all of them (C4 1.6.0).
  const setDefault = useMutation({
    mutationFn: (p: { id: string; on: boolean }) =>
      unwrap(api().PATCH('/vlm-backends/{id}/models/{model_id}', { params: { path: { id: backend.id, model_id: p.id } }, body: { is_default: p.on } })),
    onSuccess: (m) => {
      Message.success(m.is_default ? zh.credentials.defaultModelSet(m.model_name) : zh.credentials.defaultModelCleared);
      void refresh();
    },
    onError: (e) => Message.error(errorMessage(e)),
  });
  const remove = useMutation({
    mutationFn: (id: string) => unwrap(api().DELETE('/vlm-backends/{id}/models/{model_id}', { params: { path: { id: backend.id, model_id: id } } })),
    onSuccess: () => {
      Message.success(zh.credentials.modelRemoved);
      void refresh();
    },
    onError: (e) => Message.error(errorMessage(e)),
  });
  const add = useMutation({
    mutationFn: (name: string) => unwrap(api().POST('/vlm-backends/{id}/models', { params: { path: { id: backend.id } }, body: { model_name: name } })),
    onSuccess: () => {
      Message.success(zh.credentials.modelAdded);
      setManual('');
      setManualError('');
      void refresh();
    },
    onError: (e) => setManualError(errorMessage(e)),
  });
  const relist = useMutation({
    mutationFn: () => unwrap(api().POST('/vlm-backends/{id}/refresh-models', { params: { path: { id: backend.id } } })),
    onSuccess: (r) => {
      if (r.listed) Message.success(zh.credentials.refreshed(r.models.length));
      else Message.info(zh.credentials.refreshFailed(r.note ?? ''));
      void refresh();
    },
    onError: (e) => Message.error(errorMessage(e)),
  });
  const listed = backend.models_listed;
  const unknownLevels = backend.models.some((m) => !knowsLevels(m));
  const query = listed ? filter.trim() : '';
  const shown = useMemo(() => fuzzyFilter(backend.models, query, (m) => m.model_name), [backend.models, query]);
  const newId = manual.trim();
  const known = backend.models.some((m) => m.model_name === newId);
  const lastPage = Math.max(1, Math.ceil(shown.length / MODELS_PAGE_SIZE));
  const empty = listed ? (query ? zh.credentials.modelFilterNone : zh.credentials.modelsNoneListed) : zh.credentials.modelsNoneManual;
  return (
    <div data-testid="backend-models">
      <Space style={{ justifyContent: 'space-between', width: '100%', marginBottom: 8 }}>
        <Typography.Text type="secondary">{listed ? zh.credentials.modelsListed(backend.models.length) : zh.credentials.modelsNotListed}</Typography.Text>
        <Button size="small" loading={relist.isPending} onClick={() => relist.mutate()}>
          {zh.credentials.refreshModels}
        </Button>
      </Space>
      <div style={{ marginBottom: 8 }} data-testid={listed ? 'model-filter' : 'model-add'}>
        {listed ? (
          <>
            <Input
              style={{ width: 380 }}
              value={filter}
              allowClear
              prefix={<IconSearch />}
              onChange={(v) => {
                setFilter(v);
                setPage(1);
              }}
              placeholder={zh.credentials.modelFilterPlaceholder}
              aria-label={zh.credentials.modelFilterPlaceholder}
            />
            {query ? <div className="field-note">{zh.credentials.modelFilterHint(shown.length, backend.models.length)}</div> : null}
          </>
        ) : (
          <Space align="start">
            <div>
              <Input
                style={{ width: 380 }}
                value={manual}
                allowClear
                onChange={(v) => {
                  setManual(v);
                  setManualError('');
                }}
                onPressEnter={() => newId && !known && add.mutate(newId)}
                placeholder={zh.credentials.modelIdPlaceholder}
                aria-label={zh.credentials.modelIdPlaceholder}
                status={manualError ? 'error' : undefined}
              />
              {manualError ? <div className="field-note-error">{manualError}</div> : null}
              {newId && known && !manualError ? <div className="field-note">{zh.credentials.modelAlreadyThere}</div> : null}
            </div>
            <Button type="primary" loading={add.isPending} disabled={!newId || known} onClick={() => add.mutate(newId)}>
              {zh.credentials.addModel}
            </Button>
          </Space>
        )}
      </div>
      <Table
        rowKey="id"
        size="small"
        data={shown}
        scroll={{ x: 820 }}
        pagination={
          shown.length > MODELS_PAGE_SIZE
            ? { current: Math.min(page, lastPage), pageSize: MODELS_PAGE_SIZE, total: shown.length, size: 'mini', showTotal: (total: number) => zh.common.total(total), onChange: (p: number) => setPage(p) }
            : false
        }
        noDataElement={<span className="muted">{empty}</span>}
        columns={[
          { title: zh.credentials.modelColName, dataIndex: 'model_name', width: 240, render: (v: string) => <span className="mono">{v}</span> },
          { title: zh.credentials.modelColSource, dataIndex: 'source', width: 100, render: (v: string) => <Tag size="small">{zh.credentials.modelSource[v] ?? v}</Tag> },
          {
            title: zh.credentials.modelColEffort,
            dataIndex: 'reasoning_effort',
            width: 170,
            render: (_: unknown, m: VlmModel) => (
              <Select
                size="small"
                style={{ width: 160 }}
                value={m.reasoning_effort ?? ''}
                aria-label={`${m.model_name} ${zh.credentials.modelColEffort}`}
                options={effortOptions(m, backend.kind)}
                onChange={(v: string) => patch.mutate({ id: m.id, body: { reasoning_effort: (v || null) as ReasoningLevel | null } })}
              />
            ),
          },
          {
            title: zh.credentials.modelColConcurrency,
            dataIndex: 'max_concurrency',
            width: 110,
            render: (_: unknown, m: VlmModel) => (
              <InputNumber
                size="small"
                style={{ width: 100 }}
                min={1}
                precision={0}
                defaultValue={m.max_concurrency ?? undefined}
                placeholder={`${zh.credentials.modelConcurrencyPlaceholder} ${backend.max_concurrency}`}
                onBlur={(e) => {
                  const raw = (e.target as HTMLInputElement).value;
                  const n = raw ? Number(raw) : null;
                  if (n !== (m.max_concurrency ?? null)) patch.mutate({ id: m.id, body: { max_concurrency: n } });
                }}
              />
            ),
          },
          {
            title: zh.credentials.colActions,
            dataIndex: 'id',
            width: 200,
            render: (_: unknown, m: VlmModel) => (
              <Space size={4} data-testid={`model-ops-${m.id}`}>
                {m.is_default ? (
                  <>
                    <Tag size="small" color="arcoblue">
                      {zh.credentials.modelDefault}
                    </Tag>
                    <Button size="mini" type="text" loading={setDefault.isPending} onClick={() => setDefault.mutate({ id: m.id, on: false })}>
                      {zh.credentials.clearDefaultModel}
                    </Button>
                  </>
                ) : (
                  <Button size="mini" type="text" loading={setDefault.isPending} onClick={() => setDefault.mutate({ id: m.id, on: true })}>
                    {zh.credentials.setDefaultModel}
                  </Button>
                )}
                <Popconfirm title={`${zh.credentials.removeModel}「${m.model_name}」？`} onOk={() => remove.mutate(m.id)}>
                  <Button size="mini" type="text" status="danger">
                    {zh.credentials.removeModel}
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      {backend.kind === 'custom' ? <div className="field-note">{zh.credentials.customEffortNote}</div> : null}
      {unknownLevels ? <div className="field-note">{zh.credentials.unknownLevelsNote}</div> : null}
    </div>
  );
}

/**
 * Add or edit a VLM backend (07 §7, 08 §4): after saving, the models GET /models could list are
 * shown; otherwise models are entered by hand (Model ID or ep-…). Secrets are write-only.
 */
export function BackendDrawer({
  visible,
  editing,
  onClose,
  onSaved,
}: {
  visible: boolean;
  editing: VlmBackend | null;
  onClose: () => void;
  onSaved?: (b: VlmBackend) => void;
}) {
  const qc = useQueryClient();
  const backends = useBackends();
  const [form] = Form.useForm<Values>();
  const [current, setCurrent] = useState<VlmBackend | null>(editing);
  const [kind, setKind] = useState<'ark' | 'custom'>(editing?.kind ?? 'ark');
  useEffect(() => {
    if (!visible) return;
    setCurrent(editing);
    setKind(editing?.kind ?? 'ark');
    form.resetFields();
    form.setFieldsValue({
      kind: editing?.kind ?? 'ark',
      name: editing?.name ?? '',
      endpoint: editing?.endpoint ?? (editing ? '' : 'https://ark.cn-beijing.volces.com/api/v3'),
      api_key: '',
      max_concurrency: editing?.max_concurrency ?? 64,
    });
  }, [visible, editing, form]);
  const fresh = current ? backends.data?.items.find((b) => b.id === current.id) ?? current : null;

  const save = useMutation({
    mutationFn: async (v: Values) => {
      if (current) {
        const body: VlmBackendUpdate = {
          endpoint: v.endpoint.trim(),
          ...(v.api_key ? { api_key: v.api_key } : {}),
          ...(v.max_concurrency ? { max_concurrency: v.max_concurrency } : {}),
        };
        return unwrap(api().PUT('/vlm-backends/{id}', { params: { path: { id: current.id } }, body }));
      }
      const body: VlmBackendCreate = {
        name: v.name.trim(),
        kind: v.kind,
        endpoint: v.endpoint.trim(),
        ...(v.api_key ? { api_key: v.api_key } : {}),
        ...(v.max_concurrency ? { max_concurrency: v.max_concurrency } : {}),
      };
      return unwrap(api().POST('/vlm-backends', { body }));
    },
    onSuccess: (b) => {
      Message.success(b.verify_state === 'ok' ? zh.credentials.savedOk : zh.credentials.savedFailed(b.last_verify_error ?? ''));
      form.setFieldsValue({ api_key: '' });
      qc.setQueryData(qk.backends, (old: { items: VlmBackend[] } | undefined) => ({
        items: old ? [...old.items.filter((x) => x.id !== b.id), b] : [b],
      }));
      void qc.invalidateQueries({ queryKey: qk.backends });
      setCurrent(b);
      onSaved?.(b);
    },
    onError: (e) => {
      if (isApiError(e, 'name_taken')) form.setFields({ name: { error: { message: e.message } } });
      else Message.error(errorMessage(e));
    },
  });

  return (
    <Drawer
      width={880}
      title={current ? zh.credentials.editBackend : zh.credentials.newBackend}
      visible={visible}
      onCancel={onClose}
      unmountOnExit
      footer={
        <Space>
          <Button onClick={onClose}>{zh.common.close}</Button>
          <Button
            type="primary"
            loading={save.isPending}
            onClick={async () => {
              const v = await form.validate().catch(() => null);
              if (v) save.mutate(v);
            }}
          >
            {zh.common.save}
          </Button>
        </Space>
      }
    >
      <Form form={form} layout="vertical" autoComplete="off" onValuesChange={(c: Partial<Values>) => c.kind && setKind(c.kind)}>
        <Form.Item label={zh.credentials.kind} field="kind" rules={[{ required: true, message: zh.errors.requiredSelect(zh.credentials.kind) }]}>
          <Radio.Group type="button" disabled={Boolean(current)}>
            <Radio value="ark">{zh.credentials.kindArk}</Radio>
            <Radio value="custom">{zh.credentials.kindCustom}</Radio>
          </Radio.Group>
        </Form.Item>
        <Form.Item label={zh.credentials.name} field="name" rules={current ? [] : [{ required: true, message: zh.errors.required(zh.credentials.name) }, { maxLength: 64, message: zh.errors.maxLength(zh.credentials.name, 64) }]} extra={zh.credentials.nameHelp}>
          <Input placeholder={zh.credentials.namePlaceholder} disabled={Boolean(current)} />
        </Form.Item>
        <Form.Item label={zh.credentials.endpoint} field="endpoint" rules={[{ required: true, message: zh.errors.required(zh.credentials.endpoint) }]}>
          <Input placeholder="https://ark.cn-beijing.volces.com/api/v3" />
        </Form.Item>
        <Form.Item
          label={kind === 'custom' ? `${zh.credentials.apiKey}（${zh.credentials.apiKeyOptional}）` : zh.credentials.apiKey}
          field="api_key"
          rules={kind === 'ark' && !current ? [{ required: true, message: zh.errors.required(zh.credentials.apiKey) }] : []}
          extra={zh.credentials.skHelp}
        >
          <Input.Password placeholder={current?.has_api_key ? zh.credentials.keepSecret : zh.credentials.apiKeyPlaceholder} autoComplete="new-password" />
        </Form.Item>
        <Form.Item label={zh.credentials.backendConcurrency} field="max_concurrency" extra={zh.credentials.backendConcurrencyHelp}>
          <InputNumber min={1} precision={0} style={{ width: 160 }} />
        </Form.Item>
      </Form>
      {fresh ? (
        <>
          <div style={{ marginBottom: 8 }}>
            {zh.credentials.currentVerify}：<VerifyTag state={fresh.verify_state} error={fresh.last_verify_error} />
          </div>
          <Divider orientation="left">{zh.credentials.models}</Divider>
          <Models backend={fresh} />
        </>
      ) : (
        <Typography.Text type="secondary">{zh.credentials.saveBackendFirst}</Typography.Text>
      )}
    </Drawer>
  );
}
