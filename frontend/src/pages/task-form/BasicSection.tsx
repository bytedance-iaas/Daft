import { AutoComplete, Button, Card, Grid, Input, Radio, Select, Space, Tag, Typography } from '@arco-design/web-react';
import { Link } from 'react-router-dom';
import type { BrowsedDataset, Credential, DatasetItem } from '../../api/types';
import { RegionSelect } from '../../components/RegionSelect';
import { shortTime } from '../../lib/format';
import { zh } from '../../locales/zh';
import { Field } from './Field';
import type { Errors, FormValues } from './formModel';
import type { ProbeState } from '../../features/preflight/usePreflight';

const { Row, Col } = Grid;

export interface BasicSectionProps {
  v: FormValues;
  set: (patch: Partial<FormValues>, touched?: keyof FormValues) => void;
  errors: Errors;
  batch: boolean;
  batchList?: React.ReactNode;
  credentials: Credential[];
  registered: DatasetItem[];
  publicCatalog: BrowsedDataset[] | null;
  localEnabled: boolean;
  probe: ProbeState;
  onOutputBlur: () => void;
  onPickRegistered: (d: DatasetItem) => void;
  onAddCredential: () => void;
  linkNotes: { dataset: string[]; region: string[] };
  outputNote: string;
  sourceLocked?: boolean;
}

/**
 * The keys to choose from: verified ones first, then the rest with their state; a key whose
 * verification failed cannot be picked. `skip` leaves out the one the fixed first entry stands
 * for, unless it is the value.
 */
function credentialOptions(list: Credential[], skip?: string, value?: string) {
  const rank = (c: Credential) => (c.verify_state === 'ok' ? 0 : c.verify_state === 'failed' ? 2 : 1);
  return [...list]
    .filter((c) => c.name !== skip || c.name === value)
    .sort((a, b) => rank(a) - rank(b))
    .map((c) => ({
      label: c.verify_state === 'ok' ? c.name : zh.taskForm.credentialUnverified(c.name, zh.verify[c.verify_state]),
      value: c.name,
      disabled: c.verify_state === 'failed',
    }));
}

