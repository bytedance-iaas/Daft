import { Button, Card, Divider, Input, InputNumber, Radio, Select, Space, Switch, Typography } from '@arco-design/web-react';
import type { ModuleRegistry, PreflightResult } from '../../api/types';
import { paramFields, type ParamField } from '../../lib/paramSchema';
import { availabilityOf, embodimentHint, reasonText } from '../../lib/preflight';
import { zh } from '../../locales/zh';
import { Field } from './Field';
import type { Errors, FormValues } from './formModel';
import { activeModules } from './formModel';

function ParamInput({ f, value, onChange, error }: { f: ParamField; value: unknown; onChange: (v: unknown) => void; error?: string }) {
  const v = value ?? f.default;
  switch (f.kind) {
    case 'choice':
      return f.options && f.options.length <= 4 ? (
        <Radio.Group value={v} onChange={onChange} aria-label={f.title}>
          {f.options.map((o) => (
            <Radio key={String(o.value)} value={o.value}>
              {o.label}
            </Radio>
          ))}
        </Radio.Group>
      ) : (
        <Select value={v as string} onChange={onChange} aria-label={f.title} options={(f.options ?? []).map((o) => ({ label: o.label, value: o.value as string }))} />
      );
    case 'boolean':
      return <Switch checked={Boolean(v)} onChange={onChange} aria-label={f.title} />;
    case 'integer':
    case 'number':
      return (
        <InputNumber
          value={v as number | undefined}
          min={f.min}
          max={f.max}
          precision={f.kind === 'integer' ? 0 : undefined}
          onChange={(x) => onChange(x === undefined || x === null ? undefined : Number(x))}
          aria-label={f.title}
          error={Boolean(error)}
        />
      );
    default:
      return <Input value={(v as string) ?? ''} maxLength={f.maxLength} onChange={onChange} aria-label={f.title} status={error ? 'error' : undefined} />;
  }
}

/**
 * Screen 2 (D38, 07 §3): only the enabled modules, what they still need first (必填, e.g. the
 * robot type, or 「跳过该模块」), then their optional parameters generated from param_schema, and
 * one line for modules without extra settings.
 */
export function ModuleSettings({
  v,
  set,
  errors,
  registry,
  preflight,
  embodimentOptions,
}: {
  v: FormValues;
  set: (patch: Partial<FormValues>) => void;
  errors: Errors;
  registry: ModuleRegistry | undefined;
  preflight: PreflightResult | null;
  embodimentOptions: string[];
}) {
  const specs = (registry?.modules ?? []).filter((m) => v.modules.includes(m.id));
  const active = activeModules(v);
  const needing = specs.filter((m) => active.includes(m.id) && embodimentHint(preflight, m.id));
  const withParams = specs.filter((m) => active.includes(m.id) && paramFields(m.param_schema).length);
  const plain = specs.filter((m) => active.includes(m.id) && !needing.includes(m) && !withParams.includes(m));
  const skipped = specs.filter((m) => v.skipped.includes(m.id));
  const setParam = (mod: string, key: string, value: unknown) => set({ params: { ...v.params, [mod]: { ...(v.params[mod] ?? {}), [key]: value } } });
  const options = embodimentOptions.length ? embodimentOptions : needing.flatMap((m) => embodimentHint(preflight, m.id)?.options ?? []);

  return (
    <Card title={zh.taskForm.screen2Title}>
      {needing.length ? (
        <>
          <Typography.Title heading={6}>{zh.taskForm.needsInputTitle}</Typography.Title>
          {needing.map((m, i) => (
            <div key={m.id} className="module-card warn" style={{ marginBottom: 12 }} data-testid={`needs-${m.id}`}>
              <Space align="start" style={{ width: '100%', justifyContent: 'space-between' }}>
                <div>
                  <b>⚠ {m.name_zh}</b>
                  <div className="muted" style={{ fontSize: 12 }}>
                    {reasonText(availabilityOf(preflight, m.id))}
                  </div>
                </div>
                <Button size="small" onClick={() => set({ skipped: [...v.skipped, m.id] })}>
                  {zh.taskForm.skipModule}
                </Button>
              </Space>
              {i === 0 ? (
                <div style={{ marginTop: 8, maxWidth: 360 }}>
                  <Field
                    label={zh.taskForm.embodiment}
                    required
                    error={errors.embodiment}
                    extra={needing.length > 1 ? zh.taskForm.embodimentShared(needing.map((x) => x.name_zh).join('、')) : undefined}
                  >
                    <Select
                      value={v.embodiment || undefined}
                      placeholder={zh.taskForm.embodimentPlaceholder}
                      aria-label={zh.taskForm.embodiment}
                      status={errors.embodiment ? 'error' : undefined}
                      onChange={(x: string) => set({ embodiment: x })}
                      options={[...new Set(options)].map((o) => ({ label: o, value: o }))}
                    />
                  </Field>
                </div>
              ) : null}
            </div>
          ))}
        </>
      ) : null}

      {withParams.length ? (
        <>
          <Typography.Title heading={6}>{zh.taskForm.optionalTitle}</Typography.Title>
          {withParams.map((m) => (
            <div key={m.id} style={{ marginBottom: 12 }} data-testid={`params-${m.id}`}>
              <b>{m.name_zh}</b>
              {paramFields(m.param_schema).map((f) => (
                <Field key={f.key} label={f.title} required={f.required} extra={f.description} error={errors[`params.${m.id}.${f.key}`]}>
                  <ParamInput f={f} value={v.params[m.id]?.[f.key]} error={errors[`params.${m.id}.${f.key}`]} onChange={(x) => setParam(m.id, f.key, x)} />
                </Field>
              ))}
            </div>
          ))}
        </>
      ) : null}

      {plain.length ? (
        <Typography.Paragraph type="secondary" data-testid="no-settings">
          {zh.taskForm.noSettings(plain.map((m) => m.name_zh).join('、'))}
        </Typography.Paragraph>
      ) : null}

      {skipped.length ? (
        <>
          <Divider />
          <Typography.Title heading={6}>{zh.taskForm.skippedTitle}</Typography.Title>
          {skipped.map((m) => (
            <Space key={m.id} style={{ marginRight: 16 }}>
              <span className="muted">{m.name_zh}</span>
              <Button size="mini" type="text" onClick={() => set({ skipped: v.skipped.filter((x) => x !== m.id) })}>
                {zh.taskForm.unskip}
              </Button>
            </Space>
          ))}
        </>
      ) : null}
      {errors.modules ? <div className="field-note-error">{errors.modules}</div> : null}
    </Card>
  );
}
