// The EEF module's record of one episode for a person (design 12 C.9, D49): the conclusion and why,
// the CPU's sub-item readings, and the model's answer on every review window next to the marked crops
// it was shown. The adjudication card (F5.11) and the report's Episode tab (F5.12) both show it.
import { Button, Space, Table, Tag } from '@arco-design/web-react';
import { useContext } from 'react';
import type { ResultRecord } from '../../api/types';
import { CHART_COLORS, Chart, lineOption } from '../../components/Chart';
import {
  EEF_CAMERA_SUBITEMS,
  eefConclusion,
  eefCpuEvidence,
  eefCpuRows,
  eefDatasetRecord,
  eefEgoMotion,
  eefMerged,
  eefOpinion,
  eefStateMotion,
  eefTrajectorySource,
  eefWindowRows,
  type EefCalibrationSuspect as EefCalibrationSuspectRow,
  type EefCell,
  type EefEgoCamera,
  type EefOpinionCamera,
  type EefRecordCurves,
  type EefWindowRow,
} from '../../lib/eefReadings';
import { zh } from '../../locales/zh';
import { SignedImage } from '../media/SignedMedia';
import { MiniPlayerOpen } from '../visualizer/miniPlayer';

type D = Record<string, unknown>;
const details = (r: ResultRecord): D => (r.details && typeof r.details === 'object' ? (r.details as D) : {});
const Z = () => zh.eefDetail;
const OUTCOME_COLOR: Record<string, string> = { pass: 'green', reject: 'red', human: 'orange', opinion: 'arcoblue' };

const LABEL_COLOR: Record<string, string | undefined> = { inconsistent: 'red', possibly_inconsistent: 'orange', consistent: 'green', cannot_tell: undefined };
const FLAG_COLOR: Record<string, string | undefined> = { conflict: 'orangered', single_source: undefined, tracking_invalid: 'gold' };
const pFmt = (p: number | null) => (p === null ? '—' : p.toFixed(2));

/** One side of a cell: the channel, its verdict and p, or why it said nothing. */
function SideCell({ cell, side }: { cell: EefCell; side: 'cpu' | 'vlm' }) {
  const O = Z().output;
  const src = cell.sources.find((x) => (side === 'cpu' ? x.channel === 'cpu' || x.channel === 'ego' : x.channel.startsWith('vlm_')));
  if (!src) {
    const gone = (side === 'cpu') === (cell.missing === 'tracking_invalid' || ['no_gripper_reference', 'not_measured', 'cpu_cannot_tell'].includes(cell.missing ?? ''));
    return <span className="muted">{gone && cell.missing ? O.missing[cell.missing] ?? cell.missing : '—'}</span>;
  }
  const voided = side === 'cpu' && cell.flags.includes('tracking_invalid');
  return (
    <span className={voided ? 'muted' : undefined} style={voided ? { textDecoration: 'line-through' } : undefined}>
      {O.channel[src.channel] ?? src.channel}：{O.verdict[src.verdict] ?? src.verdict}
      {src.p !== null ? `（${pFmt(src.p)}）` : src.why ? `（${O.why[src.why] ?? O.missing[src.why] ?? src.why}）` : ''}
    </span>
  );
}

/**
 * The opinion of registry 5.0 (design doc 25 §7): label · confidence · grounds for the episode, then every
 * sub-item and camera with what each side said and the merge - the largest p, a conflict, a single source capped.
 */
