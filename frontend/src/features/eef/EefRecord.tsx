// The EEF module's record of one episode for a person (design 12 C.9, D49): the conclusion and why,
// the CPU's sub-item readings, and the model's answer on every review window next to the marked crops
// it was shown. The adjudication card (F5.11) and the report's Episode tab (F5.12) both show it.
import { Space, Table, Tag } from '@arco-design/web-react';
import type { ResultRecord } from '../../api/types';
import { EEF_CAMERA_SUBITEMS, eefConclusion, eefCpuEvidence, eefCpuRows, eefStateMotion, eefWindowRows, type EefWindowRow } from '../../lib/eefReadings';
import { zh } from '../../locales/zh';
import { SignedImage } from '../media/SignedMedia';

type D = Record<string, unknown>;
const details = (r: ResultRecord): D => (r.details && typeof r.details === 'object' ? (r.details as D) : {});
const Z = () => zh.eefDetail;
const OUTCOME_COLOR: Record<string, string> = { pass: 'green', reject: 'red', human: 'orange' };

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

/** The CPU's own evidence frames: the record's evidence the windows do not show. */
export function EefCpuEvidence({ taskId, record }: { taskId: string; record: ResultRecord }) {
  const rest = eefCpuEvidence((record.evidence ?? []) as string[], eefWindowRows(details(record)));
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
