import { Button, Card, Empty, Space, Table, Tag, Typography } from '@arco-design/web-react';
import { IconLoading } from '@arco-design/web-react/icon';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api, unwrap } from '../../api/client';
import type { PipelineEpisode, Task } from '../../api/types';
import { FUNNEL_STAGES, isTerminalState } from '../../lib/taskView';
import { zh } from '../../locales/zh';

const copy = zh.taskDetail.pipelineEpisodes;
const STAGE: Record<string, string> = copy.stage;
const VERDICT: Record<string, string> = copy.verdict;

function stateLabel(row: PipelineEpisode): string {
  if (row.reason === 'missing') return copy.missing;
  if (row.next_stage === 'done') return VERDICT[row.verdict ?? ''] ?? copy.finished;
  return copy.waiting(STAGE[row.next_stage] ?? row.next_stage);
}

function color(row: PipelineEpisode): string {
  if (row.verdict === 'drop') return 'red';
  if (row.verdict === 'held' || row.reason === 'missing') return 'orange';
  if (row.verdict === 'keep') return 'green';
  return 'arcoblue';
}

/** The card's refresh sign: a spinner in a fixed slot at the top right, so nothing moves (fourth round). */
function Refreshing({ on, testId }: { on: boolean; testId: string }) {
  return (
    <span className="refresh-slot" data-testid={testId} data-refreshing={on || undefined} aria-hidden>
      {on ? <IconLoading /> : null}
    </span>
  );
}

/**
 * Episode 流水线: the latest episodes through the funnel, refetched every 3 s while the task runs.
 * A refetch (or a new key when the task's state or revision changes) keeps the rows on screen and
 * only turns the spinner in the header: swapping the table for a spinner made the page jump.
 */
export function PipelineEpisodesCard({ task }: { task: Task }) {
  const [before, setBefore] = useState<number | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const live = !isTerminalState(task.state) || Boolean(task.active_subtask);
  const page = useQuery({
    queryKey: ['task', task.id, 'pipeline-episodes', before, task.state, task.result_rev,
      Boolean(task.active_subtask)],
    queryFn: () => unwrap(api().GET('/tasks/{id}/pipeline/episodes', {
      params: { path: { id: task.id }, query: { before: before ?? undefined, limit: 30 } },
    })),
    enabled: Boolean(task.started_at),
    refetchInterval: live && before === null ? 3000 : false,
    placeholderData: keepPreviousData,
  });
  const detail = useQuery({
    queryKey: ['task', task.id, 'pipeline-episode', selected, task.state, task.result_rev,
      Boolean(task.active_subtask)],
    queryFn: () => unwrap(api().GET('/tasks/{id}/pipeline/episodes/{index}', {
      params: { path: { id: task.id, index: selected! } },
    })),
    enabled: selected !== null,
    refetchInterval: live && selected !== null ? 3000 : false,
    // the same episode keeps its record while the key moves on; another one starts empty
    placeholderData: (previous, query) => (query?.queryKey[3] === selected ? previous : undefined),
  });
  const rows = page.data?.items ?? [];
  // the funnel's first layer sees every selected episode (data integrity when selected, else numeric)
  const funnel = FUNNEL_STAGES.filter((id) => task.progress.stages.some((s) => s.id === id));
  const total = task.progress.stages.find((s) => s.id === funnel[0])?.total
    ?? task.summary?.total ?? 0;
  return (
    <Card
      title={copy.title}
      data-testid="pipeline-episodes"
      extra={
        <Space size={8}>
          <Typography.Text type="secondary">{copy.count(page.data?.finished ?? 0, total || '—')}</Typography.Text>
          <Refreshing on={page.isFetching} testId="pipeline-episodes-refreshing" />
        </Space>
      }
    >
      <Typography.Paragraph type="secondary" style={{ marginTop: 0, fontSize: 12 }}>
        {copy.note}
      </Typography.Paragraph>
      {rows.length ? (
        <>
          <Table
            rowKey="episode_index"
            size="small"
            pagination={false}
            scroll={{ x: 780 }}
            data={rows}
            columns={[
              { title: copy.columnEpisode, dataIndex: 'episode_index', render: (ep: number) => (
                <Button type="text" size="mini" onClick={() => setSelected(ep)}>{copy.episode(ep)}</Button>
              ) },
              { title: copy.columnStage, dataIndex: 'last_stage', render: (stage: string) => STAGE[stage] ?? stage },
              ...(funnel.length ? funnel : FUNNEL_STAGES.slice(1)).map((stage) => ({
                title: copy.processingStage[stage] ?? stage,
                dataIndex: 'stage_processing_s.' + stage,
                render: (_: unknown, row: PipelineEpisode) => {
                  const took = (row.stage_processing_s as Record<string, number | undefined> | undefined)?.[stage];
                  return took != null ? took.toFixed(2) + ' s' : '—';
                },
              })),
              { title: copy.processingTotal, dataIndex: 'processing_s', render: (seconds: number | null | undefined) =>
                seconds != null ? seconds.toFixed(2) + ' s' : '—' },
              { title: copy.columnResult, dataIndex: 'next_stage', render: (_: unknown, row: PipelineEpisode) => (
                <Tag color={color(row)}>{stateLabel(row)}</Tag>
              ) },
            ]}
          />
          <Space style={{ marginTop: 10 }}>
            {before !== null ? <Button size="mini" onClick={() => setBefore(null)}>{copy.latest}</Button> : null}
            {page.data?.next_cursor !== null && page.data?.next_cursor !== undefined ? (
              <Button size="mini" onClick={() => setBefore(page.data!.next_cursor!)}>{copy.earlier}</Button>
            ) : null}
          </Space>
        </>
      ) : page.isLoading ? (
        <div className="muted" style={{ padding: '24px 0', textAlign: 'center' }}>{zh.common.loading}</div>
      ) : <Empty description={copy.empty} />}
      {selected !== null ? (
        <Card
          size="small"
          title={copy.detailTitle(selected)}
          extra={
            <Space size={8}>
              <Refreshing on={detail.isFetching} testId="pipeline-episode-refreshing" />
              <Button type="text" size="mini" onClick={() => setSelected(null)}>{copy.collapse}</Button>
            </Space>
          }
          style={{ marginTop: 14 }}
          data-testid="pipeline-episode-detail"
        >
          {detail.isLoading ? <span className="muted">{zh.common.loading}</span> : detail.data ? (
            <>
              <Typography.Paragraph>
                <Tag color={color(detail.data)}>{stateLabel(detail.data)}</Tag>
                {detail.data.verdict_reason ? ` ${detail.data.verdict_reason}` : ''}
              </Typography.Paragraph>
              {detail.data.processing_s != null ? <Typography.Paragraph type="secondary">
                {copy.processingTotal}：{detail.data.processing_s.toFixed(2)} s
              </Typography.Paragraph> : null}
              {Object.entries(detail.data.modules ?? {}).map(([module, record]) => (
                <div key={module} style={{ marginBottom: 10 }}>
                  <b>{module}</b> <Tag>{record.verdict}</Tag>
                  <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12, margin: '4px 0' }}>
                    {JSON.stringify(record.details, null, 2)}
                  </pre>
                </div>
              ))}
            </>
          ) : <Typography.Text type="secondary">{copy.unavailable}</Typography.Text>}
        </Card>
      ) : null}
    </Card>
  );
}
