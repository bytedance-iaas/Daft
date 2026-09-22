import { Button, Card, Empty, Grid, Popover, Progress, Space, Typography } from '@arco-design/web-react';
import { IconPlus } from '@arco-design/web-react/icon';
import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { qk } from '../../api/queries';
import type { TaskListItem } from '../../api/types';
import { Chart, barOption, chartSummary } from '../../components/Chart';
import { PageError } from '../../components/PageError';
import { PageHeader } from '../../components/PageHeader';
import { RelTime } from '../../components/RelTime';
import { StateTag } from '../../components/StateTag';
import { compactNumber, grouped, percent } from '../../lib/format';
import { stageLabel } from '../../lib/taskView';
import { zh } from '../../locales/zh';

const { Row, Col } = Grid;

function TodoCard({ label, value, foot, to, extra, testId }: { label: string; value: number; foot?: string; to?: string; extra?: ReactNode; testId: string }) {
  const body = (
    <div className="stat-cell" style={{ borderColor: value ? 'var(--c-warning)' : undefined, cursor: to ? 'pointer' : 'default' }} data-testid={testId}>
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={{ color: value ? 'var(--c-text-1)' : 'var(--c-text-3)' }}>
        {value}
      </div>
      {foot ? <div className="stat-foot">{foot}</div> : null}
      {extra}
    </div>
  );
  return to && value ? <Link to={to} style={{ color: 'inherit' }}>{body}</Link> : body;
}

/**
 * C4 cannot filter the task list by 「有待裁决」 or 「交付过期」, so the overview lists those tasks
 * itself from the newest page of tasks (see README «契约缺口»).
 */
function TaskPicker({ tasks, to, empty }: { tasks: TaskListItem[]; to: (t: TaskListItem) => string; empty: string }) {
  if (!tasks.length) return <Typography.Text type="secondary">{empty}</Typography.Text>;
  return (
    <ul style={{ margin: 0, paddingLeft: 18, maxHeight: 240, overflow: 'auto' }}>
      {tasks.map((t) => (
        <li key={t.id}>
          <Link to={to(t)}>{t.name}</Link>
          {t.pending_adjudication ? <span className="muted"> · {zh.taskList.pendingBadge(t.pending_adjudication)}</span> : null}
        </li>
      ))}
    </ul>
  );
}

