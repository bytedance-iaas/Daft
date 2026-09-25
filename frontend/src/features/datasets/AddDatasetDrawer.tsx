import { Alert, Button, Drawer, Form, Grid, Input, Message, Radio, Select, Space } from '@arco-design/web-react';
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, idempotencyKey, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { qk, useCredentials, useModules, usePublicCatalog } from '../../api/queries';
import type { InputRef } from '../../api/types';
import { RegionSelect } from '../../components/RegionSelect';
import { REGION_RE } from '../../lib/deeplink';
import { zh } from '../../locales/zh';
import { Field } from '../../pages/task-form/Field';
import { PreflightCard } from '../preflight/PreflightCard';
import { usePreflight } from '../preflight/usePreflight';

const { Row, Col } = Grid;
const TOS_URI = /^tos:\/\/[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\/.+/;

/**
 * 添加数据集 (07 §4.4, D36): source, address, region and key → automatic preflight → save,
 * which registers it (preflight + full listing, both fingerprints kept). The same source +
 * address + region registered twice returns the existing one.
 */
export function AddDatasetDrawer({ visible, onClose }: { visible: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const reg = useModules();
  const keys = useCredentials();
  const catalog = usePublicCatalog();
  const [source, setSource] = useState<'tos' | 'public'>('tos');
  const [uri, setUri] = useState('');
  const [publicUri, setPublicUri] = useState('');
  const [region, setRegion] = useState('cn-beijing');
  const [credential, setCredential] = useState('');
  const [name, setName] = useState('');
  const [note, setNote] = useState('');
  const [shown, setShown] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!visible) return;
    setSource('tos');
    setUri('');
    setPublicUri('');
    setName('');
    setNote('');
    setShown(false);
    const list = keys.data?.items ?? [];
    setCredential(list.length === 1 ? list[0].name : '');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible]);

  const input: InputRef | null = useMemo(() => {
    if (source === 'public') return publicUri ? { source: 'public', uri: publicUri } : null;
    const u = uri.trim().replace(/\/+$/, '');
    if (!TOS_URI.test(u) || !REGION_RE.test(region) || !credential) return null;
    return { source: 'tos', uri: u, region, credential };
  }, [source, uri, publicUri, region, credential]);
  const preflight = usePreflight(input && visible ? { input } : null);

  const errors: Record<string, string> = {};
  if (shown) {
    if (source === 'tos') {
      if (!uri.trim()) errors.uri = zh.errors.required(zh.taskForm.datasetUri);
      else if (!TOS_URI.test(uri.trim())) errors.uri = zh.taskForm.datasetUriBad;
      if (!region) errors.region = zh.errors.requiredSelect(zh.taskForm.region);
      if (!credential) errors.credential = zh.errors.requiredSelect(zh.taskForm.credential);
    } else if (!publicUri) errors.publicUri = zh.errors.requiredSelect(zh.taskForm.publicDataset);
  }

  const save = async () => {
    setShown(true);
    if (!input) return;
    if (preflight.status !== 'ok') {
      Message.warning(zh.datasets.saveNeedsPreflight);
      return;
    }
    setBusy(true);
    try {
      const res = await api().POST('/datasets', {
        params: { header: { 'Idempotency-Key': idempotencyKey() } },
        body: { input, ...(name.trim() ? { name: name.trim() } : {}), ...(note.trim() ? { note: note.trim() } : {}) },
      });
      const d = await unwrap(Promise.resolve(res));
      Message.success(res.response.status === 200 ? zh.datasets.existed : zh.datasets.saved);
      void qc.invalidateQueries({ queryKey: qk.datasetsAll });
      void qc.invalidateQueries({ queryKey: qk.overview });
      onClose();
      navigate(`/datasets/${d.id}`);
    } catch (e) {
      Message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer
      width={720}
      title={zh.datasets.drawerTitle}
      visible={visible}
      onCancel={onClose}
      unmountOnExit
      footer={
        <Space>
          <Button onClick={onClose}>{zh.common.cancel}</Button>
          {/* The label stays while saving: the loading state says enough (requester item 17). */}
          <Button type="primary" loading={busy} onClick={() => void save()}>
            {zh.common.save}
          </Button>
        </Space>
      }
    >
      <Alert type="info" content={zh.datasets.drawerIntro} style={{ marginBottom: 16 }} />
      <Form layout="vertical">
        <Field label={zh.taskForm.source} required>
          <Radio.Group type="button" value={source} onChange={setSource} aria-label={zh.taskForm.source}>
            <Radio value="tos">{zh.source.tos}</Radio>
            {catalog.data ? <Radio value="public">{zh.source.public}</Radio> : null}
          </Radio.Group>
        </Field>
        {source === 'tos' ? (
          <>
            <Field label={zh.taskForm.datasetUri} required error={errors.uri}>
              <Input className="mono" value={uri} onChange={setUri} placeholder={zh.taskForm.datasetUriPlaceholderShort} aria-label={zh.taskForm.datasetUri} />
            </Field>
            <Row gutter={16}>
              <Col span={12}>
                <Field label={zh.taskForm.region} required error={errors.region}>
                  <RegionSelect value={region} onChange={setRegion} ariaLabel={zh.taskForm.region} />
                </Field>
              </Col>
              <Col span={12}>
                <Field label={zh.taskForm.credential} required error={errors.credential}>
                  <Select
                    value={credential || undefined}
                    onChange={setCredential}
                    placeholder={zh.taskForm.credentialPlaceholder}
                    aria-label={zh.taskForm.credential}
                    options={(keys.data?.items ?? []).map((c) => ({ label: c.verify_state === 'ok' ? c.name : zh.taskForm.credentialUnverified(c.name, zh.verify[c.verify_state]), value: c.name }))}
                  />
                </Field>
              </Col>
            </Row>
          </>
        ) : (
          <Field label={zh.taskForm.publicDataset} required error={errors.publicUri}>
            <Select
              value={publicUri || undefined}
              onChange={setPublicUri}
              placeholder={zh.taskForm.publicDataset}
              aria-label={zh.taskForm.publicDataset}
              options={(catalog.data ?? []).map((d) => ({ label: d.name, value: d.uri }))}
            />
          </Field>
        )}
        <Row gutter={16}>
          <Col span={12}>
            <Field label={`${zh.datasets.name}（${zh.datasets.nameHelp}）`}>
              <Input value={name} onChange={setName} maxLength={128} aria-label={zh.datasets.name} />
            </Field>
          </Col>
          <Col span={12}>
            <Field label={`${zh.datasets.note}（${zh.common.optional}）`}>
              <Input value={note} onChange={setNote} maxLength={2000} aria-label={zh.datasets.note} />
            </Field>
          </Col>
        </Row>
      </Form>
      <PreflightCard state={preflight} registry={reg.data} onRerun={preflight.rerun} />
    </Drawer>
  );
}
