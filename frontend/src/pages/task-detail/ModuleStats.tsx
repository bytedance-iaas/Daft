import { Button, Card, Spin, Typography } from '@arco-design/web-react';
import { IconDown, IconRight } from '@arco-design/web-react/icon';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { moduleName, qk, useModules } from '../../api/queries';
import type { ModuleState, Plan, ReportV2, Task } from '../../api/types';
import { MODULE_STATE_COLOR } from '../../features/tasks/ModuleSummary';
import { confirmModuleRetry } from '../../features/tasks/retryModule';
import { FindingsView, LevelTag } from '../../features/findings/FindingsView';
import { LEVELS, moduleLevels, placeLabel } from '../../lib/findings';
import { isTerminalState } from '../../lib/taskView';
import { zh } from '../../locales/zh';

const T = () => zh.taskDetail;

/** Which log stage a module runs in: from the plan, else its registry stage. */
export function logStageOf(plan: Plan | undefined, moduleId: string, registryStage: string | undefined): string {
  const s = plan?.stages.find((x) => x.modules?.includes(moduleId));
  return s?.id ?? registryStage ?? '';
}

/** The episodes a module failed on, from the error lines of its stage's log. */
export function ErrorEpisodes({ taskId, stage }: { taskId: string; stage: string }) {
  const q = useQuery({
    queryKey: qk.logs(taskId, { stage, level: 'error', errors: true }),
    queryFn: () => unwrap(api().GET('/tasks/{id}/logs', { params: { path: { id: taskId }, query: { stage, level: 'error', limit: 200 } } })),
  });
  if (q.isLoading) return <Spin size={14} tip={T().errorsLoading} />;
  const lines = (q.data?.items ?? []).filter((l) => l.episode_index !== null && l.episode_index !== undefined);
  const byEp = new Map<number, string>();
  for (const l of lines) byEp.set(l.episode_index!, l.msg);
  if (!byEp.size) return <Typography.Text type="secondary">{T().errorsNone}</Typography.Text>;
  return (
    <ul style={{ margin: 0, paddingLeft: 18 }} data-testid="error-episodes">
      {[...byEp.entries()].map(([ep, msg]) => (
        <li key={ep}>
          <b>ep {ep}</b> <span className="mono">{msg}</span>
        </li>
      ))}
    </ul>
  );
}

interface LiveCounts {
  id: string;
  judged: number;
  error: number;
  flagged: number;
}

/**
 * The live module counts of a two-block run while it has no report yet (design doc 17 §5.3, C4 2.3.0
 * `modules` of the live episode page): per module the episodes judged, failed on, with a finding.
 */
function useLiveCounts(task: Task, enabled: boolean): Map<string, LiveCounts> {
  const live = !isTerminalState(task.state) || Boolean(task.active_subtask);
  const q = useQuery({
    queryKey: ['task', task.id, 'pipeline-modules', task.state, task.result_rev],
    queryFn: () => unwrap(api().GET('/tasks/{id}/pipeline/episodes', { params: { path: { id: task.id }, query: { limit: 1 } } })),
    enabled: enabled && Boolean(task.started_at),
    refetchInterval: enabled && live ? 3000 : false,
    retry: false,
  });
  return new Map((q.data?.modules ?? []).map((m) => [m.id, m]));
}

/**
 * 模块 of the task detail (design doc 17 §5.3): one row per selected module - its place, state, the episodes it
 * assessed of those selected, those it found something in (per level) and failed on; unfolded, the same
 * 检出项 chart and readings as its report section. From the current report (each new revision refreshes it);
 * while a run has no report yet, the live counts. 去报告 opens the section.
 */