export function EefOutput({ record, suspects = [] }: { record: ResultRecord; suspects?: readonly EefCalibrationSuspectRow[] }) {
  const m = eefMerged(details(record));
  if (!m) return null;
  const O = Z().output;
  // a camera of a dataset-level calibration suspect (design doc 25 §7.4): said under the conclusion, and its
  // position cell says so
  const suspect = suspects.map((x) => x.camera);
  const flagsOf = (c: EefCell) => [...c.flags, ...(c.subitem === 'position_2d' && c.camera && suspect.includes(c.camera) ? ['calibration_suspect'] : [])];
  return (
    <div data-testid="eef-output">
      <div className="episode-line">
        <Space wrap size={8}>
          <Tag color={LABEL_COLOR[m.label]} data-testid="eef-label">
            {O.label[m.label] ?? m.label}
          </Tag>
          {m.p !== null ? <b data-testid="eef-p">{O.p(pFmt(m.p))}</b> : null}
          {m.flags.map((f) => (
            <Tag key={f} size="small" color={FLAG_COLOR[f]}>
              {O.flag[f] ?? f}
            </Tag>
          ))}
        </Space>
      </div>
      {m.reason ? (
        <div className="episode-line" data-testid="eef-reason">
          {m.reason}
        </div>
      ) : null}
      {m.uncalibrated && m.p !== null ? <div className="episode-line muted">{O.uncalibrated}</div> : null}
      <EefCalibrationSuspect suspects={suspects} />
      {m.cells.length ? (
        <Table
          size="mini"
          rowKey="key"
          pagination={false}
          border={false}
          data-testid="eef-cells"
          rowClassName={(c: EefCell) => (c.flags.includes('conflict') ? 'eef-conflict-row' : '')}
          data={m.cells}
          columns={[
            { title: O.cols.subitem, dataIndex: 'subitem', render: (v: string) => O.subitem[v] ?? v },
            { title: O.cols.camera, dataIndex: 'camera', render: (v: string | null) => v ?? O.episode },
            { title: O.cols.cpu, render: (_: unknown, c: EefCell) => <SideCell cell={c} side="cpu" /> },
            { title: O.cols.vlm, render: (_: unknown, c: EefCell) => <SideCell cell={c} side="vlm" /> },
            {
              title: O.cols.merged,
              render: (_: unknown, c: EefCell) =>
                c.p === null ? (
                  <span className="muted">{O.label.cannot_tell}</span>
                ) : (
                  <Tag size="small" color={LABEL_COLOR[c.label]}>
                    {O.label[c.label] ?? c.label} · {pFmt(c.p)}
                  </Tag>
                ),
            },
            {
              title: O.cols.flags,
              render: (_: unknown, c: EefCell) =>
                [...flagsOf(c).map((f) => O.flag[f] ?? f), ...(c.missing && c.flags.includes('single_source') ? [O.missing[c.missing] ?? c.missing] : [])].join('；') || '—',
            },
          ]}
        />
      ) : (
        <div className="episode-line muted">{O.noCells}</div>
      )}
    </div>
  );
}

/** The dataset-level calibration suspects this episode is one of (design doc 25 §7.4, D86): the report's sentence -
 *  more likely the extrinsics, the TCP offset or an assumed value than this episode's own data. */
export function EefCalibrationSuspect({ suspects }: { suspects: readonly EefCalibrationSuspectRow[] }) {
  if (!suspects.length) return null;
  return (
    <>
      {suspects.map((x) => (
        <div key={x.camera} className="episode-line warn" data-testid="eef-calibration-suspect">
          <Tag size="small" color="orange">
            {Z().output.flag.calibration_suspect}
          </Tag>{' '}
          {x.message}
        </div>
      ))}
    </>
  );
}

