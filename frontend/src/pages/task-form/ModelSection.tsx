import { Alert, Button, Card, Collapse, Grid, InputNumber, Select, Space, Switch, Typography } from '@arco-design/web-react';
import { Link } from 'react-router-dom';
import type { ModuleRegistry, VlmBackend } from '../../api/types';
import { effortOptions, knowsLevels } from '../../features/keys/BackendDrawer';
import { needsVlm } from '../../lib/preflight';
import { zh } from '../../locales/zh';
import { Field } from './Field';
import type { Errors, FormValues, TimeoutKey } from './formModel';
import { activeModules } from './formModel';

const { Row, Col } = Grid;

/** 模型配置 (07 §3, 08 §4.1): backend, model, 思考强度 (「模型默认」 sends nothing). */
export function ModelSection({
  v,
  set,
  errors,
  backends,
  registry,
  onAddBackend,
}: {
  v: FormValues;
  set: (patch: Partial<FormValues>, touched?: keyof FormValues) => void;
  errors: Errors;
  backends: VlmBackend[];
  registry: ModuleRegistry | undefined;
  onAddBackend: () => void;
}) {
  const backend = backends.find((b) => b.name === v.vlmBackend);
  const model = backend?.models.find((m) => m.model_name === v.vlmModel);
  const names = (registry?.modules ?? [])
    .filter((m) => needsVlm(m) && activeModules(v).includes(m.id))
    .map((m) => m.name_zh)
    .join(zh.common.and);
  const slow = ['high', 'xhigh', 'max'].includes(v.effort);
  return (
    <Card
      title={
        <Space>
          {zh.taskForm.sectionModel}
          <Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 'normal' }}>
            {zh.taskForm.modelDesc(names)}
          </Typography.Text>
        </Space>
      }
    >
      <Row gutter={24}>
        <Col span={8}>
          <Field
            label={
              <span>
                {zh.taskForm.backend}{' '}
                <Link to="/credentials#vlm" target="_blank">
                  {zh.common.manage}
                </Link>
              </span>
            }
            required
            error={errors.vlmBackend}
          >
            <Select
              value={v.vlmBackend || undefined}
              placeholder={zh.taskForm.backendPlaceholder}
              aria-label={zh.taskForm.backend}
              status={errors.vlmBackend ? 'error' : undefined}
              onChange={(x: string) => {
                const b = backends.find((y) => y.name === x);
                set({ vlmBackend: x, vlmModel: b?.models.length === 1 ? b.models[0].model_name : '', effort: '' }, 'vlmBackend');
              }}
              options={backends.map((b) => ({ label: `${b.name}（${b.kind === 'ark' ? zh.credentials.kindArk : zh.credentials.kindCustomShort}${b.verify_state === 'ok' ? '' : ` · ${zh.verify[b.verify_state]}`}）`, value: b.name }))}
            />
            {!backends.length ? (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {zh.taskForm.noBackend}，
                <Button type="text" size="mini" style={{ padding: 0 }} onClick={onAddBackend}>
                  {zh.taskForm.addBackend}
                </Button>
              </Typography.Text>
            ) : null}
          </Field>
        </Col>
        <Col span={8}>
          <Field label={zh.taskForm.model} required error={errors.vlmModel} extra={backend && !backend.models.length ? zh.taskForm.noModel : undefined}>
            <Select
              value={v.vlmModel || undefined}
              placeholder={zh.taskForm.modelPlaceholder}
              aria-label={zh.taskForm.model}
              status={errors.vlmModel ? 'error' : undefined}
              disabled={!backend}
              onChange={(x: string) => set({ vlmModel: x, effort: '' }, 'vlmModel')}
              options={(backend?.models ?? []).map((m) => ({ label: m.is_default ? `${m.model_name}（${zh.credentials.modelDefault}）` : m.model_name, value: m.model_name }))}
            />
          </Field>
        </Col>
        <Col span={8}>
          <Field label={zh.taskForm.effort} extra={model && !knowsLevels(model) ? zh.taskForm.effortAllHint : undefined}>
            <Select value={v.effort} aria-label={zh.taskForm.effort} disabled={!model} onChange={(x: string) => set({ effort: x })} options={effortOptions(model, backend?.kind)} />
          </Field>
        </Col>
      </Row>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {zh.taskForm.effortDefaultHelp}
      </Typography.Text>
      {slow ? <Alert type="warning" style={{ marginTop: 8 }} content={zh.taskForm.effortSlowHint} /> : null}
    </Card>
  );
}

