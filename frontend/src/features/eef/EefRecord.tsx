// The EEF module's record of one episode for a person (design 12 C.9, D49): the conclusion and why,
// the CPU's sub-item readings, and the model's answer on every review window next to the marked crops
// it was shown. The adjudication card (F5.11) and the report's Episode tab (F5.12) both show it.
import { Button, Space, Table, Tag } from '@arco-design/web-react';
import { useState } from 'react';
import type { ResultRecord } from '../../api/types';
import { CHART_COLORS, Chart, lineOption } from '../../components/Chart';
import {
  EEF_CAMERA_SUBITEMS,
  eefConclusion,
  eefCpuEvidence,
  eefCpuRows,
  eefDatasetRecord,
  eefOpinion,
  eefStateMotion,
  eefWindowRows,
  type EefOpinionCamera,
  type EefRecordCurves,
  type EefWindowRow,
} from '../../lib/eefReadings';
import { zh } from '../../locales/zh';
import { SignedImage } from '../media/SignedMedia';
import { EefOverlayVideo, type SeekAsk } from './EefOverlayVideo';

type D = Record<string, unknown>;
const details = (r: ResultRecord): D => (r.details && typeof r.details === 'object' ? (r.details as D) : {});
const Z = () => zh.eefDetail;
const OUTCOME_COLOR: Record<string, string> = { pass: 'green', reject: 'red', human: 'orange', opinion: 'arcoblue' };

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
 * the stretches it finds mismatched, the most confident first, with the frames it cited. The camera's own
 * video plays with the marks drawn live over it (design doc 20); a cited frame seeks it there. It is only
 * an opinion: the episode's verdict does not depend on it.
 */
function OpinionCamera({ taskId, episode, c }: { taskId: string; episode: number; c: EefOpinionCamera }) {
  const O = Z().opinion;
  const [seek, setSeek] = useState<SeekAsk | null>(null);
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
      {c.status !== 'skipped' ? <EefOverlayVideo taskId={taskId} episode={episode} camera={c.camera} seek={seek} /> : null}
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
                <Button key={f} size="mini" type="text" aria-label={O.seekFrame(f + 1)} onClick={() => setSeek((s) => ({ frame: f, n: (s?.n ?? 0) + 1 }))}>
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

export function EefOpinion({ taskId, record }: { taskId: string; record: ResultRecord }) {
  const op = eefOpinion(details(record));
  if (!op) return null;
  const O = Z().opinion;
  return (
    <div data-testid="eef-opinion">
      <div className="eef-head">{O.title}</div>
      <div className="episode-line muted">{O.advisory}</div>
      {op.failure ? <div className="episode-line warn">{O.failed(op.failure)}</div> : null}
      {op.cameras.map((c) => (
        <OpinionCamera key={c.camera} taskId={taskId} episode={record.episode_index} c={c} />
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
