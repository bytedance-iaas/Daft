import { Alert, Button, Space, Spin, Tabs, Tag, Typography } from '@arco-design/web-react';
import { IconEdit } from '@arco-design/web-react/icon';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { pollInterval, useTaskEvents } from '../../api/events';
import { qk, useTask } from '../../api/queries';
import type { Task } from '../../api/types';
import { PageError } from '../../components/PageError';
import { PageHeader } from '../../components/PageHeader';
import { RelTime } from '../../components/RelTime';
import { StateTag } from '../../components/StateTag';
import { TaskActionButtons } from '../../features/tasks/TaskActionButtons';
import { useTaskActions } from '../../features/tasks/useTaskActions';
import { actionsFor, exportedBefore, isTerminalState, type ActionPlan } from '../../lib/taskView';
import { zh } from '../../locales/zh';
import { LogsTab } from './LogsTab';
import { OverviewTab } from './OverviewTab';
import { RenameDialog } from './RenameDialog';

/** A task is «live» while it or one of its subtasks can still change on its own. */
export function isLive(t: Task): boolean {
  return !isTerminalState(t.state) || Boolean(t.active_subtask);
}

/** Header actions: the list's plan without 「查看」 (we are on the page already). */
function headerPlan(t: Task): ActionPlan {
  const p = actionsFor(t);
  const more = p.more.filter((k) => k !== 'view');
  if (p.primary === 'view') return { ...p, primary: more[0] ?? null, more: more.slice(1) };
  return { ...p, more };
}

export function TaskDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const actions = useTaskActions();
  const [live, setLive] = useState(true);
  const mode = useTaskEvents(id, live);
  const task = useTask(id, pollInterval(mode, live));
  const [tab, setTab] = useState(location.hash === '#logs' ? 'logs' : 'overview');
  const [rename, setRename] = useState(false);
  const notice = (location.state as { notice?: string } | null)?.notice;

  useEffect(() => {
    if (task.data) setLive(isLive(task.data));
  }, [task.data]);

  const subtasks = useQuery({
    queryKey: qk.subtasks(id ?? ''),
    queryFn: () => unwrap(api().GET('/tasks/{id}/subtasks', { params: { path: { id: id! } } })),
    enabled: Boolean(id),
    refetchInterval: pollInterval(mode, live),
  });
  const timeline = useQuery({
    queryKey: qk.timeline(id ?? ''),
    queryFn: () => unwrap(api().GET('/tasks/{id}/timeline', { params: { path: { id: id! } } })),
    enabled: Boolean(id),
    refetchInterval: pollInterval(mode, live),
  });

  const crumbs = [{ label: zh.taskList.title, to: '/tasks' }, { label: task.data?.name ?? id ?? '' }];
  if (task.isError && !task.data) {
    return (
      <>
        <PageHeader crumbs={crumbs} title={zh.taskDetail.notFound} />
        <PageError error={task.error} onRetry={() => void task.refetch()} />
      </>
    );
  }
  if (!task.data) return <Spin style={{ display: 'block', margin: '80px auto' }} />;
  const t = task.data;
  const subs = subtasks.data?.items ?? [];
  const exported = exportedBefore(t.progress.stages, subs);
  const run = (key: Parameters<typeof actions.run>[0]) => actions.run(key, { id: t.id, name: t.name, held: t.summary?.held, deliveryUri: t.output.uri, exported });

  return (
    <div>
      <PageHeader
        crumbs={crumbs}
        title={t.name}
        docTitle={t.name}
        titleExtra={
          <Space size={6}>
            <StateTag state={t.state} pauseReason={t.pause_reason} />
            {t.pending_adjudication ? (
              <Link to={`/tasks/${t.id}/adjudication`}>
                <Tag color="arcoblue">{zh.taskList.pendingBadge(t.pending_adjudication)}</Tag>
              </Link>
            ) : null}
            {t.delivery_stale ? <Tag color="orange">{exported ? zh.taskList.deliveryStale : zh.taskList.deliveryNeverExported}</Tag> : null}
          </Space>
        }
        description={
          <Space split="·" wrap>
            <span className="mono">{t.id}</span>
            <span>
              {zh.taskDetail.createdAt('')}
              <RelTime ms={t.created_at} />
            </span>
            {t.note ? (
              <span>
                {zh.taskDetail.noteLabel}
                {t.note}
              </span>
            ) : null}
            <Button type="text" size="mini" icon={<IconEdit />} onClick={() => setRename(true)}>
              {zh.taskDetail.rename}
            </Button>
          </Space>
        }
        extra={<TaskActionButtons plan={headerPlan(t)} held={t.summary?.held} exported={exported} primaryType="primary" size="default" onAction={run} />}
      />
      <div className="card-gap">
        {notice ? <Alert type="info" content={notice} /> : null}
        {t.state_reason && (t.state === 'failed' || t.state === 'stopped' || t.state === 'completed_with_errors') ? (
          <Alert type="error" content={`${zh.taskDetail.stateReason}${t.state_reason}`} />
        ) : null}
        {t.active_subtask ? (
          <Alert type="info" content={zh.taskDetail.subtaskRunning(zh.taskDetail.subtaskKind[t.active_subtask.kind] ?? t.active_subtask.kind, zh.state[t.active_subtask.state])} />
        ) : null}
        {t.delivery_stale ? (
          <Alert
            type="warning"
            title={exported ? zh.taskDetail.stale : zh.taskDetail.staleNever}
            content={exported ? zh.taskDetail.staleDesc : zh.taskDetail.staleNeverDesc}
            action={
              <Button size="small" type="primary" disabled={Boolean(t.active_subtask)} onClick={() => run('export')}>
                {exported ? zh.actions.reexport : zh.actions.export}
              </Button>
            }
            data-testid="delivery-banner"
          />
        ) : null}
        {live ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }} data-testid="live-mode" data-mode={mode}>
            {mode === 'sse' ? zh.taskDetail.liveSse : zh.taskDetail.livePolling}
          </Typography.Text>
        ) : null}
        <Tabs
          activeTab={tab}
          onChange={(k) => {
            setTab(k);
            navigate({ hash: k === 'logs' ? '#logs' : '' }, { replace: true, state: location.state });
          }}
        >
          <Tabs.TabPane key="overview" title={zh.taskDetail.tabOverview}>
            <OverviewTab task={t} subtasks={subs} timeline={timeline.data?.items ?? []} />
          </Tabs.TabPane>
          <Tabs.TabPane key="logs" title={zh.taskDetail.tabLogs}>
            <LogsTab task={t} subtasks={subs} mode={mode} live={live} />
          </Tabs.TabPane>
        </Tabs>
      </div>
      <RenameDialog task={t} visible={rename} onClose={() => setRename(false)} />
      {actions.dialogs}
    </div>
  );
}
