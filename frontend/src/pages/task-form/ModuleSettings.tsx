import { Alert, Button, Card, Collapse, Divider, Input, InputNumber, Radio, Select, Space, Switch, Typography } from '@arco-design/web-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { ApiError } from '../../api/errors';
import type { ModuleAvailability, ModuleRegistry, ModuleSpec, PreflightResult, Upload, UploadIssue, UploadKind } from '../../api/types';
import { uploadFile, type UploadPhase } from '../../api/uploads';
import { shortName } from '../../lib/declaration';
import { groupFields, UPLOAD_PREFIX, type ChoiceGroup, type FieldOrGroup, type ParamField } from '../../lib/paramSchema';
import { asksModel, availabilityOf, embodimentHint, moduleParamFields, reasonText } from '../../lib/preflight';
import { zh } from '../../locales/zh';
import { Field } from './Field';
import type { Errors, FormPatch, FormValues } from './formModel';
import { activeModules } from './formModel';

/**
 * A file parameter (registry 1.5 `format: upload`): pick a file, POST /uploads validates it on
 * arrival, the handle `upload:<id>` becomes the value. A rejected file shows its located errors.
 * The button says 上传中 while the file goes out and 校验中 while the server checks it; `onBusy`
 * tells the form, which holds its submit buttons meanwhile (fourth round). The picker, the button
 * and the result come apart so a caller can lay them out (the 夹爪参考 row).
 */
