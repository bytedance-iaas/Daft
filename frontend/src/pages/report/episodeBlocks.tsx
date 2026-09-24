// One block per module in the Episode tab (F6.2): this episode's reading of the module, in
// Chinese - facts, small tables, the task_success trail, the sync curves - never raw JSON. A module
// without a block of its own lists its details readably (fieldLabel + readable).
import { Button, Space, Table, Tag, Typography } from '@arco-design/web-react';
import { useQuery } from '@tanstack/react-query';
import type { ComponentType, ReactNode } from 'react';
import { api, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { qk } from '../../api/queries';
import type { ResultRecord, SyncCurves } from '../../api/types';
import { CHART_COLORS, Chart, lineOption } from '../../components/Chart';
import { LazyVisible } from '../../components/LazyVisible';
import { EefConclusion, EefCpuEvidence, EefCpuTable, EefWindows } from '../../features/eef/EefRecord';
import { judgementName, motionFacts, motionRows, syncBadge, syncRows, taskTrail, timestampFacts, violationRows, visualRows, type CameraScoreRow, type Fact, type SyncCameraRow } from '../../lib/episodeReadings';
import { fieldLabel, readable } from '../../lib/reportView';
import { fmt, num, signed, str } from '../../lib/sectionStats';
import { zh } from '../../locales/zh';

export interface BlockProps {
  taskId: string;
  rev: number;
  ep: number;
  record: ResultRecord;
  onEpisode: (ep: number) => void;
}

type Details = Record<string, unknown>;
const E = () => zh.episodeTab;
const details = (r: ResultRecord): Details => (r.details && typeof r.details === 'object' ? (r.details as Details) : {});

function Facts({ facts }: { facts: Fact[] }) {
  if (!facts.length) return null;
  return (
    <dl className="desc-grid">
      {facts.map((f) => (
        <div key={f.label} className={`desc-item${f.warn ? ' warn' : ''}`}>
          <dt>{f.label}</dt>
          <dd>{f.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Reason({ text, tone }: { text: string | null; tone?: 'warn' | 'bad' }) {
  if (!text) return null;
  return (
    <div className={`episode-reason${tone ? ` ${tone}` : ''}`}>
      <b>{E().task.reason}：</b>
      {text}
    </div>
  );
}

/** Keys a block did not show, readably (never JSON). */
function Rest({ d, shown }: { d: Details; shown: readonly string[] }) {
  const rest = Object.entries(d).filter(([k]) => !shown.includes(k));
  if (!rest.length) return null;
  return (
    <details className="episode-rest">
      <summary>{E().generic}</summary>
      <dl className="desc-grid">
        {rest.map(([k, v]) => (
          <div key={k} className="desc-item full">
            <dt>{fieldLabel(k)}</dt>
            <dd>{readable(v)}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

// ------------------------------------------------------------------------------ the modules

function TimestampBlock({ record }: BlockProps) {
  const d = details(record);
  const { facts, gaps } = timestampFacts(d);
  return (
    <>
      <Facts facts={facts} />
      <Reason text={str(d.reason)} tone={record.verdict === 'fail' ? 'bad' : undefined} />
      {gaps.length ? (
        <div className="episode-line">
          <b>{E().timestamp.gaps}：</b>
          {gaps.join('；')}
        </div>
      ) : null}
      {record.verdict === 'pass' && !gaps.length && !str(d.reason) ? <div className="muted">{E().timestamp.stable}</div> : null}
    </>
  );
}

function KinematicsBlock({ record }: BlockProps) {
  const d = details(record);
  const { rows, more } = violationRows(d);
  const C = E().kinematics.cols;
  const sustained = Array.isArray(d.sustained_out_of_limit) ? d.sustained_out_of_limit.length : 0;
  return (
    <>
      <Reason text={str(d.reason)} tone={record.verdict === 'abstain' ? 'warn' : undefined} />
      {str(d.profile) ? (
        <div className="episode-line">
          <b>{E().kinematics.profile}：</b>
          {str(d.profile)}
        </div>
      ) : null}
      {sustained ? <div className="episode-line warn">{`${E().kinematics.sustained}：${sustained}`}</div> : null}
      {rows.length ? (
        <>
          <div className="episode-line">{E().kinematics.count(rows.length + more)}</div>
          <Table
            rowKey="key"
            size="small"
            pagination={false}
            data={rows}
            data-testid="episode-violations"
            columns={[
              { title: C.type, dataIndex: 'type' },
              { title: C.joint, dataIndex: 'joint' },
              { title: C.frame, dataIndex: 'frame', align: 'right' },
              { title: C.value, dataIndex: 'value', align: 'right' },
              { title: C.limit, dataIndex: 'limit' },
            ]}
          />
          {more ? <div className="muted">{E().kinematics.more(more)}</div> : null}
        </>
      ) : !str(d.reason) ? (
        <div className="muted">{E().kinematics.none}</div>
      ) : null}
    </>
  );
}

function MotionBlock({ record }: BlockProps) {
  const d = details(record);
  const rows = motionRows(d);
  const C = E().motion.cols;
  return (
    <>
      {rows.length ? (
        <Table
          rowKey="key"
          size="small"
          pagination={false}
          data={rows}
          data-testid="episode-motion"
          columns={[
            { title: C.name, dataIndex: 'name', width: 120 },
            { title: C.value, dataIndex: 'value', width: 90, align: 'right' },
            { title: C.role, dataIndex: 'role', width: 100 },
            { title: C.note, dataIndex: 'note' },
          ]}
        />
      ) : null}
      <Facts facts={motionFacts(d)} />
      {d.same_source ? <div className="muted">{E().motion.sameSource}</div> : null}
      <Reason text={str(d.reason)} tone={record.verdict === 'abstain' ? 'warn' : undefined} />
    </>
  );
}

function VisualBlock({ record }: BlockProps) {
  const d = details(record);
  const rows = visualRows(d);
  const C = E().visual.cols;
  return (
    <>
      {rows.length ? (
        <Table
          rowKey="key"
          size="small"
          pagination={false}
          data={rows}
          data-testid="episode-visual"
          rowClassName={(r: CameraScoreRow) => (r.low ? 'row-warn' : '')}
          columns={[
            { title: C.camera, dataIndex: 'camera', render: (v: string) => <span className="mono">{v}</span> },
            { title: C.score, dataIndex: 'score', align: 'right' },
            { title: C.sharpness, dataIndex: 'sharpness', align: 'right' },
            { title: C.exposure, dataIndex: 'exposure', align: 'right' },
            { title: C.integrity, dataIndex: 'integrity', align: 'right' },
            { title: C.frozen, dataIndex: 'frozen', align: 'right' },
            { title: C.status, dataIndex: 'status' },
          ]}
        />
      ) : null}
      {str(d.worst_camera) ? <div className="muted">{E().visual.worst(str(d.worst_camera)!.split('.').pop() ?? '')}</div> : null}
    </>
  );
}

/** Picture motion and arm motion normalised (z-scores): their shapes are what is compared. */
function normalise(values: (number | null)[]): (number | null)[] {
  const xs = values.filter((v): v is number => v !== null);
  if (!xs.length) return values;
  const mean = xs.reduce((a, b) => a + b, 0) / xs.length;
  const sd = Math.sqrt(xs.reduce((a, b) => a + (b - mean) ** 2, 0) / xs.length) || 1;
  return values.map((v) => (v === null ? null : Number(((v - mean) / sd).toFixed(3))));
}

function SyncCurvesView({ curves }: { curves: SyncCurves }) {
  const S = E().sync;
  const tol = curves.lag_tol_s;
  const peaks = curves.cameras.filter((c) => c.peak).map((c, i) => ({ name: c.camera, x: c.peak!.lag_s, y: c.peak!.corr, color: CHART_COLORS[i % CHART_COLORS.length] }));
  const xcorr = lineOption(
    curves.cameras.map((c, i) => ({ name: c.camera, data: c.lags.map((l, k) => [l ?? 0, c.xcorr[k]] as [number, number | null]), color: CHART_COLORS[i % CHART_COLORS.length] })),
    { xName: S.lagAxis, band: [-tol, tol], points: peaks, xMin: -curves.window_s, xMax: curves.window_s },
  );
  return (
    <div className="sync-curves" data-testid="sync-curves">
      <div className="sync-curves-cams">
        {curves.cameras.map((c) => {
          const flow = normalise(c.flow);
          const speed = normalise(c.speed);
          return (
            <div key={c.camera}>
              <div className="section-sub mono">{c.camera}</div>
              <Chart
                height={150}
                summary={`${c.camera}：${S.flow} / ${S.speed}`}
                option={lineOption(
                  [
                    { name: S.flow, data: c.t.map((t, k) => [t ?? 0, flow[k]] as [number, number | null]), color: CHART_COLORS[0] },
                    { name: S.speed, data: c.t.map((t, k) => [t ?? 0, speed[k]] as [number, number | null]), color: CHART_COLORS[1] },
                  ],
                  { xName: S.timeAxis },
                )}
              />
            </div>
          );
        })}
      </div>
      <div>
        <div className="section-sub">{S.xcorr}</div>
        <Chart height={260} summary={`${S.xcorr}：${curves.cameras.map((c) => (c.peak ? S.peak(c.camera, signed(c.peak.lag_s), fmt(c.peak.corr)) : c.camera)).join('；')}`} option={xcorr} />
      </div>
    </div>
  );
}

function SyncCurvesPanel({ taskId, rev, ep }: { taskId: string; rev: number; ep: number }) {
  const q = useQuery({
    queryKey: qk.syncCurves(taskId, ep, rev),
    queryFn: () => unwrap(api().GET('/tasks/{id}/episodes/{index}/sync-curves', { params: { path: { id: taskId, index: ep }, query: { rev } } })),
    retry: false,
  });
  const S = E().sync;
  return (
    <div className="episode-sub">
      <div className="section-sub">
        {S.curves}
        <span className="muted">{S.curvesDesc}</span>
      </div>
      {q.isLoading ? <span className="muted">…</span> : q.data ? <SyncCurvesView curves={q.data} /> : <div className="muted" data-testid="sync-curves-missing">{errorMessage(q.error)}</div>}
    </div>
  );
}

function SyncBlock({ taskId, rev, ep, record }: BlockProps) {
  const d = details(record);
  const badge = syncBadge(d);
  const rows = syncRows(d);
  const C = E().sync.cols;
  return (
    <>
      <Space wrap>
        <Tag color={badge.color} data-testid="sync-badge">
          {badge.text}
        </Tag>
        {num(d.consensus_lag_s) !== null ? <span>{E().sync.consensus(signed(num(d.consensus_lag_s)))}</span> : null}
      </Space>
      <Reason text={str(d.reason)} />
      {rows.length ? (
        <Table
          rowKey="key"
          size="small"
          pagination={false}
          data={rows}
          data-testid="episode-sync"
          rowClassName={(r: SyncCameraRow) => (r.flagged ? 'row-warn' : '')}
          columns={[
            { title: C.camera, dataIndex: 'camera', width: 210, render: (v: string) => <span className="mono nowrap">{v}</span> },
            { title: C.lag, dataIndex: 'lag', width: 90, align: 'right' },
            { title: C.peak, dataIndex: 'peak', width: 90, align: 'right' },
            { title: C.zero, dataIndex: 'zero', width: 90, align: 'right' },
            { title: C.trusted, dataIndex: 'trusted', width: 64 },
            {
              title: C.diagnosis,
              dataIndex: 'label',
              render: (_: unknown, r: SyncCameraRow) => (
                <span>
                  {r.label ? <b>{r.label}：</b> : null}
                  {r.text}
                </span>
              ),
            },
          ]}
        />
      ) : null}
      <LazyVisible placeholder={<div style={{ height: 160 }} />}>
        <SyncCurvesPanel taskId={taskId} rev={rev} ep={ep} />
      </LazyVisible>
    </>
  );
}

function TaskBlock({ record }: BlockProps) {
  const d = details(record);
  const trail = taskTrail(d, record.verdict);
  const T = E().task;
  const completions = Array.isArray(d.completions) ? d.completions.map((v) => num(v)) : [];
  const videoEvidence = Array.isArray(d.video_evidence)
    ? d.video_evidence.filter((v): v is Details => !!v && typeof v === 'object') : [];
  const source = str(d.task_desc_source);
  return (
    <>
      <div className="section-sub">
        {T.trail}
        {judgementName(d) ? <Tag size="small">{judgementName(d)}</Tag> : null}
      </div>
      <div className="trail" data-testid="task-trail">
        {trail.map((s) => (
          <div key={s.layer} className={`trail-step${s.reached ? '' : ' off'}${s.tone ? ` ${s.tone}` : ''}`}>
            <div className="trail-layer">{s.layer}</div>
            <div className="trail-text">{s.text}</div>
          </div>
        ))}
      </div>
      <Reason text={str(d.reason)} tone={record.verdict === 'fail' ? 'bad' : record.verdict === 'abstain' ? 'warn' : undefined} />
      {d.input_mode === 'video' ? (
        <div className="episode-sub">
          <div className="section-sub">{T.videoAssessment}{num(d.video_completion) !== null ? T.videoCompletion(fmt((num(d.video_completion) ?? 0) * 100)) : ''}</div>
          {videoEvidence.map((e, i) => (
            <div key={i} className="episode-line">
              <Tag size="small">{T.videoEvidence(str(e.camera) ?? '', fmt(num(e.start_s)), fmt(num(e.end_s)))}</Tag>
              <span>{str(e.observation)}</span>
            </div>
          ))}
        </div>
      ) : null}
      {source || str(d.task_type) ? (
        <div className="muted">
          {[source ? T.intent(zh.sections.task.sourceNames[source] ?? source) : '', str(d.task_type) ? T.taskType[str(d.task_type)!] ?? str(d.task_type) : ''].filter(Boolean).join(' · ')}
        </div>
      ) : null}
      {completions.length > 1 ? (
        <div className="episode-sub" style={{ maxWidth: 520 }}>
          <div className="section-sub">
            {T.completion}
            <span className="muted">{T.completionDesc}</span>
          </div>
          <LazyVisible placeholder={<div style={{ height: 140 }} />}>
            <Chart height={140} summary={`${T.completion}：${completions.map((v) => fmt(v)).join('，')}`} option={lineOption([{ name: T.completion, data: completions.map((v, i) => [i + 1, v] as [number, number | null]) }], { legend: false })} />
          </LazyVisible>
        </div>
      ) : null}
    </>
  );
}

function DedupBlock({ record, onEpisode }: BlockProps) {
  const d = details(record);
  const dup = num(d.duplicate_of);
  if (dup === null) return <div className="muted">{E().dedup.none}</div>;
  return (
    <div className="episode-line bad" data-testid="duplicate-of">
      {E().dedup.dupOf}{' '}
      <Button type="text" size="mini" style={{ padding: 0 }} onClick={() => onEpisode(dup)}>
        {zh.report.episode(dup)}
      </Button>{' '}
      {E().dedup.dupOfSuffix}
    </div>
  );
}

function SkillBlock({ record }: BlockProps) {
  const d = details(record);
  const K = E().skill;
  const facts: Fact[] = [];
  if (str(d.family)) facts.push({ label: K.family, value: str(d.family)! });
  if (str(d.subskill)) facts.push({ label: K.subskill, value: str(d.subskill)! });
  if (str(d.caption)) facts.push({ label: K.caption, value: str(d.caption)! });
  if (str(d.grouping_text)) {
    const src = str(d.grouping_text_source);
    facts.push({ label: K.grouping, value: `${str(d.grouping_text)}${src ? K.groupingSource(zh.sections.task.sourceNames[src] ?? src) : ''}` });
  }
  return <Facts facts={facts} />;
}

/**
 * EEF–视频一致性 (D49, F5.12): the conclusion and why, the CPU's sub-item readings per camera, every
 * review window with the model's answer and the marked crops it was shown, and the CPU's own frames
 * (the same block as the adjudication card).
 */
function EefBlock({ taskId, record }: BlockProps) {
  const d = details(record);
  const overall = str(d.overall);
  return (
    <div data-testid="episode-eef">
      <Space wrap>
        <EefConclusion record={record} />
      </Space>
      <div className="episode-line muted">
        {E().eef.overall}：{overall ? zh.sections.eef.overall[overall] ?? zh.sections.eef.overall[`${overall}s`] ?? overall : '—'}
        {Array.isArray(d.reasons) && d.reasons.length ? `；${E().eef.reasons}：${d.reasons.map(String).join('、')}` : ''}
      </div>
      <EefCpuTable record={record} />
      <EefWindows taskId={taskId} record={record} />
      <EefCpuEvidence taskId={taskId} record={record} />
    </div>
  );
}

/** Any module: its details, labelled and readable. */
export function GenericBlock({ record }: BlockProps) {
  const d = details(record);
  const entries = Object.entries(d);
  if (!entries.length) return <div className="muted">—</div>;
  return (
    <dl className="desc-grid" data-testid="episode-generic">
      {entries.map(([k, v]) => (
        <div key={k} className={`desc-item${typeof v === 'object' && v !== null ? ' full' : ''}`}>
          <dt>{fieldLabel(k)}</dt>
          <dd>{readable(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

function withRest(Block: ComponentType<BlockProps>, shown: readonly string[]): ComponentType<BlockProps> {
  function WithRest(props: BlockProps) {
    return (
      <>
        <Block {...props} />
        <Rest d={details(props.record)} shown={shown} />
      </>
    );
  }
  return WithRest;
}

/** The blocks by module id; unknown modules get GenericBlock. */
export const EPISODE_BLOCKS: Record<string, ComponentType<BlockProps>> = {
  timestamp_check: withRest(TimestampBlock, ['n', 'duration_s', 'dt_nominal', 'max_dt', 'jitter_ratio', 'gap_frames', 'reason']),
  kinematic_limits: withRest(KinematicsBlock, ['violations', 'transient_violations', 'sustained_out_of_limit', 'n_violations', 'profile', 'reason']),
  motion_quality: MotionBlock,
  visual_quality: VisualBlock,
  video_action_sync: SyncBlock,
  task_success: TaskBlock,
  dedup: DedupBlock,
  skill_profile: SkillBlock,
  eef_video_consistency: EefBlock,
};

export function blockTitleExtra(record: ResultRecord): ReactNode {
  const parts: ReactNode[] = [];
  if (record.score !== null && record.score !== undefined) parts.push(<span key="score">{`${zh.report.colScore} ${fmt(record.score, 3)}`}</span>);
  if (record.elapsed_s !== null && record.elapsed_s !== undefined) parts.push(<span key="t" className="muted">{`${E().elapsed} ${zh.common.seconds(Number(record.elapsed_s.toFixed(2)))}`}</span>);
  return parts.length ? <Space size={12}>{parts}</Space> : null;
}

export function BlockError({ text }: { text: string }) {
  return (
    <Typography.Text type="error" data-testid="block-error">
      {E().error}：{text}
    </Typography.Text>
  );
}
