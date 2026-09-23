import { Alert, Badge, Button, Card, Empty, Radio, Select, Space, Spin, Tag, Tooltip, Typography } from '@arco-design/web-react';
import { IconLeft, IconRight } from '@arco-design/web-react/icon';
import { keepPreviousData, useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { moduleName, qk, useModules } from '../../api/queries';
import type { EpisodeView, ModuleRegistry, Report, TaskEpisode, TaskEpisodePage } from '../../api/types';
import { SignedImage } from '../../features/media/SignedMedia';
import { SyncedVideos } from '../../features/media/SyncedVideos';
import { reasonLine, recordError } from '../../lib/reportView';
import { zh } from '../../locales/zh';
import { BlockError, EPISODE_BLOCKS, GenericBlock, blockTitleExtra } from './episodeBlocks';

export type EpisodeFilter = 'all' | 'passed' | 'reject' | 'held' | 'review';
export const EPISODE_FILTERS: EpisodeFilter[] = ['all', 'passed', 'reject', 'held', 'review'];
export const LIST_COLOR: Record<string, string> = { passed: 'green', reject: 'red', held: 'orange' };
const VERDICT_COLOR: Record<string, string> = { pass: 'green', fail: 'red', abstain: 'orange', scored: 'arcoblue', error: 'orangered' };
/** The order behind 上一条 / 下一条 is read this many episodes a page. */
const ORDER_PAGE = 500;
const SEARCH_LIMIT = 50;

const E = () => zh.episodeTab;

function filterQuery(f: EpisodeFilter): { list?: 'passed' | 'reject' | 'held'; review?: boolean } {
  if (f === 'all') return {};
  if (f === 'review') return { review: true };
  return { list: f };
}

function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const h = setTimeout(() => setV(value), ms);
    return () => clearTimeout(h);
  }, [value, ms]);
  return v;
}

function EpisodeOption({ item }: { item: TaskEpisode }) {
  return (
    <Space size={6}>
      <span className="mono">{zh.report.episode(item.episode_index)}</span>
      <Tag size="small" color={LIST_COLOR[item.list]}>
        {E().list[item.list] ?? item.list}
      </Tag>
      {item.review ? (
        <Tag size="small" color="arcoblue">
          {E().reviewBadge}
        </Tag>
      ) : null}
    </Space>
  );
}

/**
 * Picking an episode (F6.2): a search box over the episode list (the number, «12», «ep12» or
 * «ep 12»), a filter by list or open questions, 上一条 / 下一条 in the filtered order.
 */
