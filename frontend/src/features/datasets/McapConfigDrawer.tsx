import { Alert, Button, Drawer, Message, Space, Spin } from '@arco-design/web-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api, idempotencyKey, unwrap } from '../../api/client';
import { ApiError, errorMessage } from '../../api/errors';
import { qk } from '../../api/queries';
import type { DatasetItem, VizMapping } from '../../api/types';
import { PageError } from '../../components/PageError';
import { RelTime } from '../../components/RelTime';
import { zh } from '../../locales/zh';
import { McapConfig, type McapConfigStatus } from './McapConfig';

export const mappingKey = (id: string) => ['dataset', id, 'mapping'] as const;

/**
 * 「mcap 配置」 of a registered dataset (design doc 18 §6.4 step 5–6): the confirmed mapping (or, never
 * confirmed, the probe's draft) in the table; saving confirms a new version (PUT /datasets/{id}/mapping,
 * checked against the dataset's topics). Tasks already started keep the version frozen in run.json.
 */
export function McapConfigDrawer({ dataset, onClose }: { dataset: Pick<DatasetItem, 'id' | 'name'> | null; onClose: () => void }) {
  return dataset ? <Opened key={dataset.id} dataset={dataset} onClose={onClose} /> : null;
}

function Opened({ dataset, onClose }: { dataset: Pick<DatasetItem, 'id' | 'name'>; onClose: () => void }) {
  const qc = useQueryClient();
  const id = dataset.id;
  const doc = useQuery({ queryKey: mappingKey(id), queryFn: () => unwrap(api().GET('/datasets/{id}/mapping', { params: { path: { id } } })) });
  const [value, setValue] = useState<VizMapping | null>(null);
  const [status, setStatus] = useState<McapConfigStatus>('probing');
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<{ field: string; problem: string }[]>([]);
  const d = doc.data;
  // confirming the version already confirmed adds nothing
  const unchanged = Boolean(d?.mapping && value && JSON.stringify(d.mapping) === JSON.stringify(value));
  const shown = useMemo(() => (d?.warnings ?? []).map((w) => w.message), [d?.warnings]);
  const save = async () => {
    if (!value) return;
    setBusy(true);
    setProblems([]);
    try {
      const saved = await unwrap(api().PUT('/datasets/{id}/mapping', { params: { path: { id }, header: { 'Idempotency-Key': idempotencyKey() } }, body: { mapping: value } }));
      Message.success(zh.mcap.savedVersion(saved.version));
      qc.setQueryData(mappingKey(id), saved);
      void qc.invalidateQueries({ queryKey: qk.dataset(id) });
      void qc.invalidateQueries({ queryKey: qk.datasetsAll });
      void qc.invalidateQueries({ queryKey: ['viz'] });
      onClose();
    } catch (e) {
      const details = e instanceof ApiError ? (e.details as { errors?: { field: string; problem: string }[] } | undefined) : undefined;
      setProblems(details?.errors ?? []);
      Message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Drawer
      width={1040}
      title={zh.mcap.drawerTitle(dataset.name)}
      visible
      onCancel={onClose}
      footer={
        <Space>
          <span className="muted" style={{ fontSize: 12 }}>
            {zh.mcap.freezeHint}
          </span>
          <Button onClick={onClose}>{zh.common.cancel}</Button>
          <Button
            type="primary"
            loading={busy || (Boolean(d) && status === 'probing')}
            disabled={!value || status === 'failed' || unchanged}
            title={unchanged ? zh.mcap.unchanged : undefined}
            onClick={() => void save()}
          >
            {zh.mcap.saveVersion((d?.version ?? 0) + 1)}
          </Button>
        </Space>
      }
    >
      {doc.isError && !d ? (
        <PageError error={doc.error} onRetry={() => void doc.refetch()} />
      ) : !d ? (
        <Spin style={{ display: 'block', margin: '60px auto' }} />
      ) : (
        <div data-testid="mcap-drawer">
          <div style={{ marginBottom: 8 }} data-testid="mcap-current">
            {d.state === 'confirmed' ? (
              <span>
                {zh.mcap.current(d.version, d.mapping?.name ?? null)}
                {d.updated_at ? (
                  <span className="muted">
                    {' · '}
                    <RelTime ms={d.updated_at} />
                  </span>
                ) : null}
              </span>
            ) : (
              <span style={{ color: 'var(--c-warning)' }}>{zh.mcap.none}</span>
            )}
          </div>
          {d.warnings.map((w, i) => (
            <Alert key={`${w.code}-${i}`} type="warning" style={{ marginBottom: 8 }} content={w.message} />
          ))}
          {problems.length > 1 ? (
            <Alert
              type="error"
              style={{ marginBottom: 8 }}
              data-testid="mcap-save-problems"
              content={
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {problems.slice(0, 10).map((p, i) => (
                    <li key={i}>
                      <span className="mono">{p.field}</span>：{p.problem}
                    </li>
                  ))}
                </ul>
              }
            />
          ) : null}
          <McapConfig
            input={{ dataset_id: id }}
            saved={d.state === 'confirmed' && d.mapping ? { mapping: d.mapping, version: d.version } : null}
            value={value}
            onChange={setValue}
            onStatus={setStatus}
            shownWarnings={shown}
          />
          {d.check_mapping ? (
            <details className="mcap-json">
              <summary>{zh.mcap.checkMapping}</summary>
              <pre data-testid="mcap-check-mapping">{JSON.stringify(d.check_mapping, null, 2)}</pre>
            </details>
          ) : null}
        </div>
      )}
    </Drawer>
  );
}
