import { Button, Input, Select, Spin, Tag } from '@arco-design/web-react';
import { IconMenuFold, IconMenuUnfold, IconSearch, IconSort } from '@arco-design/web-react/icon';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, unwrap } from '../../api/client';
import type { DatasetItem, VizDataset, VizEpisodeItem } from '../../api/types';
import { formatLabel } from '../../features/visualizer/Player';
import { fmtClock } from '../../lib/vizTime';
import { zh } from '../../locales/zh';

type Sort = 'index' | 'duration' | 'steps';

/** Every registration a visualizer reader serves (the picker; C4 `GET /datasets?viz=true`). */
export function useVizDatasets() {
  return useQuery({
    queryKey: ['datasets', { viz: true, page_size: 100 }],
    queryFn: () => unwrap(api().GET('/datasets', { params: { query: { viz: true, page: 1, page_size: 100 } } })),
  });
}

function useEpisodePages(datasetId: string | null, q: string, sort: Sort) {
  return useInfiniteQuery({
    queryKey: ['viz', 'dataset', datasetId, 'episodes', q, sort],
    enabled: !!datasetId,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(
        api().GET('/datasets/{id}/viz/episodes', {
          params: { path: { id: datasetId as string }, query: { q: q || undefined, sort, order: sort === 'duration' ? 'desc' : 'asc', limit: 100, cursor: pageParam } },
        }),
      ),
    getNextPageParam: (last) => (last.has_more && last.next_cursor ? last.next_cursor : undefined),
  });
}

/**
 * The format beside a dataset in the picker: an mcap dataset carries its mapping state whether the checks
 * read it or not; a dataset the checks refuse but the visualizer reads (Galaxea's LeRobot without an action
 * column) shows none here - the summary line says what it is once it is picked.
 */
function pickerFormat(d: Pick<DatasetItem, 'format' | 'viz_mapping'>): string {
  if (d.viz_mapping) return zh.format.mcap ?? 'mcap';
  return d.format === 'unsupported' ? '' : (zh.format[d.format] ?? d.format);
}

/**
 * Folds the rail (at the top right of the rail) or unfolds it (where the page shows it while the rail is
 * folded: before the player's title). The folded rail takes no room (2026-10-04).
 */
export function RailButton({ fold, onClick }: { fold: boolean; onClick: () => void }) {
  const label = fold ? zh.vizPage.railFold : zh.vizPage.railUnfold;
  return <Button type="text" size="small" className="vz-rail-btn" icon={fold ? <IconMenuFold /> : <IconMenuUnfold />} title={label} aria-label={label} onClick={onClick} />;
}

/**
 * The left rail (design doc 18 §5.0): the dataset picker (mcap without a confirmed mapping greyed,
 * with why), a summary, the filter and the sort, the episode list; a click plays that episode.
 */
