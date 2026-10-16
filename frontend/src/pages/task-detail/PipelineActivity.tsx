import { Progress, Tag } from '@arco-design/web-react';
import { useEffect, useState, type CSSProperties } from 'react';
import type { StageProgress, Task } from '../../api/types';
import { mergedStageState, STAGE_GROUPS, stagePercent } from '../../lib/taskView';
import { zh } from '../../locales/zh';
import './pipelineActivity.css';

const copy = zh.taskDetail.pipelineActivity;
/**
 * A group's colour: its card's top edge, its bar, its timeline lane. 2026-10-16 (requester): the three layers'
 * first colours in order - 数值档 indigo, 帧档 cyan, VLM 档 orange (数据完整性 was green from the sixth round).
 */
const COLORS: Record<string, string> = { integrity: '#6366f1', cpu: '#0891b2', model: '#e87925' };

type Activity = NonNullable<StageProgress['pipeline']>;

/** One group of the episodes' stages as the chart shows it (design doc 26 §2, D88). */
interface Group {
  key: string;
  label: string;
  members: StageProgress[];
  state: StageProgress['state'];
  done: number;
  total: number;
  activity: Activity[];
}

function groupsOf(stages: readonly StageProgress[]): Group[] {
  return STAGE_GROUPS.filter((g) => g.perEpisode).flatMap((g) => {
    const members = stages.filter((s) => g.members.includes(s.id));
    if (!members.length) return [];
    const counted = members.filter((m) => m.state !== 'skipped');
    return [{
      key: g.key,
      label: zh.stageGroup[g.key],
      members,
      state: mergedStageState(members.map((m) => m.state)),
      // every stage takes every selected episode: the group has done what all its stages have
      done: counted.length ? Math.min(...counted.map((m) => m.done)) : 0,
      total: Math.max(0, ...members.map((m) => m.total)),
      activity: members.flatMap((m) => (m.pipeline ? [m.pipeline] : [])),
    }];
  });
}

/**
 * 详细进度 of the streaming pipeline (07 §4.2, design doc 26 §2): a card per group the task has - 数据完整性,
 * CPU 检查, 模型检查, side by side, three at most (2026-10-13: 数据完整性 a card too, not the low strip of the sixth
 * round) - in flight, or waiting for the next group when its queue is full; queued, done, the latest batch to
 * enter; and their timeline: solid while episodes were out, hatched while the group waited (C4 1.18 `busy`). A
 * group adds up its stages: what is in flight and queued in any of them, the time an episode spends in all of them.
 */
