import { Button, Card, Message, Select, Space, Spin, Tabs, Tooltip, Typography } from '@arco-design/web-react';
import { useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { api, idempotencyKey, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { moduleName, qk, showSubtask, useModules, useTask } from '../../api/queries';
import type { AdjudicationCard, AdjudicationCounts, Task } from '../../api/types';
import { PageError } from '../../components/PageError';
import { PageHeader } from '../../components/PageHeader';
import { keepCard, ownQuestions, statusQuery, viewCard } from '../../lib/adjudication';
import { isTerminalState } from '../../lib/taskView';
import { zh } from '../../locales/zh';
import { AppealsTab } from './AppealsTab';
import { ApplyDialog, type RelabelRerun } from './ApplyDialog';
import { EpisodeCard } from './EpisodeCard';
import { useLoadAll, usePaged } from './paging';
import { useAdjudicationList, useDecisions, type AdjTab } from './useAdjudication';

function applyBlocked(task: Task, counts: AdjudicationCounts | null): string | null {
  if (!counts || counts.unapplied === 0) return zh.adjudication.applyNothing;
  if (task.active_subtask || !isTerminalState(task.state)) return zh.adjudication.applyBusy;
  return null;
}

/**
 * 人工裁决 (07 §6, F3.3): one card per episode with its source modules; every click is saved
 * (append only) and nothing changes until 执行裁决 builds the subtask (D10). Decisions belong to
 * this task only (D32).
 */
export function AdjudicationPage() {
  const { id = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const qc = useQueryClient();
  const reg = useModules();
  const task = useTask(id, (q) => (q.state.data?.active_subtask ? 5000 : false));
  const t = task.data;
  const rev = t?.result_rev ?? 0;
  const tab: AdjTab = params.get('tab') === 'appeals' ? 'appeals' : 'review';
  const sources = (params.get('source') ?? '').split(',').filter(Boolean);
  const [line, setLine] = useState('');
  const [statusFilter, setStatusFilter] = useState('pending');
  const sq = statusQuery(statusFilter);
  const review = useAdjudicationList(id, { tab: 'review', status: sq.status, source: sources.length === 1 ? sources[0] : null }, Boolean(t) && tab === 'review');
  const appeals = useAdjudicationList(id, { tab: 'appeals', status: 'all', source: sources.length === 1 ? sources[0] : null }, Boolean(t) && tab === 'appeals');
  const decisions = useDecisions(id);
  const [applyOpen, setApplyOpen] = useState(false);
  const [applying, setApplying] = useState(false);

  const reviewCards: AdjudicationCard[] = useMemo(() => (review.data?.pages ?? []).flatMap((p) => p.items ?? []), [review.data]);
  const appealCards: AdjudicationCard[] = useMemo(() => (appeals.data?.pages ?? []).flatMap((p) => p.items ?? []), [appeals.data]);
  // Review kinds, their titles and decisions come from the registry catalog (D43).
  const catalog = reg.data?.review_lines;
  const shown = reviewCards.filter((c) => keepCard(c, { sources, line, onlyUnsure: sq.onlyUnsure })).map((c) => viewCard(c, decisions.local, catalog));
  const appealViews = appealCards.filter((c) => keepCard(c, { sources, line: '', onlyUnsure: false })).map((c) => viewCard(c, decisions.local, catalog));
  // ten cards a page with a pager (requester, third round): the queue is fetched whole in the background
  useLoadAll(review);
  useLoadAll(appeals);
  const reviewPage = usePaged(shown, `${line}|${statusFilter}|${sources.join(',')}`, 'review-pager');
  const counts = decisions.counts ?? (tab === 'review' ? review.data?.pages[0]?.counts : appeals.data?.pages[0]?.counts) ?? review.data?.pages[0]?.counts ?? appeals.data?.pages[0]?.counts ?? null;
  // Each tab filters by its own source modules (third round): review items, or appealable rejects.
  const moduleOptions = (reg.data?.modules ?? []).filter((m) => (tab === 'appeals' ? m.appealable : m.produces_adjudication)).map((m) => ({ label: m.name_zh, value: m.id }));
  // The review tab asks about episodes still in passed; appeals (applies_to reject) have their own tab.
  const typeOptions = (catalog ?? []).filter((l) => l.applies_to === 'passed').map((l) => {
    const owner = reviewCards.flatMap(ownQuestions).find((q) => q.line === l.id)?.source_module;
    return { label: owner ? zh.adjudication.typeOption(l.title_zh, moduleName(reg.data, owner)) : l.title_zh, value: l.id };
  });

  const setParam = (key: string, value: string | null, drop: string[] = []) => {
    const next = new URLSearchParams(params);
    if (!value) next.delete(key);
    else next.set(key, value);
    for (const k of drop) next.delete(k);
    setParams(next, { replace: true });
  };
  // Another tab has other source modules: the filter starts over.
  const setTab = (k: AdjTab) => setParam('tab', k === 'appeals' ? 'appeals' : null, ['source']);
  const sourceFilter = (
    <>
      <span>{zh.adjudication.filterSource}</span>
      <Select
        mode="multiple"
        allowClear
        style={{ minWidth: 220 }}
        placeholder={zh.adjudication.all}
        aria-label={zh.adjudication.filterSource}
        value={sources}
        onChange={(v: string[]) => setParam('source', v.join(','))}
        options={moduleOptions}
      />
    </>
  );

  const apply = async (relabelRerun: RelabelRerun) => {
    setApplying(true);
    try {
      // The body is optional in C4 1.4; sending it always makes the choice explicit (D39).
      const r = await unwrap(api().POST('/tasks/{id}/adjudication/apply', { params: { path: { id }, header: { 'Idempotency-Key': idempotencyKey() } }, body: { relabel_rerun: relabelRerun } }));
      showSubtask(qc, id, r.subtask);
      Message.success(zh.actions.done.apply);
      setApplyOpen(false);
      decisions.reset();
      void qc.invalidateQueries({ queryKey: qk.task(id) });
      void qc.invalidateQueries({ queryKey: qk.tasksAll });
    } catch (e) {
      Message.error(errorMessage(e));
    } finally {
      setApplying(false);
    }
  };

  const crumbs = [{ label: zh.taskList.title, to: '/tasks' }, { label: t?.name ?? id, to: `/tasks/${id}` }, { label: zh.adjudication.title }];
  if (task.isError && !t) {
    return (
      <>
        <PageHeader crumbs={crumbs} title={zh.adjudication.title} />
        <PageError error={task.error} onRetry={() => void task.refetch()} />
      </>
    );
  }
  if (!t) return <Spin style={{ display: 'block', margin: '80px auto' }} />;
  const blocked = applyBlocked(t, counts);

  return (
    <div>
      <PageHeader
        crumbs={crumbs}
        title={zh.adjudication.title}
        docTitle={`${zh.adjudication.title} · ${t.name}`}
        description={zh.adjudication.desc(t.name, zh.report.revLabel(rev))}
        extra={
          <>
            <Link to={`/tasks/${id}/report`}>
              <Button>{zh.adjudication.openReport}</Button>
            </Link>
            <Link to={`/tasks/${id}`}>
              <Button>{zh.adjudication.back}</Button>
            </Link>
          </>
        }
      />
      <div className="adj-bar">
        <Space wrap style={{ justifyContent: 'space-between', width: '100%' }}>
          <Space direction="vertical" size={2}>
            <b data-testid="adj-counts">{counts ? zh.adjudication.counts(counts.decided, counts.pending, counts.unapplied) : '—'}</b>
            <span className="muted" style={{ fontSize: 12 }}>
              {counts?.unapplied ? zh.adjudication.unappliedHint(counts.unapplied) : zh.adjudication.saveHint}
            </span>
          </Space>
          {blocked ? (
            <Tooltip content={blocked}>
              <Button type="primary" disabled>
                {zh.adjudication.apply}
              </Button>
            </Tooltip>
          ) : (
            <Button type="primary" onClick={() => setApplyOpen(true)}>
              {zh.adjudication.apply}
            </Button>
          )}
        </Space>
      </div>
      <Tabs activeTab={tab} onChange={(k) => setTab(k === 'appeals' ? 'appeals' : 'review')}>
        <Tabs.TabPane key="review" title={zh.adjudication.tabReview}>
          <Space wrap style={{ marginBottom: 12 }}>
            {tab === 'review' ? sourceFilter : null}
            <span>{zh.adjudication.filterType}</span>
            <Select style={{ width: 260 }} aria-label={zh.adjudication.filterType} value={line} onChange={setLine} options={[{ label: zh.adjudication.all, value: '' }, ...typeOptions]} />
            <span>{zh.adjudication.filterStatus}</span>
            <Select
              style={{ width: 180 }}
              aria-label={zh.adjudication.filterStatus}
              value={statusFilter}
              onChange={setStatusFilter}
              options={Object.entries(zh.adjudication.statusOption).map(([value, label]) => ({ label, value }))}
            />
          </Space>
          {review.isError && !reviewCards.length ? (
            <PageError error={review.error} onRetry={() => void review.refetch()} />
          ) : review.isLoading ? (
            <Spin style={{ display: 'block', margin: '48px auto' }} />
          ) : !shown.length && !review.hasNextPage ? (
            <Card>
              <Typography.Text type="secondary" data-testid="review-empty">
                {reviewCards.length || statusFilter !== 'all' ? zh.adjudication.empty : zh.adjudication.emptyAll}
              </Typography.Text>
            </Card>
          ) : (
            <div className="card-gap" data-testid="cards">
              {reviewPage.slice.map((v) => (
                <EpisodeCard key={v.ep} taskId={id} rev={rev} view={v} catalog={catalog} onDecide={(l, d, label) => decisions.save(v.ep, l, d, label ?? null)} />
              ))}
              {reviewPage.pager}
            </div>
          )}
        </Tabs.TabPane>
        <Tabs.TabPane key="appeals" title={zh.adjudication.tabAppeals}>
          <AppealsTab
            taskId={id}
            rev={rev}
            views={appealViews}
            catalog={catalog}
            loading={appeals.isLoading || appeals.isFetchingNextPage}
            error={appeals.error}
            resetKey={sources.join(',')}
            filter={tab === 'appeals' ? sourceFilter : null}
            onRetry={() => void appeals.refetch()}
            onDecide={(ep, l, d, label) => decisions.save(ep, l, d, label ?? null)}
          />
        </Tabs.TabPane>
      </Tabs>
      {tab === 'review' ? (
        <div className="muted" style={{ textAlign: 'center', padding: 12, fontSize: 12 }} data-testid="adj-end">
          {review.isFetchingNextPage ? (
            <Spin size={14} />
          ) : review.isError && reviewCards.length ? (
            <>
              {zh.adjudication.loadRestFailed(reviewCards.length)}
              <Button type="text" size="mini" onClick={() => void review.fetchNextPage()}>
                {zh.common.retry}
              </Button>
            </>
          ) : !review.hasNextPage && reviewCards.length ? (
            zh.adjudication.loadedAll(reviewCards.length)
          ) : null}
        </div>
      ) : null}
      <ApplyDialog taskId={id} visible={applyOpen} local={decisions.local} catalog={catalog} busy={applying} onCancel={() => setApplyOpen(false)} onOk={(r) => void apply(r)} />
    </div>
  );
}