function EpisodePicker({
  taskId,
  rev,
  ep,
  filter,
  onFilter,
  onSelect,
}: {
  taskId: string;
  rev: number;
  ep: number | null;
  filter: EpisodeFilter;
  onFilter: (f: EpisodeFilter) => void;
  onSelect: (ep: number) => void;
}) {
  const fq = filterQuery(filter);
  const order = useInfiniteQuery({
    queryKey: qk.episodes(taskId, rev, { ...fq, order: true }),
    queryFn: ({ pageParam }) => unwrap(api().GET('/tasks/{id}/episodes', { params: { path: { id: taskId }, query: { rev, limit: ORDER_PAGE, ...fq, ...(pageParam ? { cursor: pageParam } : {}) } } })),
    initialPageParam: null as string | null,
    getNextPageParam: (last: TaskEpisodePage) => last.next_cursor,
  });
  const items = useMemo(() => order.data?.pages.flatMap((p) => p.items) ?? [], [order.data]);
  const counts = order.data?.pages[0]?.counts;
  // The current episode's neighbours need the order up to just past it.
  const lastLoaded = items.length ? items[items.length - 1].episode_index : -1;
  useEffect(() => {
    if (ep !== null && order.hasNextPage && !order.isFetchingNextPage && !order.isFetchNextPageError && lastLoaded <= ep) void order.fetchNextPage();
  }, [ep, lastLoaded, order]);
  const prev = ep === null ? undefined : [...items].reverse().find((i) => i.episode_index < ep);
  const next = ep === null ? items[0] : items.find((i) => i.episode_index > ep);
  const inFilter = ep === null || items.some((i) => i.episode_index === ep) || order.hasNextPage;

  const [text, setText] = useState('');
  const q = useDebounced(text.trim());
  const search = useQuery({
    queryKey: qk.episodes(taskId, rev, { ...fq, q, limit: SEARCH_LIMIT }),
    queryFn: () => unwrap(api().GET('/tasks/{id}/episodes', { params: { path: { id: taskId }, query: { rev, limit: SEARCH_LIMIT, ...fq, ...(q ? { q } : {}) } } })),
    placeholderData: keepPreviousData,
    retry: false,
  });
  const options = search.data?.items ?? [];
  const label = (f: EpisodeFilter) => (counts ? E().filterCount(E().filter[f], f === 'all' ? counts.all : counts[f]) : E().filter[f]);
  return (
    <Card className="episode-picker">
      <Space wrap size={12} align="center">
        <Select
          showSearch
          allowClear={false}
          filterOption={false}
          style={{ width: 280 }}
          placeholder={E().searchPlaceholder}
          aria-label={E().search}
          value={ep ?? undefined}
          onSearch={setText}
          onChange={(v: number) => {
            setText('');
            onSelect(v);
          }}
          renderFormat={(_: unknown, value: unknown) => (typeof value === 'number' ? zh.report.episode(value) : String(value ?? ''))}
          notFoundContent={<span className="muted">{search.error ? errorMessage(search.error) : E().noMatch}</span>}
          loading={search.isFetching}
          data-testid="episode-search"
        >
          {options.map((item) => (
            <Select.Option key={item.episode_index} value={item.episode_index}>
              <EpisodeOption item={item} />
            </Select.Option>
          ))}
        </Select>
        <Radio.Group type="button" value={filter} onChange={(v: EpisodeFilter) => onFilter(v)} aria-label={E().filterLabel}>
          {EPISODE_FILTERS.map((f) => (
            <Radio key={f} value={f}>
              {label(f)}
            </Radio>
          ))}
        </Radio.Group>
        <Space size={8}>
          <Button icon={<IconLeft />} disabled={!prev} onClick={() => prev && onSelect(prev.episode_index)}>
            {E().prev}
          </Button>
          <Button disabled={!next} onClick={() => next && onSelect(next.episode_index)}>
            {E().next}
            <IconRight />
          </Button>
        </Space>
        {!inFilter ? <span className="muted">{E().notFiltered}</span> : null}
      </Space>
    </Card>
  );
}

/**
 * Whether a review item is a question the adjudication page asks (the registry's review lines, D43):
 * its module declares a line of that kind, or it is an appeal of an appealable module. Other
 * modules' abstentions are shown, never queued.
 */
export function askable(reg: ModuleRegistry | undefined, item: { module: string; kind?: string }): boolean {
  const spec = reg?.modules.find((m) => m.id === item.module);
  if (!spec || !item.kind) return false;
  if (item.kind === 'reject_appeal') return Boolean(spec.appealable);
  const lines = (reg?.review_lines ?? []).filter((l) => l.review_kind === item.kind).map((l) => l.id);
  return ((spec.review_lines ?? []) as string[]).some((id) => lines.includes(id));
}

