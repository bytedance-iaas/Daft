import { Button, Card, Pagination, Progress, Select, Spin, Typography } from '@arco-design/web-react';
import { IconPlus } from '@arco-design/web-react/icon';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { qk } from '../../api/queries';
import type { Overview } from '../../api/types';
import { Chart, barOption, chartSummary } from '../../components/Chart';
import { PageError } from '../../components/PageError';
import { PageHeader } from '../../components/PageHeader';
import { RelTime } from '../../components/RelTime';
import { StateTag } from '../../components/StateTag';
import { compactNumber, grouped, percent } from '../../lib/format';
import { OVERVIEW_PERIODS, activePageSize, readOverviewPeriod, writeOverviewPeriod, type OverviewPeriod } from '../../lib/overview';
import { stageLabel } from '../../lib/taskView';
import { zh } from '../../locales/zh';

function StatCell({ label, value, to, testId }: { label: string; value: ReactNode; to?: string; testId: string }) {
  const body = (
    <div className="stat-cell" style={{ cursor: to ? 'pointer' : 'default' }} data-testid={testId}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
  return to ? (
    <Link to={to} style={{ color: 'inherit' }}>
      {body}
    </Link>
  ) : (
    body
  );
}

/** The height of `ref`'s element, measured before paint and again whenever it changes. */
function useHeight<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [height, setHeight] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const measure = () => setHeight(el.clientHeight);
    measure();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, height] as const;
}

/**
 * 运行情况: the counts and the busy tasks with their progress. The card is as tall as the period
 * card next to it (styles.css «概览»); the list pages when the tasks do not fit in that height.
 */
function RunningCard({ running }: { running: Overview['running'] }) {
  const [area, height] = useHeight<HTMLDivElement>();
  const [page, setPage] = useState(1);
  const items = running.active;
  const size = activePageSize(height, items.length);
  const current = Math.min(page, Math.max(1, Math.ceil(items.length / size)));
  return (
    <Card className="overview-running" title={zh.overview.running}>
      <div className="stat-grid">
        <StatCell testId="run-running" label={zh.overview.runningCount} value={running.running} to="/tasks?state=running" />
        <StatCell testId="run-queued" label={zh.overview.queuedCount} value={running.queued} to="/tasks?state=queued" />
        <StatCell testId="run-paused" label={zh.overview.pausedCount} value={running.paused} to="/tasks?state=paused" />
      </div>
      <div ref={area} className="overview-active" data-testid="active-tasks">
        {!items.length ? <Typography.Text type="secondary">{zh.overview.noActive}</Typography.Text> : null}
        {items.slice((current - 1) * size, current * size).map((a) => (
          <div key={a.task.id} className="overview-active-row" data-testid="active-task">
            <div className="overview-active-head">
              <Link to={`/tasks/${a.task.id}`} className="overview-active-name" title={a.task.name}>
                {a.task.name}
              </Link>
              <StateTag state={a.task.state} size="small" />
              <span className="muted nowrap" style={{ fontSize: 12 }}>
                {a.stage ? stageLabel(a.stage) : ''} {a.done} / {a.total}
              </span>
            </div>
            <Progress percent={a.total ? Math.round((a.done / a.total) * 100) : 0} size="small" />
          </div>
        ))}
        {items.length > size ? (
          <div className="overview-pager">
            <Pagination simple size="mini" current={current} pageSize={size} total={items.length} onChange={setPage} />
          </div>
        ) : null}
      </div>
    </Card>
  );
}

/** The chosen period: what finished, how many episodes, the pass rate and the tokens per bucket. */
function PeriodCard({ recent, days, onDays, switching }: { recent: Overview['recent']; days: OverviewPeriod; onDays: (d: OverviewPeriod) => void; switching: boolean }) {
  const bars = recent.tokens_per_bucket.map((b) => ({ name: b.label, value: b.tokens }));
  const total = bars.reduce((a, b) => a + b.value, 0);
  const base = barOption(bars);
  // Token counts run to millions: size the grid to its axis labels instead of a fixed margin; with
  // 30 days or 13 weeks in a half-width card, date labels that would overlap are left out.
  const option = {
    ...base,
    grid: { ...(base.grid as object), left: 8, containLabel: true },
    xAxis: { ...(base.xAxis as object), axisLabel: { hideOverlap: true } },
  };
  return (
    <Card
      className="overview-period"
      title={
        <Select
          className="overview-period-select"
          aria-label={zh.overview.period}
          bordered={false}
          value={days}
          onChange={(v: OverviewPeriod) => onDays(v)}
          options={OVERVIEW_PERIODS.map((d) => ({ label: zh.overview.periods[d], value: d }))}
          triggerProps={{ autoAlignPopupWidth: false }}
        />
      }
      extra={switching ? <Spin size={14} /> : null}
    >
      <div className="stat-grid">
        <StatCell testId="recent-finished" label={zh.overview.finished} value={grouped(recent.tasks_finished)} />
        <StatCell testId="recent-episodes" label={zh.overview.episodesChecked} value={grouped(recent.episodes_checked)} />
        <StatCell testId="recent-pass" label={zh.overview.passRate} value={percent(recent.pass_rate)} />
        <StatCell testId="recent-tokens" label={zh.overview.tokens} value={compactNumber(total)} />
      </div>
      <div style={{ marginTop: 12 }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }} data-testid="tokens-chart-title">
          {zh.overview.tokensChart(recent.bucket)}
        </Typography.Text>
        <Chart option={option} height={160} summary={chartSummary(bars)} />
      </div>
    </Card>
  );
}

/**
 * 概览 (07 §4.3, D36; F6.1): two cards of one height side by side - 运行情况 and the chosen period
 * (近 7 天 / 近 1 月 / 近 3 月 / 近 1 年, remembered per browser).
 */
export function OverviewPage() {
  const navigate = useNavigate();
  const [days, setDays] = useState<OverviewPeriod>(readOverviewPeriod);
  const q = useQuery({
    queryKey: [...qk.overview, { days }],
    queryFn: () => unwrap(api().GET('/overview', { params: { query: { days } } })),
    placeholderData: keepPreviousData,
    refetchInterval: (qr) => {
      const r = qr.state.data?.running;
      return r && r.running + r.queued + r.paused > 0 ? 5000 : false;
    },
  });
  const choose = (d: OverviewPeriod) => {
    setDays(d);
    writeOverviewPeriod(d);
  };
  const o = q.data;
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
  return (
    <div>
      {header}
      <div className="card-gap">
        <div className="overview-cards">
          <div className="overview-running-cell">
            <RunningCard running={o.running} />
          </div>
          <PeriodCard recent={o.recent} days={days} onDays={choose} switching={q.isPlaceholderData} />
        </div>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {zh.overview.generatedAt} <RelTime ms={o.generated_at} />
          {live ? (
            <>
              {' · '}
              <span>{zh.overview.autoRefresh}</span>
            </>
          ) : null}
        </Typography.Text>
      </div>
    </div>
  );
}
