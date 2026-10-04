import { Alert, Badge, Button, Card, Empty, Radio, Select, Space, Spin, Tag, Tooltip, Typography } from '@arco-design/web-react';
import { IconLeft, IconRight } from '@arco-design/web-react/icon';
import { keepPreviousData, useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { moduleName, qk, useModules } from '../../api/queries';
import type { EpisodeView, ModuleRegistry, Report, ReportV2, TaskEpisode, TaskEpisodePage } from '../../api/types';
import { SignedImage } from '../../features/media/SignedMedia';
import { MiniPlayerModal } from '../../features/visualizer/MiniPlayerModal';
import { reasonLine, recordError } from '../../lib/reportView';
import { zh } from '../../locales/zh';
import { BlockError, EPISODE_BLOCKS, GenericBlock, blockTitleExtra } from './episodeBlocks';
import { asLegacyRecord, isRecordV2 } from '../../lib/records';
import { isAppealable, reviewLinesOf } from '../../lib/registry';
import { LEVELS, groupByLevel, itemLabel, levelTitle, moduleRole, unassessableTitle, type Level } from '../../lib/findings';
import { LevelTag } from '../../features/findings/FindingsView';
import { EpisodeFindings, questionTargets } from './EpisodeFindings';

export type EpisodeFilter = 'all' | 'passed' | 'reject' | 'held' | 'review';
export const EPISODE_FILTERS: EpisodeFilter[] = ['all', 'passed', 'reject', 'held', 'review'];
export const LIST_COLOR: Record<string, string> = { passed: 'green', reject: 'red', held: 'orange' };
const VERDICT_COLOR: Record<string, string> = { pass: 'green', fail: 'red', abstain: 'orange', scored: 'arcoblue', error: 'orangered' };
/** The order behind 上一条 / 下一条 is read this many episodes a page. */
const ORDER_PAGE = 500;
const SEARCH_LIMIT = 50;

const E = () => zh.episodeTab;

/** 按级别 / 按检测项 (design doc 17 §5.4): only on a findings revision (C4 2.3.0). */
export interface FindingFilter {
  level: Level | null;
  item: string | null;
}

const NO_FINDING_FILTER: FindingFilter = { level: null, item: null };

function filterQuery(f: EpisodeFilter, ff: FindingFilter = NO_FINDING_FILTER): { list?: 'passed' | 'reject' | 'held'; review?: boolean; level?: Level; item?: string } {
  const extra = { ...(ff.level ? { level: ff.level } : {}), ...(ff.item ? { item: ff.item } : {}) };
  if (f === 'all') return extra;
  if (f === 'review') return { review: true, ...extra };
  return { list: f, ...extra };
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
  findingFilter,
  onFindingFilter,
  v2,
  onSelect,
}: {
  taskId: string;
  rev: number;
  ep: number | null;
  filter: EpisodeFilter;
  onFilter: (f: EpisodeFilter) => void;
  findingFilter: FindingFilter;
  onFindingFilter: (f: FindingFilter) => void;
  /** the revision's report when it is one of the policy verdicts: its items are the 检测项 filter's */
  v2: ReportV2 | null;
  onSelect: (ep: number) => void;
}) {
  const reg = useModules();
  const fq = filterQuery(filter, findingFilter);
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
        {v2 ? (
          <>
            <Select
              style={{ width: 150 }}
              aria-label={zh.findings.filterLevel}
              value={findingFilter.level ?? ''}
              onChange={(x: string) => onFindingFilter({ ...findingFilter, level: (x || null) as Level | null })}
              options={[{ label: zh.findings.anyLevel, value: '' }, ...LEVELS.map((lv) => ({ label: levelTitle(reg.data, lv), value: lv }))]}
              data-testid="filter-level"
            />
            <Select
              style={{ width: 240 }}
              showSearch
              aria-label={zh.findings.filterItem}
              value={findingFilter.item ?? ''}
              onChange={(x: string) => onFindingFilter({ ...findingFilter, item: x || null })}
              options={[
                { label: zh.findings.anyItem, value: '' },
                ...v2.overview.findings_by_item.filter((f) => f.item && f.episodes).map((f) => ({ label: `${itemLabel(reg.data, f.item)}（${f.episodes}）`, value: f.item as string })),
              ]}
              data-testid="filter-item"
            />
          </>
        ) : null}
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
  if (item.kind === 'reject_appeal') return isAppealable(spec);
  const lines = (reg?.review_lines ?? []).filter((l) => l.review_kind === item.kind).map((l) => l.id);
  return reviewLinesOf(spec).some((id) => lines.includes(id));
}