export function EpisodeRail({
  datasetId,
  current,
  model,
  onDataset,
  onEpisode,
  onOrder,
  onFold,
}: {
  datasetId: string | null;
  current: number | null;
  model: VizDataset | undefined;
  onDataset: (id: string) => void;
  onEpisode: (index: number) => void;
  /** the episodes in the list's order (for 上一条 / 下一条) */
  onOrder: (indices: number[]) => void;
  onFold?: () => void;
}) {
  const datasets = useVizDatasets();
  const [q, setQ] = useState('');
  const [sort, setSort] = useState<Sort>('index');
  const [typed, setTyped] = useState('');
  useEffect(() => {
    const t = setTimeout(() => setQ(typed.trim()), 250);
    return () => clearTimeout(t);
  }, [typed]);
  const pages = useEpisodePages(datasetId, q, sort);
  const items: VizEpisodeItem[] = useMemo(() => pages.data?.pages.flatMap((p) => p.items ?? []) ?? [], [pages.data]);
  const total = pages.data?.pages[0]?.total ?? 0;
  useEffect(() => onOrder(items.map((e) => e.index)), [items, onOrder]);

  // the current row stays in view
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = list.current?.querySelector('.ep-row.cur') as HTMLElement | null;
    el?.scrollIntoView?.({ block: 'nearest' });
  }, [current, items.length]);

  const options: DatasetItem[] = datasets.data?.items ?? [];
  const primary = model?.annotation_sources.find((s) => s.primary && s.kind === 'segments');
  return (
    <aside className="vz-left" data-testid="vz-rail">
      <div className="vz-left-head">
        <div className="label-row">
          <span className="label">{zh.vizPage.dataset}</span>
          {onFold ? <RailButton fold onClick={onFold} /> : null}
        </div>
        <Select
          showSearch
          placeholder={zh.vizPage.pick}
          value={datasetId ?? undefined}
          onChange={(v: string) => onDataset(v)}
          loading={datasets.isLoading}
          filterOption={(input, option) => String((option as { props?: { extra?: string } })?.props?.extra ?? '').toLowerCase().includes(input.toLowerCase())}
          notFoundContent={<span className="muted">{zh.vizPage.noDatasets}</span>}
          aria-label={zh.vizPage.dataset}
        >
          {options.map((d) => (
            <Select.Option key={d.id} value={d.id} extra={d.name} disabled={d.viz?.state === 'unsupported'}>
              <span title={d.viz?.reason ?? d.name}>
                {d.name}
                {pickerFormat(d) ? <span className="muted">{` · ${pickerFormat(d)}`}</span> : null}
                {d.viz?.state === 'mapping_pending' ? (
                  <Tag size="small" color="orange" style={{ marginLeft: 6 }}>
                    {zh.vizPage.pendingTag}
                  </Tag>
                ) : null}
              </span>
            </Select.Option>
          ))}
        </Select>
      </div>
      {model ? (
        <div className="ds-meta">
          <b>{formatLabel(model.format)}</b>
          {` · ${zh.vizPage.summary(model.episode_count, model.cameras.length, model.fps, model.total_frames)}`}
          <br />
          {primary ? zh.vizPage.steps(primary.source || primary.name) : zh.vizPage.noSteps}
        </div>
      ) : null}
      <div className="ep-tools">
        {/* a search and a sort icon say what the two boxes are (requester, 2026-10-04) */}
        <Input size="small" allowClear prefix={<IconSearch />} placeholder={zh.vizPage.filter} value={typed} onChange={setTyped} aria-label={zh.vizPage.filter} />
        <Select size="small" prefix={<IconSort />} style={{ width: 132, flex: 'none' }} value={sort} onChange={(v: Sort) => setSort(v)} aria-label={zh.vizPage.sort.index}>
          {(['index', 'duration', 'steps'] as const).map((k) => (
            <Select.Option key={k} value={k}>
              {zh.vizPage.sort[k]}
            </Select.Option>
          ))}
        </Select>
      </div>
      <div className="ep-list" ref={list} role="list">
        {pages.isLoading && datasetId ? <Spin style={{ display: 'block', margin: '24px auto' }} /> : null}
        {!pages.isLoading && datasetId && !items.length ? <div className="ep-empty">{zh.vizPage.noMatch}</div> : null}
        {items.map((e) => {
          const steps = e.steps ?? [];
          return (
            <button
              key={e.index}
              type="button"
              role="listitem"
              className={`ep-row${e.index === current ? ' cur' : ''}`}
              title={zh.vizPage.rowTitle(e.index, e.frames, steps.length)}
              onClick={() => onEpisode(e.index)}
            >
              <span className="id">{`ep ${e.index}`}</span>
              <span className="dur">{e.duration_s !== null ? fmtClock(e.duration_s) : ''}</span>
              <span className={`task${e.task ? '' : ' none'}`}>{e.task || zh.vizPage.noTask}</span>
              {steps.length ? (
                <span className="steps">
                  {steps.map((s, i) => (
                    <i key={i} className={s.unqualified ? 'unq' : ''} />
                  ))}
                </span>
              ) : (
                <span className="steps none">
                  <i />
                </span>
              )}
            </button>
          );
        })}
        {pages.hasNextPage ? (
          <button type="button" className="ep-row" onClick={() => void pages.fetchNextPage()} disabled={pages.isFetchingNextPage}>
            <span className="task">{zh.vizPage.loadMore}</span>
          </button>
        ) : null}
      </div>
      <div className="vz-left-foot">
        <span>{datasetId ? zh.vizPage.count(total, items.length, !!q) : ''}</span>
      </div>
    </aside>
  );
}
