import { Progress, Tag } from '@arco-design/web-react';
import { useEffect, useState, type CSSProperties } from 'react';
import type { StageProgress, Task } from '../../api/types';
import { stageLabel, stagePercent } from '../../lib/taskView';
import { zh } from '../../locales/zh';
import './pipelineActivity.css';

const copy = zh.taskDetail.pipelineActivity;
const INTEGRITY_GREEN = '#00B42A';
const COLORS: Record<string, string> = {
  numeric: '#6366f1', frame: '#0891b2', vlm: '#e87925',
  profile_vlm: '#a855f7', profile: '#a855f7',
};

/**
 * 分档进度 of the streaming funnel (07 §4.2): the data integrity layer as a low strip on top
 * (progress and time per episode, sixth round), the other layers as cards (in flight, or waiting
 * for the next layer when that one's queue is full; queued, done, the latest batch), and their
 * timeline: solid while episodes were out, hatched while the layer waited (C4 1.18 `busy`).
 */
export function PipelineActivity({ task, stages }: { task: Task; stages: StageProgress[] }) {
  const [now, setNow] = useState(Date.now);
  const live = ['running', 'pausing', 'stopping'].includes(task.state);
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [live]);
  const integrity = stages.find((s) => s.id === 'integrity');
  const layers = stages.filter((s) => s.id !== 'integrity');
  const activity = layers.flatMap((s) => s.pipeline ? [s.pipeline] : []);
  const starts = activity.flatMap((p) => p.started_at === null ? [] : [p.started_at]);
  const origin = Math.min(...starts);
  const end = Math.max(...activity.map((p) => p.finished_at ?? (live ? Math.max(now, p.updated_at) : p.updated_at)));
  const span = Math.max(1, end - origin);
  const position = (at: number) => Math.max(0, Math.min(100, (at - origin) / span * 100));
  const withBusy = activity.some((p) => p.busy);
  return (
    <div className="pipeline-activity" data-testid="pipeline-activity">
      {integrity ? <IntegrityStrip stage={integrity} /> : null}
      <div className="pipeline-layers">
        {layers.map((s, i) => {
          const p = s.pipeline;
          const last = p?.recent.at(-1);
          const inflight = live && s.state === 'running' ? p?.inflight : 0;
          const downstream = layers[i + 1];
          const waiting = live && s.state === 'running' && !inflight && p?.held_by_downstream && downstream;
          return (
            <div className="pipeline-layer" key={s.id} data-testid={'stage-' + s.id}
              style={{ borderTopColor: COLORS[s.id] }}>
              <div className="pipeline-layer-heading">
                <b>{stageLabel(s.id)}</b><span className="muted">{zh.stageState[s.state]}</span>
              </div>
              <div className="pipeline-inflight" data-testid={'inflight-' + s.id}>
                {waiting
                  ? <span className="pipeline-waiting">{copy.waitingDownstream(stageLabel(downstream.id))}</span>
                  : <><strong>{p ? inflight : '—'}</strong><span>{copy.inflight}</span></>}
              </div>
              <div className="pipeline-counts muted">
                <span>{copy.queued(p?.queued ?? 0)}</span><span>{s.done} / {s.total} {copy.completed}</span>
              </div>
              <Progress percent={stagePercent(s)} showText={false} size="small"
                color={COLORS[s.id]} status={s.state === 'failed' ? 'error' : 'normal'} />
              {p?.processing?.mean_s != null ? <div className="muted pipeline-processing">
                {copy.mean(p.processing.mean_s.toFixed(2))}
                <span> · {copy.slowest((p.processing.max_s ?? 0).toFixed(2))}</span>
              </div> : null}
              {last ? (
                <div key={String(last.at) + '-' + last.number}
                  className={'pipeline-arrival' + (live && now - last.at < 5000 ? ' is-new' : '')}
                  data-testid={'arrival-' + s.id} aria-live="polite">
                  <Tag size="small" color={COLORS[s.id]}>{copy.batch(last.number)}</Tag>
                  <span>{copy.entered(last.count)}</span>
                  <div className="muted pipeline-episode-list">
                    {last.episodes.map((ep) => 'ep ' + ep).join(' · ')}{last.count > last.episodes.length ? ' …' : ''}
                  </div>
                </div>
              ) : <div className="pipeline-arrival muted">{copy.waiting}</div>}
            </div>
          );
        })}
      </div>
      {starts.length > 0 ? (
        <div className="pipeline-timeline" data-testid="pipeline-overlap">
          <div className="pipeline-axis"><span>{copy.timeline(layers.length)}</span><span>0 – {Math.ceil(span / 1000)} s</span></div>
          {layers.map((s) => {
            const p = s.pipeline;
            const finish = p?.finished_at ?? (live ? Math.max(now, p?.updated_at ?? now) : p?.updated_at ?? end);
            const lane = { '--lane': COLORS[s.id] } as CSSProperties;
            const bar = (from: number, to: number, className: string, min: number, key?: string) => (
              <div className={className} key={key} style={{ ...lane, left: position(from) + '%', width: Math.max(min, position(to) - position(from)) + '%' }} />
            );
            return (
              <div className="pipeline-time-row" key={s.id} data-testid={'timeline-' + s.id}>
                <span>{stageLabel(s.id)}</span>
                <div className="pipeline-time-track">
                  {p?.started_at != null ? (
                    p.busy
                      // the layer's run hatched (waiting), the spans with episodes out solid over it
                      ? <>
                          {bar(p.started_at, finish, 'pipeline-time-bar is-waiting', 0.4)}
                          {p.busy.map((b) => bar(b.start, b.end ?? finish, 'pipeline-time-bar', 0.3, String(b.start)))}
                        </>
                      // snapshots from before C4 1.18 do not say when the layer waited
                      : bar(p.started_at, finish, 'pipeline-time-bar', 0.4)
                  ) : null}
                  {p?.recent.map((batch) => <span className="pipeline-time-tick" key={batch.number}
                    title={copy.batch(batch.number) + ' · ' + copy.entered(batch.count)}
                    style={{ left: position(batch.at) + '%' }} />)}
                </div>
              </div>
            );
          })}
          <div className="muted pipeline-timeline-note">{withBusy ? copy.note : copy.noteOld}</div>
        </div>
      ) : null}
    </div>
  );
}

/** The data integrity layer, low and on one line: its progress and the time per episode. */
function IntegrityStrip({ stage }: { stage: StageProgress }) {
  const mean = stage.pipeline?.processing?.mean_s;
  return (
    <div className="pipeline-strip" data-testid="stage-integrity">
      <b>{stageLabel(stage.id)}</b>
      {/* green (sixth round); a failed layer keeps the red error bar */}
      <Progress
        percent={stagePercent(stage)}
        showText={false}
        size="small"
        status={stage.state === 'failed' ? 'error' : 'normal'}
        color={stage.state === 'failed' ? undefined : INTEGRITY_GREEN}
      />
      <span className="muted">{stage.done} / {stage.total}</span>
      {mean != null ? <span className="muted">{copy.mean(mean.toFixed(2))}</span> : null}
    </div>
  );
}