/** 高级设置 (collapsed): only upper bounds (D31); the plan itself is not open. */
export function AdvancedSection({ v, set, errors, vlm }: { v: FormValues; set: (patch: Partial<FormValues>) => void; errors: Errors; vlm: boolean }) {
  const hasError = Object.keys(errors).some((k) => k.startsWith('timeouts.') || k === 'cpuLimit' || k === 'vlmLimit');
  return (
    <Collapse defaultActiveKey={hasError ? ['adv'] : []} key={hasError ? 'open' : 'closed'}>
      <Collapse.Item
        name="adv"
        header={<b>{zh.taskForm.sectionAdvanced}</b>}
      >
        <Alert type="info" content={zh.taskForm.advancedNote} style={{ marginBottom: 12 }} />
        <Row gutter={24}>
          {vlm ? (
            <>
              <Col span={8}>
                <Field label={zh.taskForm.vlmRetry}>
                  <Select
                    value={v.vlmRetry}
                    aria-label={zh.taskForm.vlmRetry}
                    onChange={(x: number) => set({ vlmRetry: x })}
                    options={[3, 2, 1, 0].map((n) => ({ label: zh.taskForm.vlmRetryOption(n), value: n }))}
                  />
                </Field>
              </Col>
              <Col span={8}>
                <Field label={zh.taskForm.vlmHedge} extra={zh.taskForm.vlmHedgeHelp}>
                  <Switch checked={v.vlmHedge} onChange={(x) => set({ vlmHedge: x })} aria-label={zh.taskForm.vlmHedge} />
                </Field>
              </Col>
            </>
          ) : null}
          <Col span={8}>
            <Field label={zh.taskForm.cpuLimit} error={errors.cpuLimit}>
              <InputNumber value={v.cpuLimit} min={1} precision={0} placeholder={zh.taskForm.limitPlaceholder} onChange={(x) => set({ cpuLimit: x ? Number(x) : undefined })} aria-label={zh.taskForm.cpuLimit} />
            </Field>
          </Col>
          {vlm ? (
            <Col span={8}>
              <Field label={zh.taskForm.vlmLimit} error={errors.vlmLimit}>
                <InputNumber value={v.vlmLimit} min={1} precision={0} placeholder={zh.taskForm.limitPlaceholder} onChange={(x) => set({ vlmLimit: x ? Number(x) : undefined })} aria-label={zh.taskForm.vlmLimit} />
              </Field>
            </Col>
          ) : null}
        </Row>
        {vlm ? (
          <Field label={zh.taskForm.timeouts}>
            <Space wrap>
              {(Object.keys(v.timeouts) as TimeoutKey[]).map((k) => (
                <span key={k}>
                  <span className="muted">{zh.taskForm.timeoutLabels[k]} </span>
                  <InputNumber
                    style={{ width: 90 }}
                    value={v.timeouts[k]}
                    min={1}
                    onChange={(x) => set({ timeouts: { ...v.timeouts, [k]: x === undefined || x === null ? Number.NaN : Number(x) } })}
                    aria-label={`${zh.taskForm.timeouts} ${zh.taskForm.timeoutLabels[k]}`}
                    error={Boolean(errors[`timeouts.${k}`])}
                  />
                </span>
              ))}
            </Space>
          </Field>
        ) : null}
        <Space size={24}>
          <Switch checked={v.exportDataset} onChange={(x) => set({ exportDataset: x })} aria-label={zh.taskForm.exportDataset} /> {zh.taskForm.exportDataset}
          <Switch checked={v.clips} onChange={(x) => set({ clips: x })} aria-label={zh.taskForm.clips} /> {zh.taskForm.clips}
        </Space>
      </Collapse.Item>
    </Collapse>
  );
}
