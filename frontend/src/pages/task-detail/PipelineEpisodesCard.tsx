import { Button, Card, Empty, Space, Table, Tag, Typography } from '@arco-design/web-react';
import { IconLoading } from '@arco-design/web-react/icon';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api, unwrap } from '../../api/client';
import { qk } from '../../api/queries';
import { MiniPlayerModal } from '../../features/visualizer/MiniPlayerModal';
import type { operations } from '../../api/schema';
import type { EpisodeView, PipelineEpisode, Task } from '../../api/types';
import { rowStages, rowState } from '../../lib/pipelineRow';

type PipelinePage = operations['listPipelineEpisodes']['responses'][200]['content']['application/json'];
import { FUNNEL_STAGES, isTerminalState } from '../../lib/taskView';
import { zh } from '../../locales/zh';

const copy = zh.taskDetail.pipelineEpisodes;

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
 * Episode 流水线: the latest episodes through the funnel (PIPELINE_PAGE a page), refetched every 3 s while the task runs.
 * A refetch (or a new key when the task's state or revision changes) keeps the rows on screen and
 * only turns the spinner in the header: swapping the table for a spinner made the page jump. An
 * episode's link opens it in the mini player (requester, 2026-10-04: no card of its records below).
 */
/** Rows a page (sixth round: 20, was 30 - the page grew too long). */
export const PIPELINE_PAGE = 20;

/**
 * The mini player of a pipeline row: the episode's findings as chips and bands, read from the task's
 * current results the way the report's Episode 明细 reads them (requester, 2026-10-04); none while a run
 * has no results yet.
 */
function PipelineMini({ task, index, onClose }: { task: Task; index: number; onClose: () => void }) {
  const [focus, setFocus] = useState<number | null>(null);
  const rev = task.result_rev > 0 ? task.result_rev : null;
  const view = useQuery({
    queryKey: qk.episode(task.id, index, rev),
    queryFn: async () => (await unwrap(api().GET('/tasks/{id}/episodes/{index}', { params: { path: { id: task.id, index }, query: { rev: rev ?? undefined } } }))) as EpisodeView,
    enabled: rev !== null,
    retry: false,
  });
  const v = view.data ?? { episode_index: index, findings: [], dataset_id: task.dataset_id ?? null };
  return <MiniPlayerModal taskId={task.id} view={v} focus={focus} onFocus={setFocus} onClose={onClose} />;
}

export function PipelineEpisodesCard({ task }: { task: Task }) {
  const [before, setBefore] = useState<number | null>(null);
  const [mini, setMini] = useState<number | null>(null);
  const live = !isTerminalState(task.state) || Boolean(task.active_subtask);
  const page = useQuery({
    queryKey: ['task', task.id, 'pipeline-episodes', before, task.state, task.result_rev,
      Boolean(task.active_subtask)],
    queryFn: async () => (await unwrap(api().GET('/tasks/{id}/pipeline/episodes', {
      params: { path: { id: task.id }, query: { before: before ?? undefined, limit: PIPELINE_PAGE } },
    }))) as Omit<PipelinePage, 'items'> & { items: PipelineEpisode[] },
    enabled: Boolean(task.started_at),
    refetchInterval: live && before === null ? 3000 : false,
    placeholderData: keepPreviousData,
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
                <Button type="text" size="mini" title={zh.viz.mini.openTitle} onClick={() => setMini(ep)} data-testid={`pipeline-open-mini-${ep}`}>
                  {copy.episode(ep)}
                </Button>
              ) },
              { title: copy.columnStage, dataIndex: 'last_stage', render: (_: unknown, row: PipelineEpisode) => rowStages(row) },
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
                <Tag color={color(row)}>{rowState(row)}</Tag>
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
      {mini !== null ? <PipelineMini task={task} index={mini} onClose={() => setMini(null)} /> : null}
    </Card>
  );
}
