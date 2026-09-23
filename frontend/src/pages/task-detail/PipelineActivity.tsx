import { Progress, Tag } from '@arco-design/web-react';
import { useEffect, useState } from 'react';
import type { StageProgress, Task } from '../../api/types';
import { stageLabel, stagePercent } from '../../lib/taskView';
import { zh } from '../../locales/zh';
import './pipelineActivity.css';

const copy = zh.taskDetail.pipelineActivity;
const COLORS: Record<string, string> = { numeric: '#6366f1', frame: '#0891b2', vlm: '#e87925' };

export function PipelineActivity({ task, stages }: { task: Task; stages: StageProgress[] }) {
  const [now, setNow] = useState(Date.now);
  const live = ['running', 'pausing', 'stopping'].includes(task.state);
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [live]);
  const activity = stages.flatMap((s) => s.pipeline ? [s.pipeline] : []);
  const starts = activity.flatMap((p) => p.started_at === null ? [] : [p.started_at]);
  const origin = Math.min(...starts);
  const end = Math.max(...activity.map((p) => p.finished_at ?? (live ? Math.max(now, p.updated_at) : p.updated_at)));
  const span = Math.max(1, end - origin);
  const position = (at: number) => Math.max(0, Math.min(100, (at - origin) / span * 100));
  return (
    <div className="pipeline-activity" data-testid="pipeline-activity">
      <div className="pipeline-layers">
        {stages.map((s) => {
          const p = s.pipeline;
          const last = p?.recent.at(-1);
          const inflight = live && s.state === 'running' ? p?.inflight : 0;
          return (
            <div className="pipeline-layer" key={s.id} data-testid={'stage-' + s.id}
              style={{ borderTopColor: COLORS[s.id] }}>
              <div className="pipeline-layer-heading">
                <b>{stageLabel(s.id)}</b><span className="muted">{zh.stageState[s.state]}</span>
              </div>
              <div className="pipeline-inflight" data-testid={'inflight-' + s.id}>
                <strong>{p ? inflight : '—'}</strong><span>{copy.inflight}</span>
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
          <div className="pipeline-axis"><span>{copy.timeline}</span><span>0 – {Math.ceil(span / 1000)} s</span></div>
          {stages.map((s) => {
            const p = s.pipeline;
            const finish = p?.finished_at ?? (live ? Math.max(now, p?.updated_at ?? now) : p?.updated_at ?? end);
            return (
              <div className="pipeline-time-row" key={s.id}>
                <span>{stageLabel(s.id)}</span>
                <div className="pipeline-time-track">
                  {p?.started_at != null ? <div className="pipeline-time-bar" style={{
                    left: position(p.started_at) + '%',
                    width: Math.max(0.4, position(finish) - position(p.started_at)) + '%',
                    background: COLORS[s.id],
                  }} /> : null}
                  {p?.recent.map((batch) => <span className="pipeline-time-tick" key={batch.number}
                    title={copy.batch(batch.number) + ' · ' + copy.entered(batch.count)}
                    style={{ left: position(batch.at) + '%' }} />)}
                </div>
              </div>
            );
          })}
          <div className="muted pipeline-timeline-note">{copy.note}</div>
        </div>
      ) : null}
    </div>
  );
}