export function ModuleStatCards({ task, plan, v2 }: { task: Task; plan: Plan | undefined; v2: ReportV2 | null }) {
  const reg = useModules();
  const qc = useQueryClient();
  const [open, setOpen] = useState<string[]>([]);
  const [errorsOpen, setErrorsOpen] = useState<string[]>([]);
  const counts = useLiveCounts(task, !v2);
  const selected = task.modules.filter((m) => m.selected);
  const skipped = task.modules.filter((m) => !m.selected && m.availability !== 'available');
  const terminal = isTerminalState(task.state);
  const toggle = (list: string[], set: (x: string[]) => void, id: string) => set(list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);
  const retry = (m: ModuleState) =>
    confirmModuleRetry({ taskId: task.id, moduleId: m.id, name: moduleName(reg.data, m.id) || m.name, qc, onDone: () => void qc.invalidateQueries({ queryKey: ['task', task.id] }) });
  const row = (m: ModuleState) => {
    const spec = reg.data?.modules.find((x) => x.id === m.id);
    const section = v2?.modules.find((s) => s.id === m.id);
    const live = counts.get(m.id);
    const levels = section ? moduleLevels(section.summary) : null;
    const unfolded = open.includes(m.id) && section;
    const errors = m.episodes_error || live?.error || 0;
    return (
      <div key={m.id} className="module-stat" data-testid={`module-stat-${m.id}`}>
        <div className="module-stat-head">
          {section ? (
            <Button
              type="text"
              size="mini"
              icon={unfolded ? <IconDown /> : <IconRight />}
              aria-expanded={Boolean(unfolded)}
              aria-label={`${unfolded ? zh.reportPage.collapse : zh.reportPage.expand}：${moduleName(reg.data, m.id)}`}
              onClick={() => toggle(open, setOpen, m.id)}
            />
          ) : (
            <span className="module-stat-pad" />
          )}
          <b>{moduleName(reg.data, m.id) || m.name}</b>
          <span className="muted nowrap" style={{ fontSize: 12 }}>
            {placeLabel(reg.data, spec)}
          </span>
          <span className="nowrap">
            <span className="dot" style={{ background: MODULE_STATE_COLOR[m.state] }} />
            {zh.moduleState[m.state] ?? m.state}
          </span>
          {section ? (
            <span className="nowrap" data-testid={`module-assessed-${m.id}`}>
              {T().assessedOf(section.summary.assessed_episodes, m.episodes_total)}
            </span>
          ) : live ? (
            <span className="nowrap" data-testid={`module-judged-${m.id}`}>
              {T().judgedSoFar(live.judged, m.episodes_total)}
            </span>
          ) : null}
          {section ? (
            <span className="nowrap">
              {T().flaggedCount(section.summary.flagged_episodes ?? null)}
              {LEVELS.filter((lv) => levels && levels[lv]).map((lv) => (
                <span key={lv} style={{ marginLeft: 6 }}>
                  <LevelTag level={lv} />
                  {levels![lv]}
                </span>
              ))}
            </span>
          ) : live ? (
            <span className="nowrap">{T().flaggedCount(live.flagged)}</span>
          ) : null}
          {errors ? (
            <Button type="text" size="mini" status="warning" onClick={() => toggle(errorsOpen, setErrorsOpen, m.id)}>
              {T().errorsCount(errors)} {errorsOpen.includes(m.id) ? '▴' : '▾'}
            </Button>
          ) : null}
          {m.error ? <span style={{ color: 'var(--c-danger)', fontSize: 12 }}>{m.error}</span> : null}
          <span className="module-stat-spacer" />
          {terminal && (m.episodes_error > 0 || m.state === 'failed') ? (
            <Button size="mini" type="text" disabled={Boolean(task.active_subtask)} onClick={() => retry(m)}>
              {T().retryModule}
            </Button>
          ) : null}
          {section ? (
            <Link to={`/tasks/${task.id}/report?section=${encodeURIComponent(m.id)}`} data-testid={`module-report-${m.id}`}>
              {T().toSection}
            </Link>
          ) : null}
        </div>
        {errorsOpen.includes(m.id) ? (
          <div className="module-stat-body">
            <ErrorEpisodes taskId={task.id} stage={logStageOf(plan, m.id, spec?.stage)} />
          </div>
        ) : null}
        {unfolded ? (
          <div className="module-stat-body">
            <FindingsView moduleId={m.id} section={section} />
          </div>
        ) : null}
      </div>
    );
  };
  return (
    <Card title={T().modulesTitle}>
      <div data-testid="module-stats">
        {selected.map(row)}
        {skipped.map((m) => (
          <div key={m.id} className="module-stat skipped" data-testid={`module-stat-${m.id}`}>
            <div className="module-stat-head">
              <span className="module-stat-pad" />
              <b className="muted">{moduleName(reg.data, m.id) || m.name}</b>
              <span className="nowrap">
                <span className="dot" style={{ background: MODULE_STATE_COLOR.skipped }} />
                {zh.moduleState.skipped}
              </span>
              <span className="muted" style={{ fontSize: 12 }}>
                {m.unavailable_reason ?? '—'}
              </span>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}
