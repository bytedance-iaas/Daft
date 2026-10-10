import { Alert, Button, Drawer, Input, InputNumber, Message, Modal, Select, Space, Spin, Table, Tabs, Tag, Tooltip } from '@arco-design/web-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, idempotencyKey, unwrap } from '../../api/client';
import { ApiError, errorMessage } from '../../api/errors';
import { readText } from '../../api/uploads';
import { qk } from '../../api/queries';
import type {
  Assurance,
  CameraCalibration,
  DatasetDeclaration,
  Declaration,
  DeclarationGripper,
  DeclarationJoints,
  DeclarationNote,
  DeclarationPose,
  Extrinsics,
  Intrinsics,
  ToolModel,
} from '../../api/types';
import { PageError } from '../../components/PageError';
import {
  applyTemplate,
  cameraRows,
  canGenerate,
  clone,
  declarationJson,
  declared,
  emptyDeclaration,
  FINGER_AXES,
  fxcxfycy,
  LAYOUTS,
  missingOf,
  MODELS,
  MOUNTS,
  parseDeclaration,
  setCamera,
  setSemantic,
  setTool,
  withFour,
  type CameraRow,
} from '../../lib/declaration';
import { zh } from '../../locales/zh';

const T = zh.declaration;
export const declarationKey = (id: string) => ['dataset', id, 'declaration'] as const;

/**
 * 「数据集声明」 of a registered dataset (design doc 25 §3, C7 dataset-declaration/1.0): the confirmed version, or
 * the draft the Daemon makes from the dataset; three layers - sources (an mcap mapping, edited in 「mcap 配置」),
 * what the pose / joint / gripper records mean, and the cameras' and the tool's calibration - each item with how
 * sure it is. Saving confirms a new version (PUT /datasets/{id}/declaration, checked against the dataset) and takes
 * the preflight again; tasks already started keep the version they froze.
 */
export function DeclarationDrawer({
  dataset,
  onClose,
  onMapping,
}: {
  dataset: { id: string; name: string } | null;
  onClose: () => void;
  onMapping?: () => void;
}) {
  return dataset ? <Opened key={dataset.id} dataset={dataset} onClose={onClose} onMapping={onMapping} /> : null;
}

