import { Alert, Button, Drawer, Form, Grid, Input, Message, Radio, Select, Space, Tag } from '@arco-design/web-react';
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, idempotencyKey, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { qk, useCredentials, useModules, usePublicCatalog } from '../../api/queries';
import type { InputRef, Upload, VizMapping } from '../../api/types';
import { RegionSelect } from '../../components/RegionSelect';
import { REGION_RE } from '../../lib/deeplink';
import { zh } from '../../locales/zh';
import { Field } from '../../pages/task-form/Field';
import { PreflightCard } from '../preflight/PreflightCard';
import { usePreflight } from '../preflight/usePreflight';
import { AnnotationsField } from './Annotations';
import { McapConfig, type McapConfigStatus } from './McapConfig';

const { Row, Col } = Grid;
const TOS_URI = /^tos:\/\/[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\/.+/;

/**
 * 添加数据集 (07 §4.4, D36): source, address, region and key → automatic preflight → save,
 * which registers it (preflight + full listing, both fingerprints kept). The same source +
 * address + region registered twice returns the existing one. An mcap dataset gets the 「mcap 配置」
 * section and its mapping is confirmed with the registration; any dataset may carry an external
 * annotation file (design doc 18 §4.5, §6.4).
 */
export function AddDatasetDrawer({ visible, onClose }: { visible: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const reg = useModules();
  const keys = useCredentials();
  const catalog = usePublicCatalog();
  const localEnabled = Boolean(window.__CURATOR_FEATURES__?.local_input);
  const [source, setSource] = useState<'tos' | 'public' | 'local'>('tos');
  const [uri, setUri] = useState('');
  const [publicUri, setPublicUri] = useState('');
  const [region, setRegion] = useState('cn-beijing');
  const [credential, setCredential] = useState('');
  const [name, setName] = useState('');
  const [note, setNote] = useState('');
  const [shown, setShown] = useState(false);
  const [busy, setBusy] = useState(false);
  const [annotations, setAnnotations] = useState<Upload | null>(null);
  const [uploading, setUploading] = useState(false);
  const [mapping, setMapping] = useState<VizMapping | null>(null);
  const [mcapStatus, setMcapStatus] = useState<McapConfigStatus>('probing');
  useEffect(() => {
    if (!visible) return;
    setSource('tos');
    setUri('');
    setPublicUri('');
    setName('');
    setNote('');
    setShown(false);
    setAnnotations(null);
    setMapping(null);
    // the default key (C4 1.17), else the only one
    const list = keys.data?.items ?? [];
    setCredential(list.find((c) => c.is_default)?.name ?? (list.length === 1 ? list[0].name : ''));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible]);

  const input: InputRef | null = useMemo(() => {
    if (source === 'public') return publicUri ? { source: 'public', uri: publicUri } : null;
    const u = uri.trim().replace(/\/+$/, '');
    if (source === 'local') return u ? { source: 'local', uri: u } : null;
    if (!TOS_URI.test(u) || !REGION_RE.test(region) || !credential) return null;
    return { source: 'tos', uri: u, region, credential };
  }, [source, uri, publicUri, region, credential]);
  const preflight = usePreflight(input && visible ? { input } : null);
  // episode_N.mcap files make an mcap dataset even when the checks cannot read them with the site's
  // default topics (ABC-130k): the mapping is what makes it readable (design doc 18 §6.4)
  const mcap = preflight.status === 'ok' && preflight.result?.format.kind === 'mcap' && input !== null;
  const inputKey = JSON.stringify(input);
  useEffect(() => setMapping(null), [inputKey]);

  const errors: Record<string, string> = {};
  if (shown) {
    if (source === 'local') {
      if (!uri.trim()) errors.uri = zh.errors.required(zh.taskForm.localPath);
    } else if (source === 'tos') {
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
    if (uploading || (mcap && mcapStatus === 'probing')) return;
    setBusy(true);
    try {
      const res = await api().POST('/datasets', {
        params: { header: { 'Idempotency-Key': idempotencyKey() } },
        body: {
          input,
          ...(name.trim() ? { name: name.trim() } : {}),
          ...(note.trim() ? { note: note.trim() } : {}),
          ...(mcap && mapping ? { viz_mapping: mapping } : {}),
          ...(annotations ? { annotations_upload: annotations.upload_id } : {}),
        },
      });
      const d = await unwrap(Promise.resolve(res));
      Message.success(res.response.status === 200 ? zh.datasets.existed : zh.datasets.saved);
      void qc.invalidateQueries({ queryKey: qk.datasetsAll });
      void qc.invalidateQueries({ queryKey: qk.overview });
      void qc.invalidateQueries({ queryKey: ['viz'] });
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
      width={mcap ? 1040 : 720}
      title={zh.datasets.drawerTitle}
      visible={visible}
      onCancel={onClose}
      unmountOnExit
      footer={
        <Space>
          {mcap ? <span className="muted" style={{ fontSize: 12 }}>{mapping ? zh.mcap.willConfirm : mcapStatus === 'failed' ? zh.mcap.pendingSave : zh.mcap.probing}</span> : null}
          <Button onClick={onClose}>{zh.common.cancel}</Button>
          {/* The label stays while saving: the loading state says enough (requester item 17). */}
          <Button type="primary" loading={busy || uploading || (mcap && mcapStatus === 'probing')} onClick={() => void save()}>
            {zh.common.save}
          </Button>
        </Space>
      }
    >
      <Form layout="vertical">
        <Field label={zh.taskForm.source} required>
          <Radio.Group type="button" value={source} onChange={setSource} aria-label={zh.taskForm.source}>
            <Radio value="tos">{zh.source.tos}</Radio>
            {catalog.data ? <Radio value="public">{zh.source.public}</Radio> : null}
            {localEnabled ? (
              <Radio value="local">
                {zh.source.local} <Tag size="small" color="orange">{zh.source.experimental}</Tag>
              </Radio>
            ) : null}
          </Radio.Group>
        </Field>
        {source === 'local' ? (
          <Field label={zh.taskForm.localPath} required error={errors.uri} extra={zh.taskForm.localPathHelp}>
            <Input className="mono" value={uri} onChange={setUri} placeholder="/data/datasets/xxx" aria-label={zh.taskForm.localPath} />
          </Field>
        ) : source === 'tos' ? (
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
        <Field label={`${zh.annotations.label}（${zh.annotations.help}）`}>
          <AnnotationsField value={annotations} onChange={setAnnotations} onBusy={setUploading} />
        </Field>
      </Form>
      <PreflightCard state={preflight} registry={reg.data} onRerun={preflight.rerun} />
      {mcap && preflight.result?.format.supported === false ? <Alert type="info" style={{ marginTop: 12 }} content={zh.mcap.defaultsUnreadable} /> : null}
      {mcap ? <McapConfig key={inputKey} input={input} value={mapping} onChange={setMapping} onStatus={setMcapStatus} /> : null}
    </Drawer>
  );
}