function useUpload(f: ParamField, value: unknown, onChange: (v: unknown) => void, onBusy?: (busy: boolean) => void) {
  const input = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<UploadPhase | null>(null);
  const [done, setDone] = useState<Upload | null>(null);
  const [problem, setProblem] = useState<{ message: string; errors: UploadIssue[] } | null>(null);
  const busyRef = useRef(onBusy);
  busyRef.current = onBusy;
  useEffect(() => {
    busyRef.current?.(phase !== null);
  }, [phase]);
  // gone mid-upload (the other choice picked, screen changed): not busy any more
  useEffect(() => () => busyRef.current?.(false), []);
  const busy = phase !== null;
  const has = typeof value === 'string' && value.startsWith(UPLOAD_PREFIX);
  const kind = (f.uploadKind ?? 'eef_trajectory') as UploadKind;
  // POST /uploads validates, the handle is the value
  const send = async (upload: () => Promise<Upload>) => {
    setProblem(null);
    setPhase('uploading');
    try {
      const up = await upload();
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
  const pick = async (file: File | undefined) => {
    if (!file) return;
    if (f.maxMb && file.size > f.maxMb * 1024 * 1024) {
      setProblem({ message: zh.taskForm.uploadTooBig(f.maxMb), errors: [] });
      return;
    }
    await send(() => uploadFile(file, kind, setPhase));
  };
  const picker = (
    <input ref={input} type="file" accept={(f.accept ?? []).join(',')} style={{ display: 'none' }} aria-label={f.title} onChange={(e) => void pick(e.target.files?.[0])} />
  );
  const button = (
    <Space>
      <Button size="small" loading={busy} onClick={() => input.current?.click()} data-testid={`upload-button-${f.key}`}>
        {phase === 'uploading' ? zh.taskForm.uploading : phase === 'validating' ? zh.taskForm.validating : has ? zh.taskForm.uploadReplace : zh.taskForm.uploadChoose}
      </Button>
      {f.accept ? <span className="muted" style={{ fontSize: 12 }}>{zh.taskForm.uploadAccept(f.accept, f.maxMb)}</span> : null}
    </Space>
  );
  const result: ReactNode = (
    <>
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
    </>
  );
  return { picker, button, result, busy };
}

export function UploadInput({
  f,
  value,
  onChange,
  onBusy,
}: {
  f: ParamField;
  value: unknown;
  onChange: (v: unknown) => void;
  onBusy?: (busy: boolean) => void;
}) {
  const u = useUpload(f, value, onChange, onBusy);
  return (
    <div data-testid={`upload-${f.key}`}>
      {u.picker}
      {u.button}
      {u.result}
    </div>
  );
}

/** The 夹爪参考 row: the choice, then the file button; the result under the whole row. */
function ChoiceUploadRow({ choice, f, value, onChange, onBusy }: { choice: ReactNode; f: ParamField; value: unknown; onChange: (v: unknown) => void; onBusy?: (busy: boolean) => void }) {
  const u = useUpload(f, value, onChange, onBusy);
  return (
    <div data-testid={`upload-${f.key}`}>
      {u.picker}
      <div className="choice-group">
        {choice}
        {u.button}
      </div>
      {u.result}
    </div>
  );
}

function ParamInput({
  f,
  value,
  onChange,
  error,
  onBusy,
}: {
  f: ParamField;
  value: unknown;
  onChange: (v: unknown) => void;
  error?: string;
  onBusy?: (busy: boolean) => void;
}) {
  const v = value ?? f.default;
  switch (f.kind) {
    case 'upload':
      return <UploadInput f={f} value={value} onChange={onChange} onBusy={onBusy} />;
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
  onBusy,
}: {
  mod: string;
  group: ChoiceGroup;
  fields: ParamField[];
  values: Record<string, unknown>;
  errors: Errors;
  onChange: (key: string, value: unknown) => void;
  onBusy?: (key: string, busy: boolean) => void;
}) {
  const given = fields.find((f) => values[f.key] !== undefined && values[f.key] !== null && values[f.key] !== '');
  const [chosen, setChosen] = useState(given?.key ?? fields[0].key);
  const f = fields.find((x) => x.key === chosen) ?? fields[0];
  const error = fields.map((x) => errors[`params.${mod}.${x.key}`]).find(Boolean);
  const choose = (key: string) => {
    setChosen(key);
    for (const x of fields) if (x.key !== key && values[x.key]) onChange(x.key, undefined);
  };
  const choice = <Select value={chosen} onChange={choose} aria-label={group.title} style={{ width: 160 }} options={fields.map((x) => ({ label: x.title, value: x.key }))} />;
  return (
    <Field label={group.title} required={group.required} extra={f.description} error={error}>
      <div data-testid={`choice-${group.id}`}>
        {f.kind === 'upload' ? (
          // the upload result lines up with the choice, under the whole row (fourth round)
          <ChoiceUploadRow key={f.key} choice={choice} f={f} value={values[f.key]} onChange={(x) => onChange(f.key, x)} onBusy={(b) => onBusy?.(f.key, b)} />
        ) : (
          <div className="choice-group">
            {choice}
            <ParamInput key={f.key} f={f} value={values[f.key]} error={error} onChange={(x) => onChange(f.key, x)} />
          </div>
        )}
      </div>
    </Field>
  );
}

/** What the declaration still lacks (design doc 25 §4.1), each item with its camera where it names one. */
function missingText(missing: NonNullable<ModuleAvailability['trajectory_source']>['missing']): string {
  return (missing ?? [])
    .map((m) => {
      const cam = /^calibration\.cameras\.(.+)\.[a-z_]+$/.exec(m.field)?.[1];
      const what = zh.declaration.missing[m.code] ?? m.code;
      return cam ? `${what}（${shortName(cam)}）` : what;
    })
    .join('、');
}

/**
 * EEF's trajectory.json by where the platform gets the trajectory (design doc 25 §4.3, the preflight's
 * `trajectory_source`): no pose record - the upload is required (as any asked parameter); the declaration lacks
 * something - say what, link to the dataset page, the upload folded away as the way around it; generated or
 * computed - optional, an upload overrides it.
 */
function TrajectoryField({
  f,
  a,
  datasetId,
  value,
  error,
  onChange,
  onBusy,
  onRerun,
}: {
  f: ParamField;
  a: ModuleAvailability;
  datasetId: string | null;
  value: unknown;
  error?: string;
  onChange: (v: unknown) => void;
  onBusy?: (busy: boolean) => void;
  onRerun?: () => void;
}) {
  const T = zh.taskForm.eefTrajectory;
  const src = a.trajectory_source!;
  if (src.kind === 'missing_declaration') {
    const given = typeof value === 'string' && value.startsWith(UPLOAD_PREFIX);
    return (
      <Field label={f.title} required error={error}>
        <div data-testid="eef-trajectory-declaration">
          <Alert
            type="warning"
            content={
              <Space direction="vertical" size={4}>
                <span>
                  {T.missingDeclaration}
                  {missingText(src.missing)}
                </span>
                {datasetId ? (
                  <Space size={12}>
                    <Link to={`/datasets/${datasetId}?declaration=1`} target="_blank" data-testid="eef-goto-declaration">
                      {T.gotoDeclaration}
                    </Link>
                    {onRerun ? (
                      <Button size="mini" type="text" style={{ padding: 0 }} onClick={onRerun}>
                        {T.rerun}
                      </Button>
                    ) : null}
                  </Space>
                ) : (
                  <span className="muted">{T.notRegistered}</span>
                )}
              </Space>
            }
          />
          <Collapse bordered={false} defaultActiveKey={given || !datasetId ? ['upload'] : []} style={{ marginTop: 8 }}>
            <Collapse.Item name="upload" header={T.uploadInstead} extra={<span className="muted" style={{ fontSize: 12 }}>{T.uploadInsteadHint}</span>}>
              <UploadInput f={f} value={value} onChange={onChange} onBusy={onBusy} />
            </Collapse.Item>
          </Collapse>
        </div>
      </Field>
    );
  }
  const computed = src.kind !== 'missing_pose' && src.kind !== 'upload';
  return (
    <Field label={f.title} required={f.required} extra={computed ? T.overrides : f.description} error={error}>
      {computed ? (
        <div className="muted" style={{ fontSize: 12, marginBottom: 4 }} data-testid="eef-trajectory-source">
          {src.kind === 'generate' ? (src.declaration ? T.byDeclaration : T.fromData) : (T.source[src.kind] ?? src.kind)}
        </div>
      ) : null}
      <UploadInput f={f} value={value} onChange={onChange} onBusy={onBusy} />
    </Field>
  );
}

/** Reminders under a module with a model switch (design doc 25 §4.3): no model chosen - its model part will be
 * missing; switched off without a gripper reference - the third-person cameras are not assessed. */
function ModelReminder({ m, params, fields, vlmChosen }: { m: ModuleSpec; params: Record<string, unknown> | undefined; fields: ParamField[]; vlmChosen: boolean }) {
  if (!m.vlm_switch) return null;
  if (asksModel(m, params)) {
    return vlmChosen ? null : <Alert type="info" style={{ marginTop: 8 }} content={zh.taskForm.eefNoModel} data-testid={`model-reminder-${m.id}`} />;
  }
  const reference = fields.filter((f) => f.choiceGroup?.id === 'gripper_reference');
  if (!reference.length || reference.some((f) => params?.[f.key])) return null;
  return <Alert type="warning" style={{ marginTop: 8 }} content={zh.taskForm.eefOffNoReference} data-testid={`model-reminder-${m.id}`} />;
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
  onUploadBusy,
  onRerun,
}: {
  v: FormValues;
  set: (patch: FormPatch) => void;
  errors: Errors;
  registry: ModuleRegistry | undefined;
  preflight: PreflightResult | null;
  embodimentOptions: string[];
  /** A file of `<module>.<param>` started or stopped uploading (the form holds its submit meanwhile). */
  onUploadBusy?: (key: string, busy: boolean) => void;
  /** The preflight taken again (after the dataset's declaration was completed elsewhere). */
  onRerun?: () => void;
}) {
  const specs = (registry?.modules ?? []).filter((m) => v.modules.includes(m.id));
  const active = activeModules(v);
  const needing = specs.filter((m) => active.includes(m.id) && embodimentHint(preflight, m.id));
  const withParams = specs.filter((m) => active.includes(m.id) && moduleParamFields(m, preflight).length);
  const skipped = specs.filter((m) => v.skipped.includes(m.id));
  // Against the form as it is then: an upload can finish after another field (or upload) changed.
  const setParam = (mod: string, key: string, value: unknown) =>
    set((prev) => ({ params: { ...prev.params, [mod]: { ...(prev.params[mod] ?? {}), [key]: value } } }));
  const options = embodimentOptions.length ? embodimentOptions : needing.flatMap((m) => embodimentHint(preflight, m.id)?.options ?? []);
  const renderEntry = (mod: string, entry: FieldOrGroup) => {
    const a = availabilityOf(preflight, mod);
    if ('field' in entry && entry.field.key === 'trajectory_json' && a?.trajectory_source)
      return (
        <TrajectoryField
          key={entry.field.key}
          f={entry.field}
          a={a}
          datasetId={v.datasetId || null}
          value={v.params[mod]?.[entry.field.key]}
          error={errors[`params.${mod}.${entry.field.key}`]}
          onChange={(x) => setParam(mod, entry.field.key, x)}
          onBusy={(b) => onUploadBusy?.(`${mod}.${entry.field.key}`, b)}
          onRerun={onRerun}
        />
      );
    return 'group' in entry ? (
      <ChoiceGroupField
        key={entry.group.id}
        mod={mod}
        group={entry.group}
        fields={entry.fields}
        values={v.params[mod] ?? {}}
        errors={errors}
        onChange={(key, x) => setParam(mod, key, x)}
        onBusy={(key, b) => onUploadBusy?.(`${mod}.${key}`, b)}
      />
    ) : (
      <Field key={entry.field.key} label={entry.field.title} required={entry.field.required} extra={entry.field.description} error={errors[`params.${mod}.${entry.field.key}`]}>
        <ParamInput
          f={entry.field}
          value={v.params[mod]?.[entry.field.key]}
          error={errors[`params.${mod}.${entry.field.key}`]}
          onChange={(x) => setParam(mod, entry.field.key, x)}
          onBusy={(b) => onUploadBusy?.(`${mod}.${entry.field.key}`, b)}
        />
      </Field>
    );
  };

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

      {/* one sub-card a module, its name a small title (sixth round) */}
      {withParams.map((m) => (
        <Card
          key={m.id}
          size="small"
          className="module-params"
          data-testid={`params-${m.id}`}
          title={
            <Typography.Title heading={6} className="module-params-title">
              {m.name_zh}
            </Typography.Title>
          }
        >
          {groupFields(moduleParamFields(m, preflight).filter((f) => !f.advanced)).map((entry) => renderEntry(m.id, entry))}
          <ModelReminder m={m} params={v.params[m.id]} fields={moduleParamFields(m, preflight)} vlmChosen={Boolean(v.vlmBackend && v.vlmModel)} />
          {/* the judgement lines its findings are drawn with (registry 2.1 x-advanced, design doc 17 §1.3): folded away */}
          {moduleParamFields(m, preflight).some((f) => f.advanced) ? (
            <Collapse bordered={false} className="advanced-lines" data-testid={`advanced-${m.id}`}>
              <Collapse.Item name="lines" header={zh.taskForm.advancedLines} extra={<span className="muted" style={{ fontSize: 12 }}>{zh.taskForm.advancedLinesHint}</span>}>
                {groupFields(moduleParamFields(m, preflight).filter((f) => f.advanced)).map((entry) => renderEntry(m.id, entry))}
              </Collapse.Item>
            </Collapse>
          ) : null}
        </Card>
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