function SummaryCard({
  taskId,
  view,
  readOnly,
  review,
  onOpen,
  onOpenAll,
}: {
  taskId: string;
  view: EpisodeView;
  readOnly: boolean;
  review: boolean;
  /** the mini player on a finding */
  onOpen: (index: number) => void;
  /** the mini player on the whole episode */
  onOpenAll: () => void;
}) {
  const reg = useModules();
  // A findings revision (C2 2.0, design doc 17 §5.4): every finding by level, the questions on them; the reasons
  // left are the ones no finding stands for (an execution error, a discard, a relabel waiting to be judged).
  const findings = view.findings;
  const reasons = findings ? (view.reasons ?? []).filter((r) => r.kind === 'execution_error' || !r.code) : view.reasons;
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
    <Card
      title={E().summary}
      size="small"
      data-testid="episode-summary"
      // 可视化 sits on the verdict's card (requester, 2026-10-04: no card of its own)
      extra={
        <Button type="primary" size="mini" title={zh.viz.mini.openTitle} onClick={onOpenAll} data-testid="open-mini">
          {zh.viz.mini.open}
        </Button>
      }
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <Space wrap>
          <Tag color={LIST_COLOR[view.list]} data-testid="episode-list">
            {E().list[view.list] ?? view.list}
          </Tag>
          {review ? <Badge text={E().reviewBadge} status="processing" /> : null}
        </Space>
        {reasons?.length ? (
          <div>
            <b>{E().reasons}</b>
            <ul className="episode-items" data-testid="episode-reasons">
              {reasons.map((r, i) => (
                <li key={i}>{reasonLine(r, reg.data)}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {findings ? (
          <EpisodeFindings taskId={taskId} findings={findings} targets={questionTargets(view.review, (r) => askable(reg.data, r))} readOnly={readOnly} onOpen={onOpen} />
        ) : null}
        {!findings && view.review?.length ? (
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

/** A module's block title on a findings revision: its findings on this episode by level, or none, or its error. */
function FindingTags({ view, id }: { view: EpisodeView; id: string }) {
  const raw = view.modules[id];
  if (!raw) return null;
  if (isRecordV2(raw) && raw.status === 'error') return <Tag color="orangered">{E().moduleError}</Tag>;
  const groups = groupByLevel((view.findings ?? []).filter((f) => f.module === id));
  const levels = LEVELS.filter((lv) => groups[lv].length);
  if (!levels.length) return <Tag color="green">{E().noFindings}</Tag>;
  return (
    <>
      {levels.map((lv) => (
        <span key={lv} className="nowrap">
          <LevelTag level={lv} />
          <span className="muted" style={{ fontSize: 12 }}>
            {zh.findings.count(groups[lv].length)}
          </span>
        </span>
      ))}
    </>
  );
}

function ModuleBlocks({ taskId, rev, view, report, v2, onSelect }: { taskId: string; rev: number; view: EpisodeView; report: Report | undefined; v2: ReportV2 | null; onSelect: (ep: number) => void }) {
  const reg = useModules();
  const ids = [...(report?.modules.map((m) => m.id) ?? []), ...Object.keys(view.modules).filter((id) => !report?.modules.some((m) => m.id === id))];
  const findingsView = Boolean(view.findings);
  return (
    <>
      {ids.map((id) => {
        const raw = view.modules[id];
        const record = raw ? asLegacyRecord(raw, reg.data) : undefined;
        const Block = EPISODE_BLOCKS[id] ?? GenericBlock;
        // only reports: what it finds never rejects or asks (its codes, or the task's policy)
        const advisory = findingsView && moduleRole(reg.data?.modules.find((m) => m.id === id), v2?.overview.policy.preset) === 'info';
        const unassessable = raw && isRecordV2(raw) && raw.status !== 'error' ? raw.unassessable : [];
        return (
          <Card
            key={id}
            size="small"
            className="episode-block"
            data-testid={`episode-module-${id}`}
            title={
              // no wrap: a wrapping Space gives every item an 8px bottom margin, which lifted the title
              // 4px above the header's middle (requester, 2026-10-04)
              <Space size={8} align="center">
                <b>{moduleName(reg.data, id)}</b>
                {findingsView ? (
                  <FindingTags view={view} id={id} />
                ) : record ? (
                  <Tag color={VERDICT_COLOR[record.verdict]}>{zh.report.verdict[record.verdict] ?? record.verdict}</Tag>
                ) : null}
                {advisory ? <span className="muted">{E().advisory}</span> : null}
              </Space>
            }
            extra={record ? blockTitleExtra(record) : null}
          >
            {!record ? (
              <Typography.Text type="secondary">{findingsView ? E().noResult : view.list === 'passed' ? E().noRecord : E().notReached}</Typography.Text>
            ) : record.verdict === 'error' ? (
              <BlockError text={recordError(record.error)} />
            ) : (
              <>
                {unassessable.length ? (
                  <ul className="episode-items" data-testid={`unassessable-${id}`}>
                    {unassessable.map((u, i) => (
                      <li key={i}>
                        <Tag size="small">{E().unassessable}</Tag> {itemLabel(reg.data, u.item)}：{u.message_zh || unassessableTitle(reg.data, u.reason)}
                      </li>
                    ))}
                  </ul>
                ) : null}
                <Block taskId={taskId} rev={rev} ep={view.episode_index} record={record} onEpisode={onSelect} />
              </>
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
export function EpisodesTab({
  taskId,
  rev,
  readOnly,
  report,
  v2 = null,
  ep,
  onSelect,
}: {
  taskId: string;
  rev: number;
  readOnly: boolean;
  report: Report | undefined;
  v2?: ReportV2 | null;
  ep: number | null;
  onSelect: (ep: number) => void;
}) {
  const reg = useModules();
  const [filter, setFilter] = useState<EpisodeFilter>('all');
  const [findingFilter, setFindingFilter] = useState<FindingFilter>(NO_FINDING_FILTER);
  // the mini player (design doc 18 §4.6): on a finding's moment, or on the whole episode
  const [mini, setMini] = useState<{ focus: number | null } | null>(null);
  const fq = filterQuery(filter, v2 ? findingFilter : NO_FINDING_FILTER);
  // Without ?ep, the first episode of the filtered order.
  const first = useQuery({
    queryKey: qk.episodes(taskId, rev, { ...fq, limit: 1 }),
    queryFn: () => unwrap(api().GET('/tasks/{id}/episodes', { params: { path: { id: taskId }, query: { rev, limit: 1, ...fq } } })),
    enabled: ep === null,
  });
  const current = ep ?? first.data?.items[0]?.episode_index ?? null;
  const view = useQuery({
    queryKey: qk.episode(taskId, current ?? -1, rev),
    queryFn: async () => (await unwrap(api().GET('/tasks/{id}/episodes/{index}', { params: { path: { id: taskId, index: current! }, query: { rev } } }))) as EpisodeView,
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
    // The EEF module's crops are shown in its block, next to the answer they belong to (F5.12).
    const evidence = (v.evidence ?? []).filter((e) => e.module !== 'eef_video_consistency');
    body = (
      <div className="card-gap" data-testid="episode-view">
        <SummaryCard taskId={taskId} view={v} readOnly={readOnly} review={review} onOpen={(i) => setMini({ focus: i })} onOpenAll={() => setMini({ focus: null })} />
        {mini ? <MiniPlayerModal taskId={taskId} view={v} focus={mini.focus} onFocus={(i) => setMini({ focus: i })} onClose={() => setMini(null)} /> : null}
        <Card title={E().evidence} size="small">
          {evidence.length ? (
            <div className="evidence-grid">
              {evidence.map((e) => (
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
        <ModuleBlocks taskId={taskId} rev={rev} view={v} report={report} v2={v2} onSelect={onSelect} />
      </div>
    );
  }
  return (
    <div className="card-gap" data-testid="episodes-tab">
      <EpisodePicker
        taskId={taskId}
        rev={rev}
        ep={current}
        filter={filter}
        onFilter={setFilter}
        findingFilter={findingFilter}
        onFindingFilter={setFindingFilter}
        v2={v2}
        onSelect={onSelect}
      />
      {body}
    </div>
  );
}