/** 基本信息 (07 §3 screen 1): name, note, source, dataset, region, key, delivery directory. */
export function BasicSection(p: BasicSectionProps) {
  const { v, set, errors } = p;
  const noKeys = p.credentials.length === 0;
  const credSelect = (value: string, onChange: (x: string) => void, extra: { label: string; value: string }[] = [], aria = zh.taskForm.credential, skip?: string) => (
    <Space direction="vertical" style={{ width: '100%' }} size={4}>
      <Select value={value || extra.some((o) => o.value === value) ? value : undefined} onChange={onChange} placeholder={zh.taskForm.credentialPlaceholder} aria-label={aria} status={errors.credential && aria === zh.taskForm.credential ? 'error' : undefined} options={[...extra, ...credentialOptions(p.credentials, skip, value)]} />
      {noKeys ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {zh.taskForm.noCredential}，
          <Button type="text" size="mini" onClick={p.onAddCredential} style={{ padding: 0 }}>
            {zh.taskForm.addCredential}
          </Button>
        </Typography.Text>
      ) : null}
    </Space>
  );
  // The probe result only belongs to the field while it still shows the probed directory.
  const probeForField = p.probe.key.startsWith(`${v.outputUri.trim()}|`) && Boolean(v.outputUri.trim());
  const probeOk = probeForField && p.probe.status === 'ok' ? zh.taskForm.probeOk(shortTime(p.probe.at), p.probe.credential) : undefined;
  const probeError = probeForField && p.probe.status === 'fail' ? zh.taskForm.probeFail(p.probe.message) : undefined;
  const probeWarn = probeForField && p.probe.status === 'warn' ? zh.taskForm.probeLeftover(p.probe.message) : undefined;
  const registeredOptions = p.registered
    .filter((d) => d.source === 'tos' && (!v.datasetUri || d.uri.includes(v.datasetUri) || d.name.includes(v.datasetUri)))
    .slice(0, 20)
    .map((d) => ({ name: d.name, value: d.uri, key: d.id }));

  return (
    <Card title={zh.taskForm.sectionBasic}>
      <Row gutter={24}>
        <Col span={12}>
          <Field label={zh.taskForm.name} required error={errors.name}>
            <Input value={v.name} onChange={(x) => set({ name: x })} placeholder={zh.taskForm.namePlaceholder} maxLength={128} aria-label={zh.taskForm.name} />
          </Field>
        </Col>
        <Col span={12}>
          <Field label={`${zh.taskForm.note}（${zh.taskForm.noteHelp}）`} error={errors.note}>
            <Input value={v.note} onChange={(x) => set({ note: x })} placeholder={zh.taskForm.notePlaceholder} maxLength={2000} aria-label={zh.taskForm.note} />
          </Field>
        </Col>
      </Row>
      <Field label={zh.taskForm.source} required>
        <Radio.Group
          type="button"
          value={v.source}
          disabled={p.sourceLocked}
          onChange={(x: FormValues['source']) => set({ source: x, datasetId: null })}
          aria-label={zh.taskForm.source}
        >
          <Radio value="tos">{zh.source.tos}</Radio>
          {p.publicCatalog ? <Radio value="public">{zh.source.public}</Radio> : null}
          {p.localEnabled ? (
            <Radio value="local">
              {zh.source.local} <Tag size="small" color="orange">{zh.source.experimental}</Tag>
            </Radio>
          ) : null}
        </Radio.Group>
      </Field>

      {p.batch ? (
        p.batchList
      ) : v.source === 'public' ? (
        <Row gutter={24}>
          <Col span={12}>
            <Field label={zh.taskForm.publicDataset} required error={errors.publicUri} notes={p.linkNotes.dataset} extra={zh.taskForm.publicNote}>
              <Select
                value={v.publicUri || undefined}
                onChange={(x: string) => set({ publicUri: x })}
                placeholder={zh.taskForm.publicDataset}
                aria-label={zh.taskForm.publicDataset}
                showSearch
                options={(p.publicCatalog ?? []).map((d) => ({ label: `${d.name}${d.episodes ? ` · ${zh.common.items(d.episodes)}` : ''}`, value: d.uri }))}
              />
            </Field>
          </Col>
          <Col span={6}>
            <Field label={zh.taskForm.region} notes={p.linkNotes.region}>
              <Input disabled value={zh.taskForm.publicRegion} aria-label={zh.taskForm.region} />
            </Field>
          </Col>
          <Col span={6}>
            <Field label={zh.taskForm.credential}>
              <Input disabled value={zh.taskForm.publicKey} aria-label={zh.taskForm.credential} />
            </Field>
          </Col>
        </Row>
      ) : (
        <Row gutter={24}>
          <Col span={12}>
            <Field
              label={v.source === 'local' ? zh.taskForm.localPath : zh.taskForm.datasetUri}
              required
              error={errors.datasetUri}
              notes={p.linkNotes.dataset}
              extra={
                v.source === 'local'
                  ? zh.taskForm.localPathHelp
                  : v.datasetId
                    ? zh.taskForm.registeredHint(p.registered.find((d) => d.id === v.datasetId)?.name ?? v.datasetUri)
                    : undefined
              }
            >
              <AutoComplete
                value={v.datasetUri}
                data={v.source === 'tos' ? registeredOptions : []}
                placeholder={v.source === 'local' ? '/data/datasets/xxx' : zh.taskForm.datasetUriPlaceholder}
                onChange={(x: string) => set({ datasetUri: x, datasetId: null })}
                onSelect={(uri: string) => {
                  const d = p.registered.find((x) => x.uri === uri);
                  if (d) p.onPickRegistered(d);
                }}
                inputProps={{ 'aria-label': zh.taskForm.datasetUri, className: 'mono' } as never}
              />
            </Field>
          </Col>
          {v.source === 'tos' ? (
            <>
              <Col span={6}>
                <Field label={zh.taskForm.region} required error={errors.region} notes={p.linkNotes.region}>
                  <RegionSelect value={v.region} onChange={(x) => set({ region: x, datasetId: null }, 'region')} ariaLabel={zh.taskForm.region} status={errors.region ? 'error' : undefined} />
                </Field>
              </Col>
              <Col span={6}>
                <Field
                  label={
                    <span>
                      {zh.taskForm.credential}{' '}
                      <Link to="/credentials" target="_blank">
                        {zh.common.manage}
                      </Link>
                    </span>
                  }
                  required
                  error={errors.credential}
                >
                  {credSelect(v.credential, (x) => set({ credential: x, datasetId: null }, 'credential'))}
                </Field>
              </Col>
            </>
          ) : null}
        </Row>
      )}

      {p.batch && v.source === 'tos' ? (
        <Row gutter={24}>
          <Col span={12}>
            <Field label={zh.taskForm.region} required error={errors.region} notes={p.linkNotes.region}>
              <RegionSelect value={v.region} onChange={(x) => set({ region: x }, 'region')} ariaLabel={zh.taskForm.region} />
            </Field>
          </Col>
          <Col span={12}>
            <Field label={zh.taskForm.credential} required error={errors.credential}>
              {credSelect(v.credential, (x) => set({ credential: x }, 'credential'))}
            </Field>
          </Col>
        </Row>
      ) : null}

      <Row gutter={24}>
        <Col span={12}>
          <Field
            label={`${p.batch ? zh.taskForm.outputRoot : zh.taskForm.outputUri}（${zh.taskForm.outputHelp}）`}
            required
            error={errors.outputUri ?? probeError}
            ok={probeOk ?? (probeForField && p.probe.status === 'running' ? zh.taskForm.probeRunning : undefined)}
            warn={errors.outputUri ? undefined : probeWarn}
            notes={p.outputNote ? [p.outputNote] : undefined}
          >
            <Input
              className="mono"
              value={v.outputUri}
              onChange={(x) => set({ outputUri: x }, 'outputUri')}
              onBlur={p.onOutputBlur}
              placeholder={zh.taskForm.outputPlaceholder}
              aria-label={zh.taskForm.outputUri}
              status={errors.outputUri || probeError ? 'error' : undefined}
            />
          </Field>
        </Col>
        <Col span={6}>
          <Field label={zh.taskForm.outputRegion} required error={errors.outputRegion}>
            <RegionSelect
              value={v.outputRegion}
              onChange={(x) => set({ outputRegion: x })}
              allowEmpty={v.source !== 'public'}
              // the dataset's own region is the fixed first entry, not listed again (requester, third round)
              emptyLabel={zh.taskForm.sameAsDataset}
              exclude={v.source !== 'public' ? v.region || undefined : undefined}
              ariaLabel={`${zh.taskForm.outputUri}${zh.taskForm.outputRegion}`}
            />
          </Field>
        </Col>
        <Col span={6}>
          <Field label={zh.taskForm.outputCredential} required error={errors.outputCredential}>
            {credSelect(
              v.outputCredential,
              (x) => set({ outputCredential: x }, 'outputCredential'),
              // the dataset's own key is the fixed first entry, not listed again (requester, third round)
              v.source !== 'public' ? [{ label: zh.taskForm.sameAsDataset, value: '' }] : [],
              `${zh.taskForm.outputUri}${zh.taskForm.outputCredential}`,
              v.source !== 'public' ? v.credential || undefined : undefined,
            )}
          </Field>
        </Col>
      </Row>
    </Card>
  );
}
