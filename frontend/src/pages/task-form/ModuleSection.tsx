import { Button, Card, Checkbox, Divider, Grid, Modal, Radio, Space, Tag, Typography } from '@arco-design/web-react';
import { useState } from 'react';
import type { ModuleAvailability, ModuleRegistry, ModuleSpec } from '../../api/types';
import { needsVlm, optIn, reasonText, toggleModule } from '../../lib/preflight';
import { zh } from '../../locales/zh';
import type { Errors, FormValues } from './formModel';

const { Row, Col } = Grid;

export type AvailabilityMap = Record<string, ModuleAvailability | undefined> | null;

function needsInputText(a: ModuleAvailability): string {
  if (a.input_hint?.field === 'embodiment_id') return zh.taskForm.moduleNeedsEmbodiment;
  if (a.input_hint?.field === 'vlm') return zh.taskForm.moduleNeedsVlm;
  return reasonText(a);
}

function ModuleCard({
  m,
  a,
  checked,
  marked,
  onToggle,
  disabled,
}: {
  m: ModuleSpec;
  a: ModuleAvailability | undefined;
  checked: boolean;
  marked?: string;
  onToggle: () => void;
  disabled: boolean;
}) {
  const warn = a?.availability === 'needs_input';
  const cls = ['module-card', checked ? 'selected' : '', warn ? 'warn' : '', disabled ? 'disabled' : '', marked ? 'marked' : ''].filter(Boolean).join(' ');
  return (
    <div className={cls} data-testid={`module-${m.id}`}>
      <Checkbox checked={checked} disabled={disabled} onChange={onToggle} aria-label={m.name_zh}>
        <b>{m.name_zh}</b>
      </Checkbox>{' '}
      <Tag size="small">{zh.gate[m.gate]}</Tag> {needsVlm(m) ? <Tag size="small" color="purple">{zh.taskForm.callsModel}</Tag> : null}
      <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
        {m.summary_zh}
      </div>
      {warn && a ? (
        <div style={{ color: 'var(--c-warning)', fontSize: 12, marginTop: 4 }}>
          ⚠ {needsInputText(a)}
        </div>
      ) : null}
      {marked ? <div className="field-note-error">{marked}</div> : null}
    </div>
  );
}

/** 质检范围 (07 §3): presets, tri-state module cards straight from the registry and preflight. */
export function ModuleSection({
  v,
  set,
  errors,
  registry,
  availability,
  marked,
}: {
  v: FormValues;
  set: (patch: Partial<FormValues>) => void;
  errors: Errors;
  registry: ModuleRegistry | undefined;
  availability: AvailabilityMap;
  marked: Record<string, string>;
}) {
  const [details, setDetails] = useState<{ m: ModuleSpec; a: ModuleAvailability } | null>(null);
  const modules = registry?.modules ?? [];
  const state = (id: string) => availability?.[id]?.availability ?? (availability ? 'available' : null);
  const usable = modules.filter((m) => state(m.id) === 'available' || state(m.id) === 'needs_input');
  const unsupported = modules.filter((m) => state(m.id) === 'unsupported');
  const toggle = (id: string) => {
    const modules = toggleModule(registry, v.modules, id);
    set({ modules, preset: 'custom', skipped: v.skipped.filter((x) => x !== id) });
  };
  const help = v.preset === 'full' ? zh.taskForm.presetFullHelp : v.preset === 'quick' ? zh.taskForm.presetQuickHelp : zh.taskForm.presetCustomHelp;
  return (
    <Card
      title={zh.taskForm.sectionModules}
      extra={
        <Space>
          <Button size="small" type="text" disabled={!availability} onClick={() => set({ modules: usable.filter((m) => !optIn(m)).map((m) => m.id), preset: 'custom', skipped: [] })}>
            {zh.taskForm.selectAll}
          </Button>
          <Button size="small" type="text" onClick={() => set({ modules: [], preset: 'custom' })}>
            {zh.taskForm.clearAll}
          </Button>
        </Space>
      }
    >
      <Radio.Group
        type="button"
        value={v.preset}
        onChange={(x: FormValues['preset']) => {
          if (x === 'custom') return set({ preset: 'custom' });
          const ids = usable.filter((m) => !optIn(m) && (x === 'full' || !needsVlm(m))).map((m) => m.id);
          set({ preset: x, modules: ids, skipped: [] });
        }}
        aria-label={zh.taskForm.sectionModules}
      >
        <Radio value="full">{zh.preset.full}</Radio>
        <Radio value="quick">{zh.preset.quick}</Radio>
        <Radio value="custom">{zh.preset.custom}</Radio>
      </Radio.Group>
      <div className="muted" style={{ margin: '8px 0 12px', fontSize: 12 }}>
        {availability ? help : zh.taskForm.waitingPreflight}
      </div>
      {errors.modules ? <div className="field-note-error" style={{ marginBottom: 8 }}>{errors.modules}</div> : null}
      <Row gutter={[12, 12]}>
        {(availability ? usable : modules).map((m) => (
          <Col key={m.id} xs={24} sm={12} md={8} lg={6}>
            <ModuleCard m={m} a={availability?.[m.id]} checked={v.modules.includes(m.id)} marked={marked[m.id]} disabled={!availability} onToggle={() => toggle(m.id)} />
          </Col>
        ))}
      </Row>
      {unsupported.length ? (
        <>
          <Divider />
          <Typography.Text>
            {zh.taskForm.unsupportedTitle(unsupported.length)} <span className="muted">{zh.taskForm.unsupportedDesc}</span>
          </Typography.Text>
          <Row gutter={[12, 12]} style={{ marginTop: 8 }}>
            {unsupported.map((m) => {
              const a = availability?.[m.id];
              return (
                <Col key={m.id} xs={24} sm={12} md={8} lg={6}>
                  <div className="module-card disabled" data-testid={`module-${m.id}`}>
                    <Checkbox checked={false} disabled aria-label={m.name_zh}>
                      <b>{m.name_zh}</b>
                    </Checkbox>{' '}
                    <Tag size="small">{zh.gate[m.gate]}</Tag>
                    <div style={{ fontSize: 12, marginTop: 4 }}>
                      <span style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{reasonText(a)}</span>
                      <Button type="text" size="mini" style={{ padding: 0 }} onClick={() => a && setDetails({ m, a })}>
                        {zh.taskForm.details}
                      </Button>
                    </div>
                  </div>
                </Col>
              );
            })}
          </Row>
        </>
      ) : null}
      <Modal visible={Boolean(details)} title={details ? zh.taskForm.detailsTitle(details.m.name_zh) : ''} onCancel={() => setDetails(null)} footer={null} unmountOnExit>
        {details ? (
          <div>
            <Typography.Paragraph>{reasonText(details.a)}</Typography.Paragraph>
            {details.a.reason && details.a.reason_code && zh.reason[details.a.reason_code] ? (
              <Typography.Paragraph type="secondary" className="mono" style={{ fontSize: 12 }}>
                {details.a.reason}
              </Typography.Paragraph>
            ) : null}
          </div>
        ) : null}
      </Modal>
    </Card>
  );
}