/** 判过 / 判废 / 转人工 and why: the person's reasons, the confirmed defects, what was not checked. */
export function EefConclusion({ record, tag = true }: { record: ResultRecord; tag?: boolean }) {
  const c = eefConclusion(details(record));
  return (
    <div data-testid="eef-conclusion">
      {tag && c.outcome ? (
        <Tag color={OUTCOME_COLOR[c.outcome]} data-testid="eef-outcome">
          {zh.sections.eef.outcome[c.outcome] ?? c.outcome}
        </Tag>
      ) : null}
      {c.human.length ? (
        <div className="episode-line warn">
          <b>{Z().why}：</b>
          <ul className="eef-calls">
            {c.human.map((h, i) => (
              <li key={i}>{h.text}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {c.confirmed.length ? (
        <div className="episode-line bad">
          <b>{Z().confirmed}：</b>
          <ul className="eef-calls">
            {c.confirmed.map((h, i) => (
              <li key={i}>{h.text}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {!c.human.length && !c.confirmed.length && c.reason ? <div className="episode-line">{c.reason}</div> : null}
      {c.unchecked.length ? (
        <div className="episode-line muted">
          {Z().unchecked}：{c.unchecked.join('；')}
        </div>
      ) : null}
    </div>
  );
}

/** The CPU's sub-item statuses per camera, suspect cells marked. */
export function EefCpuTable({ record }: { record: ResultRecord }) {
  const d = details(record);
  const rows = eefCpuRows(d);
  const state = eefStateMotion(d);
  if (!rows.length && !state) return null;
  const C = zh.episodeTab.eef.cols;
  return (
    <div data-testid="eef-cpu">
      <div className="eef-head">{Z().cpu}</div>
      {rows.length ? (
        <Table
          rowKey="key"
          size="small"
          pagination={false}
          data={rows}
          columns={[
            { title: C.camera, dataIndex: 'camera', render: (v: string) => <span className="mono">{v}</span> },
            { title: C.mount, dataIndex: 'mount' },
            ...EEF_CAMERA_SUBITEMS.map((k) => ({
              title: zh.sections.eef.subitem[k] ?? k,
              dataIndex: k,
              render: (_: unknown, r: (typeof rows)[number]) => <span className={r.suspect.includes(k) ? 'eef-suspect' : undefined}>{r.cells[k]}</span>,
            })),
          ]}
        />
      ) : null}
      {state ? (
        <div className="episode-line">
          {Z().stateMotion}：{state}
        </div>
      ) : null}
    </div>
  );
}

function WindowBlock({ taskId, w }: { taskId: string; w: EefWindowRow }) {
  return (
    <div className={`eef-window${w.conflict ? ' conflict' : ''}`} data-testid={`eef-window-${w.key}`}>
      <Space wrap size={8}>
        <span className="mono">{w.camera}</span>
        <b>{w.title}</b>
        <span className="muted">{w.frames}</span>
        {w.target ? <span className="mono muted">{w.target}</span> : null}
        {w.cached ? <Tag size="small">{Z().cached}</Tag> : null}
      </Space>
      {w.answered ? (
        <Space wrap size={6} style={{ marginTop: 4 }}>
          {w.votes.map((v) => (
            <Tag key={v.label} size="small" color={v.tone === 'bad' ? 'red' : v.tone === 'good' ? 'green' : undefined}>
              {v.label}：{v.value}
            </Tag>
          ))}
          {w.offset ? <span className="muted">{w.offset}</span> : null}
        </Space>
      ) : (
        <div className="episode-line warn">{w.failure ?? Z().failed}</div>
      )}
      {w.explanation ? <div className="episode-line">「{w.explanation}」</div> : null}
      {w.conflict ? (
        <Tag color="orange" size="small">
          {w.conflict}
        </Tag>
      ) : null}
      {w.evidence.length ? (
        <div className="evidence-grid" style={{ marginTop: 6 }}>
          {w.evidence.map((p) => (
            <SignedImage key={p} task={taskId} scope="delivery" path={p} alt={`${w.camera} · ${p.split('/').pop() ?? ''}`} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** Every review window with the model's answer and the marked crops it was shown. */
export function EefWindows({ taskId, record }: { taskId: string; record: ResultRecord }) {
  const windows = eefWindowRows(details(record));
  return (
    <div data-testid="eef-windows">
      <div className="eef-head">{Z().windows}</div>
      {windows.length ? windows.map((w) => <WindowBlock key={w.key} taskId={taskId} w={w} />) : <div className="muted">{Z().windowsNone}</div>}
    </div>
  );
}

/** The CPU's own evidence frames: the record's evidence the windows and the dataset-record block do not show. */
export function EefCpuEvidence({ taskId, record }: { taskId: string; record: ResultRecord }) {
  const d = details(record);
  const rest = eefCpuEvidence((record.evidence ?? []) as string[], eefWindowRows(d), eefDatasetRecord(d)?.overlays.map((o) => o.path));
  if (!rest.length) return null;
  return (
    <div>
      <div className="eef-head">{Z().cpuEvidence}</div>
      <div className="evidence-grid">
        {rest.map((p) => (
          <SignedImage key={p} task={taskId} scope="delivery" path={p} alt={p.split('/').pop() ?? p} />
        ))}
      </div>
    </div>
  );
}

/**
 * No gripper reference (design doc 12 §10.5, D-E15): the model's opinion on each camera's whole clip -
 * the stretches it finds mismatched, the most confident first, with the frames it cited. A cited frame
 * opens the mini player there, the marks drawn over the camera's own video (design doc 22 §3.3). It is
 * only an opinion: the episode's verdict does not depend on it.
 */
function OpinionCamera({ c }: { c: EefOpinionCamera }) {
  const O = Z().opinion;
  const open = useContext(MiniPlayerOpen);
  return (
    <div className="eef-window" data-testid={`eef-opinion-${c.camera}`}>
      <Space wrap size={8}>
        <b>{c.camera}</b>
        {c.point ? (
          <span className="muted">
            {Z().target(c.point, c.axis)}
            {c.fingerAxis ? O.finger(c.fingerAxis) : ''}
          </span>
        ) : null}
        {c.status === 'skipped' ? <span className="muted">{O.skipped}</span> : null}
      </Space>
      {c.reason && c.status === 'skipped' ? <div className="episode-line muted">{c.reason}</div> : null}
      {c.unseen ? <div className="episode-line warn">{O.unseen}</div> : null}
      {c.failures.map((f) => (
        <div key={f} className="episode-line warn">
          {f}
        </div>
      ))}
      {c.repairs.map((r) => (
        <div key={r} className="episode-line muted" data-testid="eef-opinion-repaired">
          {r}
        </div>
      ))}
      {c.status !== 'skipped' && !c.segments.length && !c.failures.length ? <div className="episode-line">{O.none}</div> : null}
      {c.segments.map((g, i) => (
        <div key={g.key} className="eef-opinion-segment" data-testid="eef-opinion-segment">
          <Space wrap size={6}>
            <b>{O.segment(i + 1)}</b>
            <span>{Z().frames(g.startFrame, g.endFrame, g.endFrame - g.startFrame + 1)}</span>
            {g.startS !== null && g.endS !== null ? <span className="muted">{O.seconds(g.startS, g.endS)}</span> : null}
            <Tag size="small">{O.aspect[g.aspect] ?? g.aspect}</Tag>
            <Tag size="small" color={g.confidence >= 0.7 ? 'red' : g.confidence >= 0.5 ? 'orange' : undefined}>
              {O.confidence(Math.round(g.confidence * 100))}
            </Tag>
          </Space>
          {g.observation ? <div className="episode-line">「{g.observation}」</div> : null}
          {g.evidenceFrames.length ? (
            <Space wrap size={4} className="episode-line">
              <span className="muted">{O.evidenceFrames}</span>
              {g.evidenceFrames.map((f) => (
                <Button key={f} size="mini" type="text" disabled={!open} title={O.evidenceHint} aria-label={O.seekFrame(f + 1)} onClick={() => open?.({ camera: c.camera, frame: f })}>
                  {f + 1}
                </Button>
              ))}
            </Space>
          ) : null}
        </div>
      ))}
      {c.summaries.length ? <div className="episode-line muted">{O.summary}：{c.summaries.join('；')}</div> : null}
    </div>
  );
}

/**
 * A handheld gripper's trajectory the platform derived from the recording (design doc 22 §5.4): which calibration
 * (built-in DEMO or uploaded, the assumed fields), each hand's camera - paired how well, or why not drawn - and
 * what the export's checks found suspect. Nothing for an uploaded trajectory.json.
 */
export function EefTrajectorySource({ record }: { record: ResultRecord }) {
  const src = eefTrajectorySource(details(record));
  if (!src) return null;
  const T = Z().source;
  if (src.kind === 'generated')
    return (
      <div data-testid="eef-source">
        <div className="episode-line">
          <Space wrap size={8}>
            <Tag color="arcoblue">{T.generated(src.version)}</Tag>
            {src.assumed.length ? <span className="muted">{T.assumed(src.assumed.join('、'))}</span> : null}
          </Space>
        </div>
        {src.declaredFixed.length ? (
          <div className="episode-line warn" data-testid="eef-declared-fixed">
            {T.declaredFixed(src.declaredFixed.join('、'))}
          </div>
        ) : null}
        {src.reason ? <div className="episode-line warn">{[T.reason[src.reason] ?? src.reason, src.message].filter(Boolean).join('：')}</div> : null}
      </div>
    );
  return (
    <div data-testid="eef-source">
      <div className="episode-line">
        <Space wrap size={8}>
          <Tag color="arcoblue">{T.derived}</Tag>
          <span>{src.builtin ? T.builtin(src.gripper) : T.uploaded(src.gripper)}</span>
          {src.assumed.length ? <span className="muted">{T.assumed(src.assumed.join('、'))}</span> : null}
        </Space>
      </div>
      {src.reason ? <div className="episode-line warn">{[T.reason[src.reason] ?? src.reason, src.message].filter(Boolean).join('：')}</div> : null}
      {src.cameras.map((c) => (
        <div key={c.hand} className={`episode-line${c.status === 'ok' ? ' muted' : ' warn'}`}>
          {c.status === 'ok'
            ? T.paired(c.hand, c.pairingRate !== null ? (c.pairingRate * 100).toFixed(1) : '—', T.intrinsics[c.intrinsics ?? ''] ?? c.intrinsics ?? '—')
            : T.unsupported(c.hand, c.reason ?? c.status)}
        </div>
      ))}
      {src.suspects.length ? (
        <div className="episode-line warn">
          {T.suspects}：{src.suspects.join('；')}
        </div>
      ) : null}
    </div>
  );
}

export function EefOpinion({ record }: { record: ResultRecord }) {
  const op = eefOpinion(details(record));
  if (!op) return null;
  const O = Z().opinion;
  if (op.status === 'not_asked')
    // the model was not asked (design doc 25 D84): switched off, or the task has no model
    return (
      <div data-testid="eef-opinion">
        <div className="eef-head">{O.title}</div>
        <div className="episode-line muted">{O.notAsked(zh.eefDetail.output.missing[op.missing ?? ''] ?? op.missing ?? '')}</div>
      </div>
    );
  if (op.status === 'not_assessable')
    // no trajectory for the episode (design doc 22 §5.4): nothing was drawn or asked; a derived one's block says why
    return (
      <div data-testid="eef-opinion">
        <div className="eef-head">{O.title}</div>
        <div className="episode-line warn">{O.notAssessable(eefTrajectorySource(details(record)) ? null : op.failure)}</div>
      </div>
    );
  return (
    <div data-testid="eef-opinion">
      <div className="eef-head">{O.title}</div>
      <div className="episode-line muted">{op.handheld ? O.advisoryHandheld : O.advisory}</div>
      {op.bridged ? <div className="episode-line muted">{O.bridged(op.bridged.maxGapMs, op.bridged.hands)}</div> : null}
      {op.failure ? <div className="episode-line warn">{O.failed(op.failure)}</div> : null}
      {op.cameras.map((c) => (
        <OpinionCamera key={c.camera} c={c} />
      ))}
    </div>
  );
}

const EGO_TONE: Record<string, string | undefined> = { ok: 'green', suspect: 'orange', good: 'green', bad: 'orange' };
const BAND_TONE: Record<string, string | undefined> = { minor: undefined, moderate: 'orange', severe: 'red' };
const fixed = (v: number | null, digits = 1) => (v === null ? '—' : v.toFixed(digits));
const signed = (v: number) => `${v > 0 ? '+' : ''}${v.toFixed(2)}`;

/** One wrist camera: its readings, the bad stretches (worst first) with the frames that show them, the unmatched ones. */
function EgoCamera({ c }: { c: EefEgoCamera }) {
  const G = Z().ego;
  const open = useContext(MiniPlayerOpen);
  const readings = [
    c.rotationMedian !== null ? G.rotation(fixed(c.rotationMedian), fixed(c.rotationP95)) : null,
    c.lagS !== null ? G.lag(signed(c.lagS), fixed(c.lagConfidence, 2)) : null,
    c.coverage !== null ? G.coverage(Math.round(c.coverage * 100), c.pairs ?? 0) : null,
    c.directionMedian !== null ? G.direction(fixed(c.directionMedian, 0)) : null,
  ].filter(Boolean);
  return (
    <div className="eef-window" data-testid={`eef-ego-${c.camera}`}>
      <Space wrap size={8}>
        <b>{c.camera}</b>
        <Tag size="small" color={EGO_TONE[c.status]}>
          {G.status[c.status] ?? c.status}
        </Tag>
        {c.reason ? <span className="muted">{G.reason[c.reason] ?? c.reason}</span> : null}
      </Space>
      {readings.length ? <div className="episode-line muted">{readings.join(' · ')}</div> : null}
      {c.segments.map((g, i) => (
        <div key={g.key} className="eef-opinion-segment" data-testid="eef-ego-segment">
          <Space wrap size={6}>
            <b>{G.segment(i + 1)}</b>
            <span>{Z().frames(g.startFrame, g.endFrame, g.endFrame - g.startFrame + 1)}</span>
            {g.startS !== null && g.endS !== null ? <span className="muted">{Z().opinion.seconds(g.startS, g.endS)}</span> : null}
            <Tag size="small">{G.reasons[g.reason] ?? g.reason}</Tag>
            <Tag size="small" color={BAND_TONE[g.band]}>
              {G.band[g.band] ?? g.band}
            </Tag>
            <span>
              {g.reason === 'time_offset'
                ? (g.lagS ?? 0) < 0
                  ? G.early(Math.abs(g.lagS ?? 0).toFixed(2))
                  : G.late(Math.abs(g.lagS ?? g.magnitude).toFixed(2))
                : G.rotated(g.magnitude.toFixed(1))}
            </span>
          </Space>
          {g.evidenceFrames.length ? (
            <Space wrap size={4} className="episode-line">
              <span className="muted">{G.evidenceFrames}</span>
              {g.evidenceFrames.map((f) => (
                <Button key={f} size="mini" type="text" disabled={!open} title={G.evidenceHint} aria-label={Z().opinion.seekFrame(f + 1)} onClick={() => open?.({ camera: c.camera, frame: f })}>
                  {f + 1}
                </Button>
              ))}
            </Space>
          ) : null}
        </div>
      ))}
      {c.unmatched.length ? (
        <div className="episode-line muted" data-testid="eef-ego-unmatched">
          {G.unmatched}：
          {c.unmatched
            .map((u) => `${Z().frames(u.startFrame, u.endFrame, u.endFrame - u.startFrame + 1)}${u.startS !== null && u.endS !== null ? `（${Z().opinion.seconds(u.startS, u.endS)}）` : ''}`)
            .join('、')}
        </div>
      ) : null}
    </div>
  );
}

/**
 * A handheld gripper's wrist cameras (design doc 22 §5.3): each camera's motion from its pictures against its
 * recorded poses - good or bad in one sentence, every camera's readings and bad stretches. Reported only; the
 * thresholds are uncalibrated and the trajectory may rest on assumed values (the note says which).
 */
export function EefEgoMotion({ record }: { record: ResultRecord }) {
  const ego = eefEgoMotion(details(record));
  if (!ego) return null;
  const G = Z().ego;
  return (
    <div data-testid="eef-ego">
      <div className="eef-head">{G.title}</div>
      <div className="episode-line muted">{G.advisory}</div>
      <div className="episode-line">
        <Space wrap size={8}>
          <Tag color={EGO_TONE[ego.verdict]} data-testid="eef-ego-verdict">
            {G.verdict[ego.verdict] ?? ego.verdict}
          </Tag>
          <span>{ego.explanation}</span>
        </Space>
      </div>
      <div className="episode-line muted">
        {[ego.uncalibrated ? G.uncalibrated : null, ego.windowS !== null ? G.window(ego.windowS) : null].filter(Boolean).join(' · ')}
      </div>
      {ego.assumed ? <div className="episode-line warn">{ego.assumed}</div> : null}
      {ego.cameras.map((c) => (
        <EgoCamera key={c.camera} c={c} />
      ))}
    </div>
  );
}

const RECORD_TONE: Record<string, string | undefined> = { ok: 'green', suspect: 'orange', error: 'red' };

function RecordCurves({ curves }: { curves: EefRecordCurves }) {
  const R = Z().record;
  const line = (raw: (number | null)[], after: (number | null)[]) =>
    lineOption(
      [
        { name: R.seriesRaw, data: curves.frame.map((f, k) => [f, raw[k] ?? null] as [number, number | null]), color: CHART_COLORS[0] },
        { name: R.seriesAfter, data: curves.frame.map((f, k) => [f, after[k] ?? null] as [number, number | null]), color: CHART_COLORS[2], dashed: true },
      ],
      { xName: R.frameAxis },
    );
  return (
    <div className="eef-record-curves">
      <div>
        <div className="section-sub">{R.chartPosition}</div>
        <Chart height={150} summary={R.chartPosition} option={line(curves.position, curves.positionAfter)} />
      </div>
      <div>
        <div className="section-sub">{R.chartRotation}</div>
        <Chart height={150} summary={R.chartRotation} option={line(curves.rotation, curves.rotationAfter)} />
      </div>
    </div>
  );
}

/**
 * 轨迹与数据集记录 (design doc 12 §8.7, D-E16): the uploaded trajectory against the dataset's own record,
 * source by source - how the frames were paired, the residuals, the constant against the declared
 * relation, the time offset, the stretches that differ and their curves - then the dataset's own two
 * records against each other and the overlay frames. Reported only: never part of the verdict.
 */
export function EefDatasetRecord({ taskId, record }: { taskId: string; record: ResultRecord }) {
  const r = eefDatasetRecord(details(record));
  if (!r) return null;
  const R = Z().record;
  return (
    <div data-testid="eef-record">
      <div className="eef-head">
        <Space size={8}>
          <span>{R.title}</span>
          <Tag size="small" color={RECORD_TONE[r.status]} data-testid="eef-record-status">
            {r.statusText}
          </Tag>
        </Space>
      </div>
      <div className="episode-line muted">{R.advisory}</div>
      {r.reasons.length ? (
        <div className="episode-line">
          {r.reasons.join('；')}
          {r.message ? <span className="muted mono">（{r.message}）</span> : null}
        </div>
      ) : null}
      {r.sources.map((src) => (
        <div key={src.kind} className="eef-window" data-testid={`eef-record-${src.kind}`}>
          <Space wrap size={8}>
            <b>{src.name}</b>
            <Tag size="small" color={RECORD_TONE[src.status]}>
              {src.statusText}
            </Tag>
            {src.where ? <span className="mono muted">{src.where}</span> : null}
          </Space>
          {src.frames || src.alignment ? <div className="episode-line muted">{[src.frames, src.alignment].filter(Boolean).join('；')}</div> : null}
          {src.reasons.length ? <div className={`episode-line${src.status === 'suspect' ? ' warn' : ''}`}>{src.reasons.join('；')}</div> : null}
          {src.notes.length ? <div className="episode-line muted">{src.notes.join('；')}</div> : null}
          {src.residual.length ? (
            <Table
              rowKey="key"
              size="small"
              pagination={false}
              data={src.residual}
              style={{ marginTop: 6 }}
              columns={[
                { title: R.cols.what, dataIndex: 'what' },
                { title: R.cols.position, dataIndex: 'position' },
                { title: R.cols.rotation, dataIndex: 'rotation' },
              ]}
            />
          ) : null}
          {src.relation.map((x) => (
            <div key={x.label} className={`episode-line${x.bad ? ' warn' : ''}`}>
              {x.label}：{x.value}
            </div>
          ))}
          {src.lag ? <div className="episode-line">{src.lag}</div> : null}
          {src.segments.length ? (
            <div className="episode-line">
              {R.segments}：
              <ul className="eef-calls">
                {src.segments.map((g) => (
                  <li key={g}>{g}</li>
                ))}
              </ul>
            </div>
          ) : null}
          {src.curves ? <RecordCurves curves={src.curves} /> : null}
        </div>
      ))}
      {r.internal ? (
        <div className={`episode-line${r.internal.bad ? ' warn' : ' muted'}`} data-testid="eef-record-internal">
          {R.internal}：{r.internal.text}
        </div>
      ) : null}
      {r.overlays.length ? (
        <div>
          <div className="eef-head">{R.overlay}</div>
          <div className="episode-line muted">{r.legend.join('；')}</div>
          <div className="evidence-grid">
            {r.overlays.map((o) => (
              <SignedImage key={o.path} task={taskId} scope="delivery" path={o.path} alt={`${o.camera} · ${o.path.split('/').pop() ?? ''}`} />
            ))}
          </div>
        </div>
      ) : null}
      {r.evidenceError ? <div className="episode-line muted">{R.evidenceError(r.evidenceError)}</div> : null}
    </div>
  );
}
