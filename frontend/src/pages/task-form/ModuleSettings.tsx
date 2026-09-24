import { Button, Card, Divider, Input, InputNumber, Radio, Select, Space, Switch, Typography } from '@arco-design/web-react';
import { useRef, useState } from 'react';
import { ApiError } from '../../api/errors';
import type { ModuleRegistry, PreflightResult, Upload, UploadIssue, UploadKind } from '../../api/types';
import { uploadFile, type UploadPhase } from '../../api/uploads';
import { groupFields, paramFields, UPLOAD_PREFIX, type ChoiceGroup, type ParamField } from '../../lib/paramSchema';
import { availabilityOf, embodimentHint, reasonText } from '../../lib/preflight';
import { zh } from '../../locales/zh';
import { Field } from './Field';
import type { Errors, FormPatch, FormValues } from './formModel';
import { activeModules } from './formModel';

/**
 * A file parameter (registry 1.5 `format: upload`): pick a file, POST /uploads validates it on
 * arrival, the handle `upload:<id>` becomes the value. A rejected file shows its located errors.
 * The button says 上传中 while the file goes out and 校验中 while the server checks it.
 */
export function UploadInput({ f, value, onChange }: { f: ParamField; value: unknown; onChange: (v: unknown) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<UploadPhase | null>(null);
  const busy = phase !== null;
  const [done, setDone] = useState<Upload | null>(null);
  const [problem, setProblem] = useState<{ message: string; errors: UploadIssue[] } | null>(null);
  const has = typeof value === 'string' && value.startsWith(UPLOAD_PREFIX);
  const pick = async (file: File | undefined) => {
    if (!file) return;
    setProblem(null);
    if (f.maxMb && file.size > f.maxMb * 1024 * 1024) {
      setProblem({ message: zh.taskForm.uploadTooBig(f.maxMb), errors: [] });
      return;
    }
    setPhase('uploading');
    try {
      const up = await uploadFile(file, (f.uploadKind ?? 'eef_trajectory') as UploadKind, setPhase);
      setDone(up);
      onChange(up.handle);
    } catch (e) {
      const details = e instanceof ApiError ? (e.details as { errors?: UploadIssue[] } | undefined) : undefined;
      setProblem({ message: e instanceof Error ? e.message : String(e), errors: details?.errors ?? [] });
    } finally {
      setPhase(null);
      if (input.current) input.current.value = '';
    }
  };
  return (
    <div data-testid={`upload-${f.key}`}>
      <input ref={input} type="file" accept={(f.accept ?? []).join(',')} style={{ display: 'none' }} aria-label={f.title} onChange={(e) => void pick(e.target.files?.[0])} />
      <Space>
        <Button size="small" loading={busy} onClick={() => input.current?.click()} data-testid={`upload-button-${f.key}`}>
          {phase === 'uploading' ? zh.taskForm.uploading : phase === 'validating' ? zh.taskForm.validating : has ? zh.taskForm.uploadReplace : zh.taskForm.uploadChoose}
        </Button>
        {f.accept ? <span className="muted" style={{ fontSize: 12 }}>{zh.taskForm.uploadAccept(f.accept, f.maxMb)}</span> : null}
      </Space>
      {done && value === done.handle ? (
        <div style={{ fontSize: 12, marginTop: 4 }} data-testid={`upload-done-${f.key}`}>
          {zh.taskForm.uploadDone(done.name, done.sha256)}
          <div className="muted">{zh.taskForm.uploadSummary(done.validation.summary as Record<string, unknown>)}</div>
          {done.validation.warnings.length ? <div className="muted">{zh.taskForm.uploadWarnings(done.validation.warnings.length)}</div> : null}
        </div>
      ) : has ? (
        <div className="muted mono" style={{ fontSize: 12, marginTop: 4 }}>
          {String(value)}
        </div>
      ) : null}
      {problem ? (
        <div className="field-note-error" style={{ marginTop: 4 }} data-testid={`upload-error-${f.key}`}>
          {zh.taskForm.uploadFailed}
          {problem.message}
          {problem.errors.length > 1 ? (
            <>
              <div className="muted">{zh.taskForm.uploadMore(problem.errors.length)}</div>
              <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                {problem.errors.slice(0, 5).map((e, i) => (
                  <li key={i}>
                    {e.problem}
                    {zh.taskForm.uploadWhere(e) ? <span className="muted">（{zh.taskForm.uploadWhere(e)}）</span> : null}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ParamInput({ f, value, onChange, error }: { f: ParamField; value: unknown; onChange: (v: unknown) => void; error?: string }) {
  const v = value ?? f.default;
  switch (f.kind) {
    case 'upload':
      return <UploadInput f={f} value={value} onChange={onChange} />;
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
 * A choice group (registry 1.10 `x-choice-group`, e.g. the EEF module's 夹爪参考): which of the
 * parameters, then that one's value. Only the chosen one keeps a value - picking the other drops
 * it (二选一) - and the help text is the chosen one's.
 */
function ChoiceGroupField({
  mod,
  group,
  fields,
  values,
  errors,
  onChange,
}: {
  mod: string;
  group: ChoiceGroup;
  fields: ParamField[];
  values: Record<string, unknown>;
  errors: Errors;
  onChange: (key: string, value: unknown) => void;
}) {
  const given = fields.find((f) => values[f.key] !== undefined && values[f.key] !== null && values[f.key] !== '');
  const [chosen, setChosen] = useState(given?.key ?? fields[0].key);
  const f = fields.find((x) => x.key === chosen) ?? fields[0];
  const error = fields.map((x) => errors[`params.${mod}.${x.key}`]).find(Boolean);
  const choose = (key: string) => {
    setChosen(key);
    for (const x of fields) if (x.key !== key && values[x.key]) onChange(x.key, undefined);
  };
  return (
    <Field label={group.title} required={group.required} extra={f.description} error={error}>
      <div className="choice-group" data-testid={`choice-${group.id}`}>
        <Select value={chosen} onChange={choose} aria-label={group.title} style={{ width: 160 }} options={fields.map((x) => ({ label: x.title, value: x.key }))} />
        <ParamInput key={f.key} f={f} value={values[f.key]} error={error} onChange={(x) => onChange(f.key, x)} />
      </div>
    </Field>
  );
}

/**
 * Screen 2 (D38, 07 §3): only the enabled modules, what they still need first (必填, e.g. the
 * robot type, or 「跳过该模块」), then each module's parameters generated from param_schema under
 * its name (fourth round: no 「可选设置」 heading). Modules without extra settings are left out.
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
  set: (patch: FormPatch) => void;
  errors: Errors;
  registry: ModuleRegistry | undefined;
  preflight: PreflightResult | null;
  embodimentOptions: string[];
}) {
  const specs = (registry?.modules ?? []).filter((m) => v.modules.includes(m.id));
  const active = activeModules(v);
  const needing = specs.filter((m) => active.includes(m.id) && embodimentHint(preflight, m.id));
  const withParams = specs.filter((m) => active.includes(m.id) && paramFields(m.param_schema).length);
  const skipped = specs.filter((m) => v.skipped.includes(m.id));
  // Against the form as it is then: an upload can finish after another field (or upload) changed.
  const setParam = (mod: string, key: string, value: unknown) =>
    set((prev) => ({ params: { ...prev.params, [mod]: { ...(prev.params[mod] ?? {}), [key]: value } } }));
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

      {withParams.map((m) => (
        <div key={m.id} className="module-params" data-testid={`params-${m.id}`}>
          <Typography.Title heading={5} className="module-params-title">
            {m.name_zh}
          </Typography.Title>
          {groupFields(paramFields(m.param_schema)).map((entry) =>
            'group' in entry ? (
              <ChoiceGroupField
                key={entry.group.id}
                mod={m.id}
                group={entry.group}
                fields={entry.fields}
                values={v.params[m.id] ?? {}}
                errors={errors}
                onChange={(key, x) => setParam(m.id, key, x)}
              />
            ) : (
              <Field key={entry.field.key} label={entry.field.title} required={entry.field.required} extra={entry.field.description} error={errors[`params.${m.id}.${entry.field.key}`]}>
                <ParamInput f={entry.field} value={v.params[m.id]?.[entry.field.key]} error={errors[`params.${m.id}.${entry.field.key}`]} onChange={(x) => setParam(m.id, entry.field.key, x)} />
              </Field>
            ),
          )}
        </div>
      ))}

      {/* Modules without extra settings are not listed (requester item 16); a screen with nothing
          to set says so instead of showing an empty card. */}
      {!needing.length && !withParams.length && !skipped.length ? (
        <Typography.Paragraph type="secondary" data-testid="nothing-to-set">
          {zh.taskForm.nothingToSet}
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