/** 概览 (07 §4.3, D36): what needs attention, what runs, the last 7 days, datasets. */
export function OverviewPage() {
  const navigate = useNavigate();
  const q = useQuery({
    queryKey: qk.overview,
    queryFn: () => unwrap(api().GET('/overview')),
    refetchInterval: (qr) => {
      const r = qr.state.data?.running;
      return r && r.running + r.queued + r.paused > 0 ? 5000 : false;
    },
  });
  const o = q.data;
  const needTasks = Boolean(o && (o.todo.adjudication.tasks || o.todo.delivery_pending));
  const recentTasks = useQuery({
    queryKey: qk.tasks({ page: 1, pageSize: 100, purpose: 'overview' }),
    queryFn: () => unwrap(api().GET('/tasks', { params: { query: { page: 1, page_size: 100 } } })),
    enabled: needTasks,
  });
  const tasks = recentTasks.data?.items ?? [];
  const header = (
    <PageHeader
      crumbs={[{ label: zh.overview.title }]}
      title={zh.overview.title}
      description={zh.overview.desc}
      extra={
        <Button type="primary" icon={<IconPlus />} onClick={() => navigate('/tasks/new')}>
          {zh.overview.newTask}
        </Button>
      }
    />
  );
  if (q.isError && !o) {
    return (
      <>
        {header}
        <PageError error={q.error} onRetry={() => void q.refetch()} />
      </>
    );
  }
  if (!o) return header;
  const live = o.running.running + o.running.queued + o.running.paused > 0;
  const days = o.recent.tokens_per_day.map((d) => ({ name: d.date.slice(5), value: d.tokens }));
  const totalTokens = o.recent.tokens_per_day.reduce((a, d) => a + d.tokens, 0);
  const todoCount = o.todo.error_tasks + o.todo.adjudication.tasks + o.todo.delivery_pending + o.todo.datasets_changed + o.todo.credentials_failed + o.todo.backends_failed;
  return (
    <div>
      {header}
      <div className="card-gap">
        <Card title={zh.overview.todo} extra={live ? <span className="muted">{zh.overview.autoRefresh}</span> : null}>
          {!todoCount ? <Typography.Text type="secondary">{zh.overview.todoNone}</Typography.Text> : null}
          <div className="stat-grid">
            <TodoCard testId="todo-errors" label={zh.overview.errorTasks} value={o.todo.error_tasks} foot={zh.overview.errorTasksFoot} to="/tasks?state=completed_with_errors" />
            <TodoCard
              testId="todo-adjudication"
              label={zh.overview.adjudication}
              value={o.todo.adjudication.episodes}
              foot={zh.overview.adjudicationFoot(o.todo.adjudication.tasks)}
              extra={
                o.todo.adjudication.tasks ? (
                  <Popover trigger="click" content={<TaskPicker tasks={tasks.filter((t) => t.pending_adjudication > 0)} to={(t) => `/tasks/${t.id}/adjudication`} empty={zh.common.loading} />}>
                    <Button type="text" size="mini" style={{ padding: 0 }}>
                      {zh.overview.showTasks}
                    </Button>
                  </Popover>
                ) : null
              }
            />
            <TodoCard
              testId="todo-delivery"
              label={zh.overview.delivery}
              value={o.todo.delivery_pending}
              foot={zh.overview.deliveryFoot}
              extra={
                o.todo.delivery_pending ? (
                  <Popover trigger="click" content={<TaskPicker tasks={tasks.filter((t) => t.delivery_stale)} to={(t) => `/tasks/${t.id}`} empty={zh.common.loading} />}>
                    <Button type="text" size="mini" style={{ padding: 0 }}>
                      {zh.overview.showTasks}
                    </Button>
                  </Popover>
                ) : null
              }
            />
            <TodoCard testId="todo-datasets" label={zh.overview.datasetsChanged} value={o.todo.datasets_changed} foot={zh.overview.datasetsChangedFoot} to="/datasets?check_state=changed" />
            <TodoCard testId="todo-keys" label={zh.overview.keysFailed} value={o.todo.credentials_failed} to="/credentials" />
            <TodoCard testId="todo-backends" label={zh.overview.backendsFailed} value={o.todo.backends_failed} to="/credentials#vlm" />
          </div>
        </Card>
        <Row gutter={16}>
          <Col span={12}>
            <Card title={zh.overview.running} style={{ height: '100%' }}>
              <div className="stat-grid">
                <TodoCard testId="run-running" label={zh.overview.runningCount} value={o.running.running} to="/tasks?state=running" />
                <TodoCard testId="run-queued" label={zh.overview.queuedCount} value={o.running.queued} to="/tasks?state=queued" />
                <TodoCard testId="run-paused" label={zh.overview.pausedCount} value={o.running.paused} to="/tasks?state=paused" />
              </div>
              <div style={{ marginTop: 12 }} data-testid="active-tasks">
                {!o.running.active.length ? (
                  <Typography.Text type="secondary">{zh.overview.noActive}</Typography.Text>
                ) : (
                  o.running.active.map((a) => (
                    <div key={a.task.id} style={{ marginBottom: 8 }}>
                      <Space>
                        <Link to={`/tasks/${a.task.id}`}>{a.task.name}</Link>
                        <StateTag state={a.task.state} size="small" />
                        <span className="muted" style={{ fontSize: 12 }}>
                          {a.stage ? stageLabel(a.stage) : ''} {a.done} / {a.total}
                        </span>
                      </Space>
                      <Progress percent={a.total ? Math.round((a.done / a.total) * 100) : 0} size="small" />
                    </div>
                  ))
                )}
              </div>
            </Card>
          </Col>
          <Col span={12}>
            <Card title={zh.overview.recent} style={{ height: '100%' }}>
              <div className="stat-grid">
                <TodoCard testId="recent-finished" label={zh.overview.finished} value={o.recent.tasks_finished} />
                <div className="stat-cell" data-testid="recent-episodes">
                  <div className="stat-label">{zh.overview.episodesChecked}</div>
                  <div className="stat-value">{grouped(o.recent.episodes_checked)}</div>
                </div>
                <div className="stat-cell" data-testid="recent-pass">
                  <div className="stat-label">{zh.overview.passRate}</div>
                  <div className="stat-value">{percent(o.recent.pass_rate)}</div>
                </div>
                <div className="stat-cell" data-testid="recent-tokens">
                  <div className="stat-label">{zh.overview.tokens}</div>
                  <div className="stat-value">{compactNumber(totalTokens)}</div>
                </div>
              </div>
              {days.length ? (
                <div style={{ marginTop: 12 }}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {zh.overview.tokensChart}
                  </Typography.Text>
                  <Chart option={barOption(days)} height={160} summary={chartSummary(days)} />
                </div>
              ) : (
                <Empty />
              )}
            </Card>
          </Col>
        </Row>
        <Card title={zh.overview.datasets} extra={<Link to="/datasets">{zh.nav.datasets}</Link>}>
          <div className="stat-grid">
            <TodoCard testId="ds-total" label={zh.overview.datasetsTotal} value={o.datasets.total} to="/datasets" />
            <TodoCard testId="ds-changed" label={zh.overview.datasetsChanged} value={o.datasets.changed} to="/datasets?check_state=changed" />
          </div>
        </Card>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {zh.overview.generatedAt} <RelTime ms={o.generated_at} />
        </Typography.Text>
      </div>
    </div>
  );
}