function Opened({ dataset, onClose, onMapping }: { dataset: { id: string; name: string }; onClose: () => void; onMapping?: () => void }) {
  const qc = useQueryClient();
  const id = dataset.id;
  const doc = useQuery({
    queryKey: declarationKey(id),
    queryFn: () => unwrap(api().GET('/datasets/{id}/declaration', { params: { path: { id } } })),
  });
  const detail = useQuery({ queryKey: qk.dataset(id), queryFn: () => unwrap(api().GET('/datasets/{id}', { params: { path: { id } } })) });
  const templates = useQuery({ queryKey: ['viz', 'templates'], queryFn: () => unwrap(api().GET('/viz/templates')) });
  const d = doc.data;
  const [value, setValue] = useState<Declaration | null>(null);
  useEffect(() => {
    if (d && !value) setValue(clone(d.declaration ?? d.draft ?? emptyDeclaration()));
  }, [d, value]);
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<{ field: string; problem: string }[]>([]);
  const columns = useMemo(
    () => (detail.data?.preflight?.dataset?.features ?? []).filter((f) => !['video', 'image', 'string', 'bool'].includes(f.dtype)),
    [detail.data],
  );
  const unchanged = Boolean(d?.declaration && value && JSON.stringify(d.declaration) === JSON.stringify(value));
  const sources = (d?.cameras ?? []).map((c) => c.source);

  const save = async () => {
    if (!value) return;
    setBusy(true);
    setProblems([]);
    try {
      const saved = await unwrap(
        api().PUT('/datasets/{id}/declaration', {
          params: { path: { id }, header: { 'Idempotency-Key': idempotencyKey() } },
          body: { declaration: value },
        }),
      );
      Message.success(T.saved(saved.version));
      qc.setQueryData(declarationKey(id), saved);
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

  const fileInput = useRef<HTMLInputElement>(null);
  const importFile = async (f: File) => {
    try {
      const next = parseDeclaration(await readText(f));
      setValue(next);
      Message.success(T.imported(f.name));
    } catch (e) {
      Message.error(T.importInvalid(f.name, e instanceof Error ? e.message : String(e)));
    }
  };
  const exportFile = () => {
    if (!value) return;
    const url = URL.createObjectURL(new Blob([declarationJson(value)], { type: 'application/json' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `${dataset.name}.declaration.json`;
    a.click();
    URL.revokeObjectURL(url);
  };
  const saveTemplate = () => {
    if (!value) return;
    let name = '';
    Modal.confirm({
      title: T.saveAsTemplate,
      content: <Input data-testid="decl-template-name" placeholder={T.saveAsTemplate} onChange={(v) => (name = v)} />,
      onOk: async () => {
        const body = { name: name.trim() || dataset.name, mapping: { ...value, suspects: undefined } as Declaration };
        await unwrap(api().POST('/viz/templates', { params: { header: { 'Idempotency-Key': idempotencyKey() } }, body }));
        void qc.invalidateQueries({ queryKey: ['viz', 'templates'] });
      },
    });
  };
  const usable = (templates.data?.items ?? []).filter(
    (t) => t.mapping && 'schema_version' in t.mapping && t.mapping.schema_version === 'dataset-declaration/1.0' && ('calibration' in t.mapping || 'semantics' in t.mapping),
  );

  return (
    <Drawer
      width="min(1100px, 100vw)"
      title={T.drawerTitle(dataset.name)}
      visible
      onCancel={onClose}
      footer={
        <Space>
          <input
            ref={fileInput}
            type="file"
            accept=".json,application/json"
            style={{ display: 'none' }}
            data-testid="decl-import"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void importFile(f);
              e.target.value = '';
            }}
          />
          <Button onClick={() => fileInput.current?.click()}>{T.importJson}</Button>
          <Button disabled={!value} onClick={exportFile}>
            {T.exportJson}
          </Button>
          {usable.length ? (
            <Select
              placeholder={T.applyTemplate}
              style={{ width: 200 }}
              data-testid="decl-apply-template"
              onChange={(tid: string) => {
                const t = usable.find((x) => x.id === tid);
                if (t?.mapping && value) {
                  setValue(applyTemplate(value, t.mapping as Declaration));
                  Message.info(T.templateApplied(t.name));
                }
              }}
            >
              {usable.map((t) => (
                <Select.Option key={t.id} value={t.id}>
                  {t.name}
                </Select.Option>
              ))}
            </Select>
          ) : null}
          <Button disabled={!value} onClick={saveTemplate}>
            {T.saveAsTemplate}
          </Button>
          <Button onClick={onClose}>{zh.common.cancel}</Button>
          <Button type="primary" loading={busy} disabled={!value || unchanged} title={unchanged ? T.unchanged : undefined} data-testid="decl-save" onClick={() => void save()}>
            {T.save((d?.version ?? 0) + 1)}
          </Button>
        </Space>
      }
    >
      {doc.isError && !d ? (
        <PageError error={doc.error} onRetry={() => void doc.refetch()} />
      ) : !d || !value ? (
        <Spin style={{ display: 'block', margin: '60px auto' }} />
      ) : (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert type="info" content={T.hint} />
          {d.state === 'none' ? <Alert type="warning" content={T.draftNote} data-testid="decl-draft-note" /> : null}
          <Status d={d} value={value} sources={sources} />
          {problems.length ? (
            <Alert
              type="error"
              data-testid="decl-problems"
              content={problems.slice(0, 8).map((p) => (
                <div key={p.field}>
                  <span className="mono">{p.field}</span> {p.problem}
                </div>
              ))}
            />
          ) : null}
          <Tabs defaultActiveTab="cameras">
            <Tabs.TabPane key="sources" title={T.tabs.sources}>
              {d.format === 'mcap' ? (
                <Space>
                  <span>{T.sourcesMcap(value.name ?? null, d.version)}</span>
                  {onMapping ? (
                    <Button size="small" onClick={onMapping}>
                      {T.sourcesMcapEdit}
                    </Button>
                  ) : null}
                </Space>
              ) : (
                <span>{T.sourcesLerobot}</span>
              )}
            </Tabs.TabPane>
            <Tabs.TabPane key="semantics" title={T.tabs.semantics}>
              <Semantics value={value} onChange={setValue} columns={columns.map((c) => c.key)} />
            </Tabs.TabPane>
            <Tabs.TabPane key="cameras" title={T.tabs.cameras}>
              <Cameras value={value} onChange={setValue} rows={cameraRows(value, d.cameras)} columns={columns.map((c) => c.key)} />
            </Tabs.TabPane>
            <Tabs.TabPane key="tool" title={T.tabs.tool}>
              <Tool value={value} onChange={setValue} />
            </Tabs.TabPane>
            <Tabs.TabPane key="handheld" title={T.tabs.handheld}>
              <Handheld value={value} onChange={setValue} />
            </Tabs.TabPane>
            <Tabs.TabPane key="timing" title={T.tabs.timing}>
              <Timing value={value} />
            </Tabs.TabPane>
          </Tabs>
        </Space>
      )}
    </Drawer>
  );
}

function noteText(n: DeclarationNote): string {
  return T.missing[n.code] ?? n.code;
}

/** Whether the trajectory can be generated (live, from the form), what is unresolved, and the confirmed one's suspects. */
function Status({ d, value, sources }: { d: DatasetDeclaration; value: Declaration; sources: string[] }) {
  const kind = d.trajectory.kind;
  const generated = ['session', 'mcap_derive', 'dataset_file'].includes(kind);
  const missing = generated ? [] : missingOf(value, sources);
  const ok = generated || canGenerate(value, sources);
  return (
    <Space direction="vertical" size={4} data-testid="decl-status">
      <div>
        <b>{T.trajectoryTitle}：</b>
        {generated ? (
          <span>{T.trajectory[kind]}</span>
        ) : ok ? (
          <span style={{ color: 'var(--c-success)' }} data-testid="decl-can-generate">
            {T.trajectory.generate}
          </span>
        ) : (
          <span style={{ color: 'var(--c-warning)' }} data-testid="decl-missing">
            {T.trajectory.missing_declaration}
            {missing.map(noteText).join('、')}
          </span>
        )}
      </div>
      {d.unresolved.length ? (
        <div>
          <b>{T.unresolved}：</b>
          {d.unresolved.map((u) => `${noteText(u)}（${u.field}）`).join('；')}
        </div>
      ) : null}
      {d.suspects.length ? (
        <div data-testid="decl-suspects">
          <b>{T.suspects}：</b>
          {d.suspects.map((s) => `${T.suspectCodes[s.code] ?? s.code}：${s.message}`).join('；')}
        </div>
      ) : null}
    </Space>
  );
}

function Sure({
  item,
  onChange,
  compact = false,
}: {
  item: { assurance?: Assurance; assumptions?: { code: string; args?: Record<string, unknown> }[] } | null | undefined;
  onChange: (yes: boolean) => void;
  compact?: boolean;
}) {
  if (!item) return null;
  const a = item.assurance ?? 'declared';
  const notes = (item.assumptions ?? []).map((x) => (T.assumptions[x.code] ? T.assumptions[x.code](x.args ?? {}) : x.code));
  const tag = (
    <Tag color={a === 'declared' ? 'green' : 'orange'} size="small">
      {T.assurance[a] ?? a}
    </Tag>
  );
  return (
    <Space size={4} wrap>
      {compact && notes.length ? <Tooltip content={`${T.assumedBy}${notes.join('；')}`}>{tag}</Tooltip> : tag}
      <Button size="mini" type={a === 'declared' ? 'secondary' : 'outline'} onClick={() => onChange(a !== 'declared')}>
        {a === 'declared' ? T.confirmedItem : T.confirmItem}
      </Button>
      {!compact && notes.length ? (
        <span className="muted" style={{ fontSize: 12 }}>
          {T.assumedBy}
          {notes.join('；')}
        </span>
      ) : null}
    </Space>
  );
}

function ColumnSelect({ value, columns, onChange, testid }: { value: string | undefined; columns: string[]; onChange: (v: string) => void; testid?: string }) {
  const options = value && !columns.includes(value) ? [value, ...columns] : columns;
  return (
    <Select value={value} style={{ width: 320 }} showSearch data-testid={testid} onChange={onChange}>
      {options.map((c) => (
        <Select.Option key={c} value={c}>
          {c}
        </Select.Option>
      ))}
    </Select>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', gap: 12, alignItems: 'center', margin: '6px 0' }}>
      <span style={{ width: 140, flex: 'none' }} className="muted">
        {label}
      </span>
      <div>{children}</div>
    </div>
  );
}

function Semantics({ value, onChange, columns }: { value: Declaration; onChange: (d: Declaration) => void; columns: string[] }) {
  const sem = value.semantics ?? {};
  const pose = sem.pose ?? null;
  const joints = sem.joints ?? null;
  const grip = sem.gripper ?? null;
  const setPose = (patch: Partial<DeclarationPose>) => onChange(setSemantic(value, 'pose', { ...(pose as DeclarationPose), ...patch } as DeclarationPose));
  return (
    <div data-testid="decl-semantics">
      <h4>{T.pose}</h4>
      {pose ? (
        <>
          <Sure item={pose} onChange={(yes) => onChange(setSemantic(value, 'pose', declared(pose, yes)))} />
          {'key' in pose && pose.key !== undefined ? (
            <Row label={T.key}>
              <ColumnSelect value={pose.key} columns={columns} onChange={(v) => setPose({ key: v } as Partial<DeclarationPose>)} testid="decl-pose-key" />
            </Row>
          ) : (
            <Row label={T.topic}>
              <span className="mono">{'topic' in pose ? pose.topic : ''}</span>
            </Row>
          )}
          <Row label={T.layout}>
            <Select value={pose.layout} style={{ width: 320 }} onChange={(v) => setPose({ layout: v })}>
              {LAYOUTS.map((l) => (
                <Select.Option key={l} value={l}>
                  {T.layouts[l]}
                </Select.Option>
              ))}
            </Select>
          </Row>
          <Row label={T.positionUnit}>
            <Select value={pose.units.position} style={{ width: 120 }} onChange={(v) => setPose({ units: { ...pose.units, position: v } })}>
              <Select.Option value="m">m</Select.Option>
              <Select.Option value="mm">mm</Select.Option>
            </Select>
            <span style={{ margin: '0 12px' }} className="muted">
              {T.angleUnit}
            </span>
            <Select value={pose.units.angle ?? 'rad'} style={{ width: 120 }} onChange={(v) => setPose({ units: { ...pose.units, angle: v } })}>
              <Select.Option value="rad">rad</Select.Option>
              <Select.Option value="deg">deg</Select.Option>
            </Select>
          </Row>
          <Row label={T.frameId}>
            <Input value={pose.frame_id ?? ''} placeholder={T.frameIdHint} style={{ width: 320 }} data-testid="decl-pose-frame" onChange={(v) => setPose({ frame_id: v || null })} />
          </Row>
          <Row label={T.referenceFrame}>
            <Input value={pose.reference_frame} style={{ width: 320 }} onChange={(v) => setPose({ reference_frame: v || 'robot_base' })} />
          </Row>
        </>
      ) : (
        <span className="muted">{T.none_}</span>
      )}
      <h4>{T.joints}</h4>
      {joints ? (
        <>
          <Sure item={joints} onChange={(yes) => onChange(setSemantic(value, 'joints', declared(joints, yes)))} />
          <Row label={T.key}>
            <span className="mono">{'key' in joints ? joints.key : 'topic' in joints ? joints.topic : ''}</span>
          </Row>
          <Row label={T.robot}>
            <Select
              value={joints.robot}
              style={{ width: 200 }}
              onChange={(v) => onChange(setSemantic(value, 'joints', { ...joints, robot: v } as DeclarationJoints))}
            >
              {Object.entries(T.robots).map(([k, v]) => (
                <Select.Option key={k} value={k}>
                  {v}
                </Select.Option>
              ))}
            </Select>
          </Row>
        </>
      ) : (
        <span className="muted">{T.none_}</span>
      )}
      <h4>{T.gripper}</h4>
      {grip ? (
        <>
          <Sure item={grip} onChange={(yes) => onChange(setSemantic(value, 'gripper', declared(grip, yes)))} />
          <Row label={T.key}>
            <span className="mono">
              {'key' in grip ? grip.key : 'topic' in grip ? grip.topic : ''}
              {grip.index !== undefined ? `[${grip.index}]` : ''}
            </span>
          </Row>
          <Row label={T.closedFraction}>
            <Select
              value={typeof grip.closed_fraction === 'object' ? 'range' : grip.closed_fraction ?? 'identity'}
              style={{ width: 200 }}
              onChange={(v: string) =>
                onChange(
                  setSemantic(value, 'gripper', {
                    ...grip,
                    closed_fraction: v === 'range' ? { min: 0, max: 0.08 } : (v as 'identity' | 'one_minus'),
                  } as DeclarationGripper),
                )
              }
            >
              {Object.entries(T.closedFractions).map(([k, v]) => (
                <Select.Option key={k} value={k}>
                  {v}
                </Select.Option>
              ))}
            </Select>
          </Row>
        </>
      ) : (
        <span className="muted">{T.none_}</span>
      )}
    </div>
  );
}

function num(v: number | string | undefined | null): number | null {
  const n = typeof v === 'number' ? v : v === undefined || v === null || v === '' ? NaN : Number(v);
  return Number.isFinite(n) ? n : null;
}

function Cameras({ value, onChange, rows, columns }: { value: Declaration; onChange: (d: Declaration) => void; rows: CameraRow[]; columns: string[] }) {
  const set = (src: string, patch: Partial<CameraCalibration>) => onChange(setCamera(value, src, patch));
  return (
    <Table
      rowKey="source"
      pagination={false}
      size="small"
      scroll={{ x: 960 }}
      data={rows}
      data-testid="decl-cameras"
      columns={[
        {
          title: T.cameraCols.camera,
          render: (_: unknown, r: CameraRow) => (
            <Space direction="vertical" size={2}>
              <span className="mono">{r.name}</span>
              <span className="muted" style={{ fontSize: 12 }}>
                {r.width && r.height ? `${r.width}×${r.height}` : ''}
              </span>
              {r.calibration ? (
                <Input
                  size="mini"
                  addBefore={T.cameraCols.id}
                  style={{ width: 170 }}
                  value={r.calibration.camera_id ?? r.name}
                  data-testid={`decl-camera-id-${r.name}`}
                  onChange={(v) => set(r.source, { camera_id: v || undefined })}
                />
              ) : null}
              <Sure compact item={r.calibration} onChange={(yes) => r.calibration && set(r.source, declared(r.calibration, yes))} />
            </Space>
          ),
        },
        {
          title: T.cameraCols.mount,
          render: (_: unknown, r: CameraRow) => (
            <Space direction="vertical" size={4}>
              <Select
                value={r.calibration?.mount}
                placeholder={T.mountUnknown}
                style={{ width: 160 }}
                data-testid={`decl-mount-${r.name}`}
                onChange={(v: CameraCalibration['mount']) => set(r.source, { mount: v, ...(v === 'wrist' && !r.calibration?.owner ? { owner: 'arm' } : {}) })}
              >
                {MOUNTS.map((m) => (
                  <Select.Option key={m} value={m}>
                    {T.mounts[m]}
                  </Select.Option>
                ))}
              </Select>
              {r.calibration?.mount === 'wrist' ? (
                <Input size="mini" value={r.calibration.owner ?? ''} addBefore={T.cameraCols.owner} style={{ width: 160 }} onChange={(v) => set(r.source, { owner: v || null })} />
              ) : null}
            </Space>
          ),
        },
        {
          title: T.cameraCols.intrinsics,
          render: (_: unknown, r: CameraRow) => <IntrinsicsCell row={r} onChange={(intr) => set(r.source, { intrinsics: intr })} />,
        },
        {
          title: T.cameraCols.extrinsics,
          render: (_: unknown, r: CameraRow) => <ExtrinsicsCell row={r} columns={columns} onChange={(e) => set(r.source, { extrinsics: e })} />,
        },
        {
          title: T.cameraCols.state,
          render: (_: unknown, r: CameraRow) =>
            r.reason === null ? (
              <Tag color="green" size="small">
                {T.drawable}
              </Tag>
            ) : (
              <Tag color="orange" size="small" data-testid={`decl-reason-${r.name}`}>
                {T.reasons[r.reason] ?? r.reason}
              </Tag>
            ),
        },
      ]}
    />
  );
}

/** fx cx fy cy kept while a person types them (the declaration takes the four once they are all there). */
function IntrinsicsCell({ row, onChange }: { row: CameraRow; onChange: (i: Intrinsics | null) => void }) {
  const intr = row.calibration?.intrinsics ?? null;
  const [four, setFour] = useState<(number | null)[]>(() => fxcxfycy(intr) ?? [null, null, null, null]);
  if (!row.calibration) return <span className="muted">—</span>;
  return (
    <Space direction="vertical" size={4}>
      <Space size={4}>
        {four.map((x, i) => (
          <InputNumber
            key={i}
            size="mini"
            style={{ width: 86 }}
            value={x ?? undefined}
            placeholder={['fx', 'cx', 'fy', 'cy'][i]}
            data-testid={`decl-intr-${row.name}-${i}`}
            onChange={(v) => {
              const next = [...four];
              next[i] = num(v);
              setFour(next);
              const done = withFour(intr, next);
              if (done) onChange(done);
            }}
          />
        ))}
      </Space>
      <Select
        size="mini"
        value={intr?.model ?? 'pinhole'}
        style={{ width: 180 }}
        disabled={!intr}
        onChange={(m) => intr && onChange({ ...intr, model: m, coefficients: m === 'pinhole' ? [] : intr.coefficients })}
      >
        {MODELS.map((m) => (
          <Select.Option key={m} value={m}>
            {T.models[m]}
          </Select.Option>
        ))}
      </Select>
      {intr && intr.model !== 'pinhole' ? (
        <Input
          size="mini"
          addBefore={T.coefficients}
          defaultValue={(intr.coefficients ?? []).join(' ')}
          onBlur={(e) => onChange({ ...intr, coefficients: e.target.value.split(/[\s,]+/).filter(Boolean).map(Number) })}
        />
      ) : null}
    </Space>
  );
}

function ExtrinsicsCell({ row, columns, onChange }: { row: CameraRow; columns: string[]; onChange: (e: Extrinsics | null) => void }) {
  const ext = (row.calibration?.extrinsics ?? null) as Extrinsics | null;
  if (!row.calibration) return <span className="muted">—</span>;
  const mode = ext?.mode ?? 'none';
  return (
    <Space direction="vertical" size={4}>
      <Select
        size="mini"
        value={mode}
        style={{ width: 180 }}
        onChange={(m: string) =>
          onChange(
            m === 'none'
              ? null
              : m === 'column'
                ? ({ mode: 'column', key: columns.find((c) => c.includes('extrinsic')) ?? columns[0] ?? '', layout: 'xyz_rpy' } as Extrinsics)
                : m === 'static'
                  ? ({ mode: 'static', xyz_rpy: [0, 0, 0, 0, 0, 0] } as Extrinsics)
                  : ({ mode: 'camera_tcp', T_camera_tcp: [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]] } as Extrinsics),
          )
        }
      >
        <Select.Option value="none">{T.extrinsicNone}</Select.Option>
        {Object.entries(T.extrinsicModes).map(([k, v]) => (
          <Select.Option key={k} value={k}>
            {v}
          </Select.Option>
        ))}
      </Select>
      {ext?.mode === 'column' ? (
        <ColumnSelect value={ext.key} columns={columns} onChange={(k) => onChange({ ...ext, key: k })} />
      ) : ext?.mode === 'static' && 'xyz_rpy' in ext && ext.xyz_rpy ? (
        <Input
          size="mini"
          addBefore={T.xyzRpy}
          defaultValue={ext.xyz_rpy.join(' ')}
          onBlur={(e) => {
            const v = e.target.value.split(/[\s,]+/).filter(Boolean).map(Number);
            if (v.length === 6 && v.every(Number.isFinite)) onChange({ ...ext, xyz_rpy: v });
          }}
        />
      ) : ext?.mode === 'camera_tcp' ? (
        <Input.TextArea
          autoSize
          defaultValue={JSON.stringify(ext.T_camera_tcp)}
          onBlur={(e) => {
            try {
              const T4 = JSON.parse(e.target.value) as number[][];
              if (T4.length === 4) onChange({ ...ext, T_camera_tcp: T4 });
            } catch {
              /* left as it was */
            }
          }}
        />
      ) : null}
    </Space>
  );
}

function Tool({ value, onChange }: { value: Declaration; onChange: (d: Declaration) => void }) {
  const tool = value.calibration?.tool ?? null;
  if (!tool)
    return (
      <Space>
        <span className="muted">{T.noTool}</span>
        <Button size="small" onClick={() => onChange(setTool(value, { tcp_offset_m: [0, 0, 0], finger_axis: null, max_opening_m: null, axes: { from: 'tcp', length_m: 0.06 }, assurance: 'declared' }))}>
          {T.addTool}
        </Button>
      </Space>
    );
  const set = (patch: Partial<ToolModel>) => onChange(setTool(value, patch));
  const tcp = tool.tcp_offset_m ?? [0, 0, 0];
  return (
    <div data-testid="decl-tool">
      <Sure item={tool} onChange={(yes) => onChange(setTool(value, declared(tool, yes)))} />
      <Row label={T.toolModel}>
        <Input value={tool.model ?? ''} style={{ width: 200 }} onChange={(v) => set({ model: v || null })} />
      </Row>
      <Row label={T.tcpOffset}>
        <Space size={4}>
          {tcp.map((x, i) => (
            <InputNumber
              key={i}
              size="small"
              step={0.001}
              style={{ width: 100 }}
              value={x}
              data-testid={`decl-tcp-${i}`}
              onChange={(v) => {
                const next = [...tcp];
                next[i] = num(v) ?? 0;
                set({ tcp_offset_m: next });
              }}
            />
          ))}
        </Space>
      </Row>
      <Row label={T.fingerAxis}>
        <Select value={tool.finger_axis ?? undefined} allowClear style={{ width: 160 }} onChange={(v) => set({ finger_axis: v ?? null })}>
          {FINGER_AXES.map((a) => (
            <Select.Option key={a} value={a}>
              {T.fingerAxes[a]}
            </Select.Option>
          ))}
        </Select>
      </Row>
      <Row label={T.maxOpening}>
        <InputNumber size="small" step={0.001} style={{ width: 120 }} value={tool.max_opening_m ?? undefined} data-testid="decl-opening" onChange={(v) => set({ max_opening_m: num(v) })} />
      </Row>
    </div>
  );
}

function Handheld({ value, onChange }: { value: Declaration; onChange: (d: Declaration) => void }) {
  const hh = value.calibration?.handheld ?? null;
  const [text, setText] = useState(hh ? JSON.stringify(hh.calibration, null, 1) : '');
  if (!hh) return <span className="muted">{T.handheldNone}</span>;
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {hh.builtin ? <Alert type="warning" content={T.handheldBuiltin(hh.builtin)} /> : null}
      <span className="muted">{T.handheldJson}</span>
      <Input.TextArea
        autoSize={{ minRows: 8, maxRows: 24 }}
        className="mono"
        value={text}
        onChange={setText}
        onBlur={() => {
          try {
            const cal = JSON.parse(text) as Record<string, unknown>;
            const out = clone(value);
            out.calibration = { ...(out.calibration ?? {}), handheld: { ...hh, calibration: cal as { schema_version: 'umi-calibration/2' }, builtin: null } };
            onChange(out);
          } catch {
            /* the text stays to be fixed */
          }
        }}
      />
    </Space>
  );
}

function Timing({ value }: { value: Declaration }) {
  const clocks = value.timing?.source_clocks ?? [];
  if (!clocks.length) return <span className="muted">{T.timingNone}</span>;
  return (
    <Table
      rowKey="key"
      pagination={false}
      size="small"
      data={clocks}
      columns={[
        { title: T.clockCols.channel, dataIndex: 'channel' },
        { title: T.clockCols.key, dataIndex: 'key', render: (k: string) => <span className="mono">{k}</span> },
        { title: T.clockCols.unit, dataIndex: 'unit' },
      ]}
    />
  );
}
