import { Alert, Button, Checkbox, Grid, Input, Message, Select, Space, Spin, Table, Tag } from '@arco-design/web-react';
import type { ColumnProps } from '@arco-design/web-react/es/Table';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { api, idempotencyKey, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import type { McapProbe, McapProbeRequest, McapTopic, VizMapping, VizTemplate } from '../../api/types';
import { readText } from '../../api/uploads';
import {
  canBeCamera,
  canBeDepth,
  canBeSeries,
  depthOf,
  exportName,
  mappingJson,
  PATH_RE,
  seriesOf,
  setDepthPair,
  setFields,
  setName,
  setPair,
  setSegments,
  setSmart,
  setTask,
  setTimeline,
  setUse,
  summarize,
  TIMELINE_SOURCES,
  topicsOf,
  usageOf,
  validateMapping,
  warningsOf,
  type MappingProblem,
  type SeriesRole,
  type TimelineSource,
  type TopicLike,
  type TopicUse,
} from '../../lib/vizMapping';
import { zh } from '../../locales/zh';
import './mcap.css';

const { Row, Col } = Grid;

export const vizTemplatesKey = ['viz-templates'] as const;

type ProbeInput = McapProbeRequest['input'];

/** A row of the table: a probed topic, or one only the mapping names (the file lacks it). */
interface Row extends TopicLike {
  key: string;
  probed: McapTopic | null;
}

/** The 用途 select's value: a use, a curve with its role. */
function selectValueOf(m: VizMapping, topic: string): string {
  const u = usageOf(m, topic);
  return u === 'series' ? `series:${seriesOf(m, topic)?.role ?? 'other'}` : u;
}

const fieldText = (t: McapTopic | null): string => {
  const f = t?.fields ?? [];
  if (!f.length) return '';
  const shown = f.slice(0, 6).map((x) => zh.mcap.fieldSize(x.path, x.size));
  return f.length > 6 ? `${shown.join(' · ')} …` : shown.join(' · ');
};

/** A path input that says when what is typed is not a C7 path. */
function PathInput({ value, onChange, placeholder, label }: { value: string; onChange: (v: string) => void; placeholder: string; label: string }) {
  const bad = value !== '' && !PATH_RE.test(value);
  return <Input size="small" className="mono" value={value} status={bad ? 'error' : undefined} placeholder={placeholder} aria-label={label} title={bad ? zh.mcap.fieldsBad(value) : undefined} onChange={onChange} />;
}

/** The table: one row per topic, what it is used as, its name and its fields. */
function MappingTable({ probe, value, onChange }: { probe: McapProbe; value: VizMapping; onChange: (m: VizMapping) => void }) {
  const rows: Row[] = useMemo(() => {
    const probed = probe.topics.map((t) => ({ ...t, key: t.topic, probed: t }));
    const have = new Set(probe.topics.map((t) => t.topic));
    const extra = topicsOf(value)
      .filter((t) => !have.has(t))
      .map((t) => ({ topic: t, key: t, probed: null, schema: cameraSchema(value, t) }));
    return [...probed, ...extra];
  }, [probe.topics, value]);
  const columns: ColumnProps<Row>[] = [
    {
      title: zh.mcap.colTopic,
      dataIndex: 'topic',
      width: 230,
      render: (_: unknown, r) => (
        <div>
          <div className="mono mcap-topic">{r.topic}</div>
          {!r.probed ? <div className="mcap-bad">{zh.mcap.missingTopic}</div> : null}
        </div>
      ),
    },
    {
      title: zh.mcap.colSchema,
      dataIndex: 'schema',
      width: 190,
      render: (_: unknown, r) => (
        <div>
          <div className="mono mcap-schema">{r.schema ?? '—'}</div>
          {r.probed ? <div className="muted mcap-small">{zh.mcap.rate(r.probed.rate_hz, r.probed.count)}</div> : null}
        </div>
      ),
    },
    {
      title: zh.mcap.colUse,
      dataIndex: 'use',
      width: 132,
      render: (_: unknown, r) => {
        const current = selectValueOf(value, r.topic);
        const options = [
          { value: 'camera', label: zh.mcap.use.camera, disabled: !canBeCamera(r) },
          { value: 'depth', label: zh.mcap.use.depth, disabled: !canBeDepth(r) },
          ...(['action', 'state', 'other'] as const).map((role) => ({ value: `series:${role}`, label: zh.mcap.use[role], disabled: !canBeSeries(r) })),
          { value: 'task', label: zh.mcap.use.task },
          { value: 'segments', label: zh.mcap.use.segments },
          { value: 'ignore', label: zh.mcap.use.ignore },
          ...(current === 'unmapped' || !r.probed ? [{ value: 'unmapped', label: zh.mcap.use.unmapped }] : []),
        ];
        return (
          <Select
            size="small"
            value={current}
            options={options}
            aria-label={zh.mcap.useAria(r.topic)}
            data-testid={`mcap-use-${r.topic}`}
            onChange={(v: string) => {
              const [use, role] = v.split(':') as [TopicUse, SeriesRole | undefined];
              onChange(setUse(value, r, use, role));
            }}
          />
        );
      },
    },
    {
      title: zh.mcap.colName,
      dataIndex: 'name',
      width: 150,
      render: (_: unknown, r) => {
        const use = usageOf(value, r.topic);
        if (use === 'camera' || use === 'depth' || use === 'series') {
          const entry = use === 'camera' ? value.cameras.find((c) => c.topic === r.topic) : use === 'depth' ? depthOf(value, r.topic) : seriesOf(value, r.topic);
          return <Input size="small" value={entry?.name ?? ''} placeholder={zh.mcap.namePlaceholder} aria-label={zh.mcap.nameAria(r.topic)} status={entry?.name ? undefined : 'error'} onChange={(v) => onChange(setName(value, r.topic, v))} />;
        }
        return <span className="muted">{use === 'task' || use === 'segments' ? zh.mcap.use[use] : '—'}</span>;
      },
    },
    {
      title: zh.mcap.colFields,
      dataIndex: 'fields',
      render: (_: unknown, r) => <FieldsCell row={r} value={value} onChange={onChange} />,
    },
  ];
  return (
    <Table
      className="mcap-tbl"
      rowKey="key"
      size="small"
      pagination={false}
      border={false}
      columns={columns}
      data={rows}
      rowClassName={(r: Row) => (['ignore', 'unmapped'].includes(usageOf(value, r.topic)) ? 'mcap-idle' : '')}
      data-testid="mcap-table"
    />
  );
}

function cameraSchema(m: VizMapping, topic: string): string | null {
  return m.cameras.find((c) => c.topic === topic)?.schema ?? depthOf(m, topic)?.schema ?? seriesOf(m, topic)?.schema ?? null;
}

/**
 * 字段 / 说明: the picture of a camera; a depth's picture and the camera it is drawn over; a curve's
 * fields, pairing and layout; the text fields of a task or segment topic.
 */
function FieldsCell({ row, value, onChange }: { row: Row; value: VizMapping; onChange: (m: VizMapping) => void }) {
  const use = usageOf(value, row.topic);
  const notes = row.probed?.notes ?? [];
  let body: ReactNode = <span className="muted mcap-small">{fieldText(row.probed) || '—'}</span>;
  if (use === 'camera') {
    const img = row.probed?.image;
    body = <span className="mcap-small">{img ? zh.mcap.image(img.codec, img.width, img.height) : '—'}</span>;
  } else if (use === 'depth') {
    const d = depthOf(value, row.topic)!;
    const img = row.probed?.image;
    // the picture, then the camera it is drawn over (a narrow column: one under the other)
    body = (
      <div className="mcap-series">
        <span className="mcap-small">{img ? zh.mcap.image(img.codec, img.width, img.height) : '—'}</span>
        <div className="mcap-depth-pair">
          <span className="muted mcap-small">{zh.mcap.depthPair}</span>
          <Select
            size="mini"
            allowClear
            value={d.pair_with ?? undefined}
            placeholder={zh.mcap.depthPairNone}
            aria-label={zh.mcap.depthPairAria(row.topic)}
            options={value.cameras.map((c) => ({ label: c.topic, value: c.topic }))}
            onChange={(v?: string) => onChange(setDepthPair(value, row.topic, v ?? null))}
          />
        </div>
      </div>
    );
  } else if (use === 'series') {
    const s = seriesOf(value, row.topic)!;
    const opposite = s.role === 'state' ? 'action' : s.role === 'action' ? 'state' : null;
    const partners = opposite ? value.series.filter((x) => x.role === opposite && x.topic !== row.topic) : [];
    const known = (row.fields ?? []).map((f) => ({ label: zh.mcap.fieldSize(f.path, f.size), value: f.path }));
    const extra = (s.fields ?? []).filter((f) => !known.some((k) => k.value === f)).map((f) => ({ label: f, value: f }));
    body = (
      <div className="mcap-series">
        <Select
          size="small"
          mode="multiple"
          allowCreate
          value={s.fields ?? []}
          placeholder={zh.mcap.fieldsPlaceholder}
          options={[...known, ...extra]}
          aria-label={zh.mcap.fieldsAria(row.topic)}
          data-testid={`mcap-fields-${row.topic}`}
          onChange={(v: string[]) => {
            const bad = v.find((p) => !PATH_RE.test(p));
            if (bad) Message.warning(zh.mcap.fieldsBad(bad));
            else onChange(setFields(value, row.topic, v));
          }}
        />
        <Space size={8} wrap>
          {opposite && (partners.length || s.pair_with) ? (
            <Space size={4}>
              <span className="muted mcap-small">{zh.mcap.pairWith}</span>
              <Select
                size="mini"
                allowClear
                style={{ width: 170 }}
                value={s.pair_with ?? undefined}
                placeholder={zh.mcap.pairNone}
                aria-label={zh.mcap.pairAria(row.topic)}
                options={partners.map((x) => ({ label: x.topic, value: x.topic }))}
                onChange={(v?: string) => onChange(setPair(value, row.topic, v ?? null))}
              />
            </Space>
          ) : null}
          <Checkbox checked={s.smart !== false} onChange={(c: boolean) => onChange(setSmart(value, row.topic, c))}>
            <span className="mcap-small">{zh.mcap.smart}</span>
          </Checkbox>
        </Space>
      </div>
    );
  } else if (use === 'task' && value.task && 'topic' in value.task) {
    body = <PathInput value={value.task.field ?? ''} placeholder={zh.mcap.taskField} label={zh.mcap.taskField} onChange={(v) => onChange(setTask(value, v ? { topic: row.topic, field: v } : { topic: row.topic }))} />;
  } else if (use === 'segments' && value.segments && 'topic' in value.segments) {
    const seg = value.segments;
    const set = (k: 'start_field' | 'end_field' | 'label_field', v: string) => onChange(setSegments(value, { ...seg, [k]: v }));
    body = (
      <Space size={4}>
        <PathInput value={seg.start_field} placeholder={zh.mcap.segStart} label={zh.mcap.segStart} onChange={(v) => set('start_field', v)} />
        <PathInput value={seg.end_field} placeholder={zh.mcap.segEnd} label={zh.mcap.segEnd} onChange={(v) => set('end_field', v)} />
        <PathInput value={seg.label_field} placeholder={zh.mcap.segLabel} label={zh.mcap.segLabel} onChange={(v) => set('label_field', v)} />
      </Space>
    );
  }
  return (
    <div>
      {body}
      {notes.map((n) => (
        <div key={n} className="mcap-note">
          {n}
        </div>
      ))}
    </div>
  );
}

/** 时间轴 / 帧号基准 / 任务描述来源 / 分段标注来源. */
function SourceSelects({ probe, value, onChange }: { probe: McapProbe; value: VizMapping; onChange: (m: VizMapping) => void }) {
  const source = value.timeline?.source ?? 'log_time';
  const textTopics = probe.topics.map((t) => t.topic).filter((t) => !['camera', 'depth', 'series'].includes(usageOf(value, t)));
  const metaKeys = new Map<string, string>();
  for (const rec of Object.values(probe.metadata)) for (const [k, v] of Object.entries(rec)) if (!metaKeys.has(k)) metaKeys.set(k, v);
  const task = value.task;
  const taskValue = !task ? '' : 'metadata_key' in task ? `meta:${task.metadata_key}` : `topic:${task.topic}`;
  const seg = value.segments;
  const segValue = !seg ? '' : 'attachment' in seg ? `att:${seg.attachment}` : `topic:${seg.topic}`;
  return (
    <Row gutter={12} className="mcap-sources">
      <Col span={6}>
        <div className="mcap-label">{zh.mcap.timeline}</div>
        <Select size="small" value={source} aria-label={zh.mcap.timeline} options={TIMELINE_SOURCES.map((s) => ({ value: s, label: zh.mcap.timelineSource[s] }))} onChange={(v: TimelineSource) => onChange(setTimeline(value, { source: v, ...(v === 'message_timestamp' ? { timestamp_field: value.timeline?.timestamp_field ?? 'timestamp' } : {}) }))} />
        {source === 'message_timestamp' ? (
          <div style={{ marginTop: 6 }}>
            <PathInput value={value.timeline?.timestamp_field ?? ''} placeholder={zh.mcap.timestampField} label={zh.mcap.timestampField} onChange={(v) => onChange(setTimeline(value, { timestamp_field: v }))} />
          </div>
        ) : null}
      </Col>
      <Col span={6}>
        <div className="mcap-label">{zh.mcap.frameRef}</div>
        <Select
          size="small"
          value={value.timeline?.frame_reference ?? ''}
          aria-label={zh.mcap.frameRef}
          options={[{ value: '', label: zh.mcap.frameRefAuto }, ...[...value.series, ...value.cameras].map((x) => ({ value: x.topic, label: x.topic }))]}
          onChange={(v: string) => onChange(setTimeline(value, { frame_reference: v || null }))}
        />
      </Col>
      <Col span={6}>
        <div className="mcap-label">{zh.mcap.taskSource}</div>
        <Select
          size="small"
          value={taskValue}
          aria-label={zh.mcap.taskSource}
          options={[
            { value: '', label: zh.mcap.taskNone },
            ...[...metaKeys].map(([k, v]) => ({ value: `meta:${k}`, label: zh.mcap.taskMeta(k, v) })),
            ...textTopics.map((t) => ({ value: `topic:${t}`, label: zh.mcap.taskTopic(t) })),
          ]}
          onChange={(v: string) => onChange(setTask(value, !v ? null : v.startsWith('meta:') ? { metadata_key: v.slice(5) } : { topic: v.slice(6) }))}
        />
      </Col>
      <Col span={6}>
        <div className="mcap-label">{zh.mcap.segSource}</div>
        <Select
          size="small"
          value={segValue}
          aria-label={zh.mcap.segSource}
          options={[
            { value: '', label: zh.mcap.segNone },
            ...textTopics.map((t) => ({ value: `topic:${t}`, label: zh.mcap.segTopic(t) })),
            ...probe.attachments.map((a) => ({ value: `att:${a.name}`, label: zh.mcap.segAttachment(a.name) })),
          ]}
          onChange={(v: string) => {
            if (!v) return onChange(setSegments(value, null));
            if (v.startsWith('att:')) return onChange(setSegments(value, { attachment: v.slice(4) }));
            const t = probe.topics.find((x) => x.topic === v.slice(6));
            onChange(t ? setUse(value, t, 'segments') : value);
          }}
        />
      </Col>
    </Row>
  );
}

/** The summary row: counts, then what would be missed (the probe's own warnings too: the check reader's gaps). */
function Summary({ probe, value, shown }: { probe: McapProbe; value: VizMapping; shown: readonly string[] }) {
  const s = summarize(value, probe);
  const warn = warningsOf(s);
  const text: Record<string, string> = {
    no_camera: zh.mcap.warn.no_camera,
    no_series: zh.mcap.warn.no_series,
    no_anchor: zh.mcap.warn.no_anchor,
    missing: zh.mcap.warn.missing(s.missing.join('、')),
    unmapped: zh.mcap.warn.unmapped(s.unmapped.length),
  };
  return (
    <div className="mcap-sum" data-testid="mcap-summary">
      <div className="mcap-counts">
        <span>{zh.mcap.sumCameras(s.cameras)}</span>
        {s.depths ? <span>{zh.mcap.sumDepths(s.depths)}</span> : null}
        <span>{zh.mcap.sumSeries(s.series, s.action, s.state, s.other)}</span>
        <span>{zh.mcap.sumTask(s.task)}</span>
        <span>{zh.mcap.sumSegments(s.segments)}</span>
        <span>{zh.mcap.sumIgnored(s.ignored)}</span>
      </div>
      {warn.map((w) => (
        <div key={w} className={w === 'unmapped' ? 'mcap-info' : 'mcap-note'} data-testid={`mcap-warn-${w}`}>
          {text[w]}
        </div>
      ))}
      {probe.warnings
        .filter((w) => !shown.includes(w.message))
        .map((w, i) => (
          <div key={`${w.code}-${i}`} className="mcap-note" data-testid={`mcap-probe-warn-${w.code}`}>
            {w.message}
          </div>
        ))}
    </div>
  );
}

/** 另存为模版: the mapping into the site's template library (POST /viz/templates, names unique). */
function SaveAsTemplate({ value, onDone }: { value: VizMapping; onDone: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState(value.name ?? '');
  const [desc, setDesc] = useState('');
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (!name.trim()) return void Message.warning(zh.errors.required(zh.mcap.saveAsName));
    setBusy(true);
    try {
      const t = await unwrap(
        api().POST('/viz/templates', {
          params: { header: { 'Idempotency-Key': idempotencyKey() } },
          body: { name: name.trim(), ...(desc.trim() ? { description: desc.trim() } : {}), mapping: { ...value, name: name.trim() } },
        }),
      );
      Message.success(zh.mcap.savedAs(t.name));
      void qc.invalidateQueries({ queryKey: vizTemplatesKey });
      onDone();
    } catch (e) {
      Message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Space className="mcap-saveas" wrap data-testid="mcap-saveas">
      <Input size="small" style={{ width: 240 }} value={name} maxLength={128} placeholder={zh.mcap.saveAsName} aria-label={zh.mcap.saveAsName} onChange={setName} />
      <Input size="small" style={{ width: 280 }} value={desc} maxLength={500} placeholder={zh.mcap.saveAsDesc} aria-label={zh.mcap.saveAsDesc} onChange={setDesc} />
      <Button size="small" type="primary" loading={busy} onClick={() => void save()}>
        {zh.mcap.saveAsOk}
      </Button>
    </Space>
  );
}

function download(m: VizMapping): void {
  const url = URL.createObjectURL(new Blob([mappingJson(m)], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = exportName(m);
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export type McapConfigStatus = 'probing' | 'ready' | 'failed';

/**
 * 「mcap 配置」(design doc 18 §6.4, F13.7): probe the episode files (POST /viz/mcap-probe), draft a
 * mapping with the template that fits (or the one picked), and let a person confirm it in a table;
 * the mapping is the parent's (`value`), which saves it - with the registration in the add drawer,
 * as a new version in the dataset's drawer. `saved` is the confirmed mapping the table starts from.
 */
export function McapConfig({
  input,
  saved = null,
  value,
  onChange,
  onStatus,
  shownWarnings = [],
}: {
  input: ProbeInput;
  saved?: { mapping: VizMapping; version: number } | null;
  value: VizMapping | null;
  onChange: (m: VizMapping | null) => void;
  onStatus?: (s: McapConfigStatus) => void;
  /** warnings the caller already shows (the dataset drawer's, for the confirmed version): not repeated */
  shownWarnings?: readonly string[];
}) {
  const templates = useQuery({ queryKey: vizTemplatesKey, queryFn: () => unwrap(api().GET('/viz/templates')), staleTime: 60_000 });
  const [template, setTemplate] = useState('');
  const [nonce, setNonce] = useState(0);
  const [imported, setImported] = useState<string | null>(null);
  const [problems, setProblems] = useState<{ file: string; list: MappingProblem[] } | null>(null);
  const [savingAs, setSavingAs] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const inputKey = JSON.stringify(input);
  const probe = useQuery({
    queryKey: ['mcap-probe', inputKey, template, nonce],
    queryFn: () => unwrap(api().POST('/viz/mcap-probe', { body: { input, ...(template ? { template } : {}) } })),
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
  });
  // A draft replaces the table when the address or the template changes; 重新探测 keeps the edits.
  // The dataset's drawer starts from the confirmed mapping instead of the first draft.
  const applied = useRef<string | null>(null);
  const [fromSaved, setFromSaved] = useState(false);
  const change = useRef(onChange);
  change.current = onChange;
  useEffect(() => {
    if (!probe.data) return;
    const k = `${inputKey}|${template}`;
    if (applied.current === k) return;
    const first = applied.current === null;
    applied.current = k;
    const keep = first && saved !== null && !template;
    setFromSaved(keep);
    change.current(keep ? saved.mapping : probe.data.draft);
  }, [probe.data, inputKey, template, saved]);
  const status: McapConfigStatus = probe.isError ? 'failed' : probe.data && value ? 'ready' : 'probing';
  const statusRef = useRef(onStatus);
  statusRef.current = onStatus;
  useEffect(() => {
    statusRef.current?.(status);
  }, [status]);

  const importFile = async (file: File | undefined) => {
    if (!file || !probe.data) return;
    if (fileInput.current) fileInput.current.value = '';
    let raw: unknown;
    try {
      raw = JSON.parse(await readText(file));
    } catch {
      setProblems({ file: file.name, list: [{ field: '<root>', problem: zh.mcap.importNotJson(file.name) }] });
      return;
    }
    const list = validateMapping(raw, new Set(probe.data.topics.map((t) => t.topic)));
    if (list.length) {
      setProblems({ file: file.name, list });
      return;
    }
    setProblems(null);
    setImported(file.name);
    setFromSaved(false);
    onChange(raw as VizMapping);
  };

  const builtins = (templates.data?.items ?? []).filter((t: VizTemplate) => t.builtin);
  const team = (templates.data?.items ?? []).filter((t: VizTemplate) => !t.builtin);
  const p = probe.data;
  let matchLine: string | null = null;
  if (imported) matchLine = zh.mcap.imported(imported);
  else if (fromSaved && saved) matchLine = zh.mcap.keptSaved(saved.version);
  else if (p?.matched) matchLine = template ? zh.mcap.matchedPicked(p.matched.name, zh.mcap.coverage(p.matched.coverage)) : zh.mcap.matchedAuto(p.matched.name, zh.mcap.coverage(p.matched.coverage));
  else if (p) matchLine = zh.mcap.noMatch(p.draft.name ?? '—');

  return (
    <div className="mcap" data-testid="mcap-config">
      <div className="mcap-head">
        <span className="mcap-title">{zh.mcap.section}</span>
        <Tag size="small" color="arcoblue">
          {zh.mcap.onlyMcap}
        </Tag>
        <span className="muted mcap-small">{zh.mcap.sectionHint}</span>
      </div>
      <Space className="mcap-tools" wrap align="end">
        <div>
          <div className="mcap-label">{zh.mcap.template}</div>
          <Select
            style={{ width: 380 }}
            value={template}
            aria-label={zh.mcap.template}
            onChange={(v: string) => {
              setImported(null);
              setProblems(null);
              setTemplate(v);
            }}
            options={[
              { value: '', label: zh.mcap.auto },
              ...builtins.map((t) => ({ value: t.id, label: zh.mcap.builtin(t.name) })),
              ...team.map((t) => ({ value: t.id, label: zh.mcap.team(t.name) })),
            ]}
          />
        </div>
        <Button size="default" title={zh.mcap.reprobeTitle} loading={probe.isFetching} onClick={() => setNonce((n) => n + 1)}>
          {zh.mcap.reprobe}
        </Button>
        <Button disabled={!p} onClick={() => fileInput.current?.click()}>
          {zh.mcap.importJson}
        </Button>
        <Button disabled={!value} onClick={() => value && download(value)}>
          {zh.mcap.exportJson}
        </Button>
        <input ref={fileInput} type="file" accept=".json,application/json" style={{ display: 'none' }} aria-label={zh.mcap.importJson} onChange={(e) => void importFile(e.target.files?.[0])} />
      </Space>
      <p className="muted mcap-small" style={{ margin: '6px 0 0' }}>
        {zh.mcap.probeHint}
      </p>
      {problems ? (
        <Alert
          type="error"
          style={{ marginTop: 8 }}
          closable
          onClose={() => setProblems(null)}
          data-testid="mcap-import-problems"
          content={
            <div>
              {problems.list.length === 1 && problems.list[0].field === '<root>' ? problems.list[0].problem : zh.mcap.importInvalid(problems.file, problems.list.length)}
              {problems.list.length > 1 || problems.list[0]?.field !== '<root>' ? (
                <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                  {problems.list.slice(0, 8).map((x, i) => (
                    <li key={i}>
                      <span className="mono">{x.field}</span>：{x.problem}
                    </li>
                  ))}
                  {problems.list.length > 8 ? <li className="muted">{zh.mcap.moreProblems(problems.list.length - 8)}</li> : null}
                </ul>
              ) : null}
            </div>
          }
        />
      ) : null}
      {probe.isError ? (
        <Alert
          type="error"
          style={{ marginTop: 8 }}
          content={zh.mcap.probeFailed(errorMessage(probe.error))}
          action={
            <Button size="mini" onClick={() => setNonce((n) => n + 1)}>
              {zh.mcap.reprobe}
            </Button>
          }
        />
      ) : !p || !value ? (
        <div className="mcap-probing">
          <Spin size={16} /> <span className="muted">{zh.mcap.probing}</span>
        </div>
      ) : (
        <>
          <div className="mcap-match" data-testid="mcap-match">
            {matchLine}
            <span className="muted mcap-small" style={{ marginLeft: 12 }}>
              {zh.mcap.probed(p.file, p.files)}
            </span>
          </div>
          <Summary probe={p} value={value} shown={shownWarnings} />
          <MappingTable probe={p} value={value} onChange={onChange} />
          <SourceSelects probe={p} value={value} onChange={onChange} />
          <details className="mcap-json">
            <summary>{zh.mcap.json}</summary>
            <pre data-testid="mcap-json">{mappingJson(value)}</pre>
          </details>
          <Space className="mcap-foot" wrap>
            <Button size="small" onClick={() => setSavingAs((x) => !x)}>
              {zh.mcap.saveAs}
            </Button>
          </Space>
          {savingAs ? <SaveAsTemplate value={value} onDone={() => setSavingAs(false)} /> : null}
        </>
      )}
    </div>
  );
}