function SummaryCard({ taskId, view, readOnly, review }: { taskId: string; view: EpisodeView; readOnly: boolean; review: boolean }) {
  const reg = useModules();
  // one 去裁决 per page the questions are on (module × tab)
  const seen = new Set<string>();
  const target = (r: { module: string; kind?: string }) => {
    if (!askable(reg.data, r)) return null;
    const appeal = r.kind === 'reject_appeal';
    const key = `${appeal ? 'appeals' : 'review'}|${r.module}`;
    if (seen.has(key)) return null;
    seen.add(key);
    return `/tasks/${taskId}/adjudication?${appeal ? 'tab=appeals&' : ''}source=${encodeURIComponent(r.module)}`;
  };
  return (
    <Card title={E().summary} size="small" data-testid="episode-summary">
      <Space direction="vertical" style={{ width: '100%' }}>
        <Space wrap>
          <Tag color={LIST_COLOR[view.list]} data-testid="episode-list">
            {E().list[view.list] ?? view.list}
          </Tag>
          {review ? <Badge text={E().reviewBadge} status="processing" /> : null}
        </Space>
        {view.reasons?.length ? (
          <div>
            <b>{E().reasons}</b>
            <ul className="episode-items" data-testid="episode-reasons">
              {view.reasons.map((r, i) => (
                <li key={i}>{reasonLine(r, reg.data)}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {view.review?.length ? (
          <div>
            <b>{E().review}</b>
            <ul className="episode-items" data-testid="episode-review">
              {view.review.map((r, i) => {
                const to = target(r);
                return (
                  <li key={i}>
                    <Space size={8} wrap>
                      <span>{reasonLine(r, reg.data)}</span>
                      {to ? (
                        readOnly ? (
                          <Tooltip content={zh.report.historyDisabled}>
                            <Button size="mini" disabled>
                              {E().goAdjudicate}
                            </Button>
                          </Tooltip>
                        ) : (
                          <Link to={to}>
                            <Button size="mini" type="primary">
                              {E().goAdjudicate}
                            </Button>
                          </Link>
                        )
                      ) : null}
                    </Space>
                  </li>
                );
              })}
            </ul>
          </div>
        ) : null}
        {view.task_text?.text ? (
          <div data-testid="episode-task-text">
            <b>{E().taskText}</b>：<span className="mono">{view.task_text.text}</span>
            {view.task_text.source ? <span className="muted">{E().taskSource(zh.sections.task.sourceNames[view.task_text.source] ?? view.task_text.source)}</span> : null}
          </div>
        ) : null}
      </Space>
    </Card>
  );
}

function ModuleBlocks({ taskId, rev, view, report, onSelect }: { taskId: string; rev: number; view: EpisodeView; report: Report | undefined; onSelect: (ep: number) => void }) {
  const reg = useModules();
  const ids = [...(report?.modules.map((m) => m.id) ?? []), ...Object.keys(view.modules).filter((id) => !report?.modules.some((m) => m.id === id))];
  return (
    <>
      {ids.map((id) => {
        const record = view.modules[id];
        const Block = EPISODE_BLOCKS[id] ?? GenericBlock;
        const advisory = reg.data?.modules.find((m) => m.id === id)?.affects_dataset_verdict === false;
        return (
          <Card
            key={id}
            size="small"
            className="episode-block"
            data-testid={`episode-module-${id}`}
            title={
              <Space size={8}>
                <b>{moduleName(reg.data, id)}</b>
                {record ? <Tag color={VERDICT_COLOR[record.verdict]}>{zh.report.verdict[record.verdict] ?? record.verdict}</Tag> : null}
                {advisory ? <span className="muted">{E().advisory}</span> : null}
              </Space>
            }
            extra={record ? blockTitleExtra(record) : null}
          >
            {!record ? (
              <Typography.Text type="secondary">{view.list === 'passed' ? E().noRecord : E().notReached}</Typography.Text>
            ) : record.verdict === 'error' ? (
              <BlockError text={recordError(record.error)} />
            ) : (
              <Block taskId={taskId} rev={rev} ep={view.episode_index} record={record} onEpisode={onSelect} />
            )}
          </Card>
        );
      })}
    </>
  );
}

/**
 * Episode 明细 (07 §5, F6.2): one episode at a time - its verdict, its cameras played in sync, its
 * evidence frames, then one block per module in report order with this episode's reading.
 * `?ep=N` picks it (else the first of the filtered order); the address can be shared.
 */
export function EpisodesTab({ taskId, rev, readOnly, report, ep, onSelect }: { taskId: string; rev: number; readOnly: boolean; report: Report | undefined; ep: number | null; onSelect: (ep: number) => void }) {
  const reg = useModules();
  const [filter, setFilter] = useState<EpisodeFilter>('all');
  // Without ?ep, the first episode of the filtered order.
  const first = useQuery({
    queryKey: qk.episodes(taskId, rev, { ...filterQuery(filter), limit: 1 }),
    queryFn: () => unwrap(api().GET('/tasks/{id}/episodes', { params: { path: { id: taskId }, query: { rev, limit: 1, ...filterQuery(filter) } } })),
    enabled: ep === null,
  });
  const current = ep ?? first.data?.items[0]?.episode_index ?? null;
  const view = useQuery({
    queryKey: qk.episode(taskId, current ?? -1, rev),
    queryFn: () => unwrap(api().GET('/tasks/{id}/episodes/{index}', { params: { path: { id: taskId, index: current! }, query: { rev } } })),
    enabled: current !== null,
    retry: false,
  });
  const listing = useQuery({
    queryKey: qk.episodes(taskId, rev, { q: current === null ? '' : String(current), limit: SEARCH_LIMIT }),
    queryFn: () => unwrap(api().GET('/tasks/{id}/episodes', { params: { path: { id: taskId }, query: { rev, limit: SEARCH_LIMIT, q: String(current) } } })),
    enabled: current !== null,
  });
  const review = Boolean(listing.data?.items.find((i) => i.episode_index === current)?.review);
  const v = view.data;
  let body;
  if (current === null) body = first.isLoading ? <Spin style={{ display: 'block', margin: '48px auto' }} /> : <Empty description={first.error ? errorMessage(first.error) : E().empty} />;
  else if (view.isLoading) body = <Spin style={{ display: 'block', margin: '48px auto' }} />;
  else if (!v) body = <Alert type="error" content={errorMessage(view.error)} data-testid="episode-error" />;
  else {
    const origin = v.videos[0]?.origin;
    body = (
      <div className="card-gap" data-testid="episode-view">
        <SummaryCard taskId={taskId} view={v} readOnly={readOnly} review={review} />
        <Card title={E().videos} size="small">
          {v.videos.length ? (
            <SyncedVideos
              key={`${v.episode_index}-${v.revision}`}
              task={taskId}
              videos={v.videos}
              extra={origin ? <span className="muted" style={{ fontSize: 12 }}>{zh.report.videoFrom(zh.report.videoOrigin[origin] ?? origin)}</span> : null}
            />
          ) : (
            <Empty />
          )}
        </Card>
        <Card title={E().evidence} size="small">
          {v.evidence?.length ? (
            <div className="evidence-grid">
              {v.evidence.map((e) => (
                <figure key={e.path} style={{ margin: 0 }}>
                  <SignedImage task={taskId} scope="delivery" path={e.path} alt={`${moduleName(reg.data, e.module)} · ${e.path.split('/').pop() ?? ''}`} />
                  <figcaption className="muted" style={{ fontSize: 12 }}>
                    {moduleName(reg.data, e.module)}
                  </figcaption>
                </figure>
              ))}
            </div>
          ) : (
            <Typography.Text type="secondary">{zh.report.evidenceNone}</Typography.Text>
          )}
        </Card>
        <ModuleBlocks taskId={taskId} rev={rev} view={v} report={report} onSelect={onSelect} />
      </div>
    );
  }
  return (
    <div className="card-gap" data-testid="episodes-tab">
      <EpisodePicker taskId={taskId} rev={rev} ep={current} filter={filter} onFilter={setFilter} onSelect={onSelect} />
      {body}
    </div>
  );
}