export function PipelineActivity({ task, stages }: { task: Task; stages: StageProgress[] }) {
  const [now, setNow] = useState(Date.now);
  const live = ['running', 'pausing', 'stopping'].includes(task.state);
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [live]);
  const layers = groupsOf(stages);
  const activity = layers.flatMap((g) => g.activity);
  const starts = activity.flatMap((p) => (p.started_at === null ? [] : [p.started_at]));
  const origin = Math.min(...starts);
  const end = Math.max(...activity.map((p) => p.finished_at ?? (live ? Math.max(now, p.updated_at) : p.updated_at)));
  const span = Math.max(1, end - origin);
  const position = (at: number) => Math.max(0, Math.min(100, ((at - origin) / span) * 100));
  const withBusy = activity.some((p) => p.busy);
  return (
    <div className="pipeline-activity" data-testid="pipeline-activity">
      <div className="pipeline-layers" style={{ '--cards': layers.length } as CSSProperties}>
        {layers.map((g, i) => {
          const running = live && g.state === 'running';
          const inflight = running ? g.activity.reduce((a, p) => a + p.inflight, 0) : 0;
          const queued = g.activity.reduce((a, p) => a + p.queued, 0);
          // the latest batch to enter the group: its first stage's
          const last = g.activity[0]?.recent.at(-1);
          const means = g.activity.map((p) => p.processing?.mean_s).filter((x): x is number => x !== null && x !== undefined);
          const slowest = g.activity.length === 1 ? g.activity[0].processing?.max_s : null;
          const other = layers[i + 1] ?? layers[i - 1];
          const waiting = running && !inflight && g.activity.some((p) => p.held_by_downstream) && other;
          return (
            <div className="pipeline-layer" key={g.key} data-testid={'stage-' + g.key} style={{ borderTopColor: COLORS[g.key] }}>
              <div className="pipeline-layer-heading">
                <b>{g.label}</b>
                <span className="muted">{zh.stageState[g.state]}</span>
              </div>
              <div className="pipeline-inflight" data-testid={'inflight-' + g.key}>
                {waiting ? (
                  <span className="pipeline-waiting">{copy.waitingDownstream(other.label)}</span>
                ) : (
                  <>
                    <strong>{g.activity.length ? inflight : '—'}</strong>
                    <span>{copy.inflight}</span>
                  </>
                )}
              </div>
              <div className="pipeline-counts muted">
                <span>{copy.queued(queued)}</span>
                <span>
                  {g.done} / {g.total} {copy.completed}
                </span>
              </div>
              <Progress percent={stagePercent(g)} showText={false} size="small" color={COLORS[g.key]} status={g.state === 'failed' ? 'error' : 'normal'} />
              {means.length ? (
                <div className="muted pipeline-processing">
                  <span>{copy.mean(means.reduce((a, x) => a + x, 0).toFixed(2))}</span>
                  {slowest != null ? <span>· {copy.slowest(slowest.toFixed(2))}</span> : null}
                </div>
              ) : null}
              {last ? (
                <div key={String(last.at) + '-' + last.number} className={'pipeline-arrival' + (live && now - last.at < 5000 ? ' is-new' : '')} data-testid={'arrival-' + g.key} aria-live="polite">
                  <Tag size="small" color={COLORS[g.key]}>
                    {copy.batch(last.number)}
                  </Tag>
                  <span>{copy.entered(last.count)}</span>
                  <div className="muted pipeline-episode-list">
                    {last.episodes.map((ep) => 'ep ' + ep).join(' · ')}
                    {last.count > last.episodes.length ? ' …' : ''}
                  </div>
                </div>
              ) : (
                <div className="pipeline-arrival muted">{copy.waiting}</div>
              )}
            </div>
          );
        })}
      </div>
      {starts.length > 0 ? (
        <div className="pipeline-timeline" data-testid="pipeline-overlap">
          <div className="pipeline-axis">
            <span>{copy.timeline}</span>
            <span>0 – {Math.ceil(span / 1000)} s</span>
          </div>
          {layers.map((g) => {
            const lane = { '--lane': COLORS[g.key] } as CSSProperties;
            const bar = (from: number, to: number, className: string, min: number, key?: string) => (
              <div className={className} key={key} style={{ ...lane, left: position(from) + '%', width: Math.max(min, position(to) - position(from)) + '%' }} />
            );
            const runs = g.activity.filter((p) => p.started_at !== null);
            const finish = (p: Activity) => p.finished_at ?? (live ? Math.max(now, p.updated_at) : p.updated_at);
            const from = runs.length ? Math.min(...runs.map((p) => p.started_at as number)) : null;
            const to = runs.length ? Math.max(...runs.map(finish)) : null;
            return (
              <div className="pipeline-time-row" key={g.key} data-testid={'timeline-' + g.key}>
                <span>{g.label}</span>
                <div className="pipeline-time-track">
                  {from !== null && to !== null
                    ? runs.some((p) => p.busy)
                      ? // the group's run hatched (waiting), the spans with episodes out in any of its stages solid over it
                        [bar(from, to, 'pipeline-time-bar is-waiting', 0.4, 'run'), ...runs.flatMap((p, k) => (p.busy ?? []).map((b) => bar(b.start, b.end ?? finish(p), 'pipeline-time-bar', 0.3, `${k}-${b.start}`)))]
                      : // snapshots from before C4 1.18 do not say when a stage waited
                        bar(from, to, 'pipeline-time-bar', 0.4)
                    : null}
                  {g.activity[0]?.recent.map((batch) => (
                    <span className="pipeline-time-tick" key={batch.number} title={copy.batch(batch.number) + ' · ' + copy.entered(batch.count)} style={{ left: position(batch.at) + '%' }} />
                  ))}
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
