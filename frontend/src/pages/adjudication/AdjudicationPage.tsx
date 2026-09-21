import { Alert, Button, Card, Message, Modal, Select, Space, Spin, Tabs, Tooltip, Typography } from '@arco-design/web-react';
import { useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { api, idempotencyKey, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { moduleName, qk, useModules, useTask } from '../../api/queries';
import type { AdjudicationCard, AdjudicationCounts, DecisionValue, Task } from '../../api/types';
import { Sentinel } from '../../components/LazyVisible';
import { PageError } from '../../components/PageError';
import { PageHeader } from '../../components/PageHeader';
import { keepCard, statusQuery, viewCard, type CardView } from '../../lib/adjudication';
import { isTerminalState } from '../../lib/taskView';
import { zh } from '../../locales/zh';
import { AppealsTab } from './AppealsTab';
import { EpisodeCard } from './EpisodeCard';
import { useAdjudicationList, useDecisions, type AdjTab } from './useAdjudication';

const DECISION_TEXT: Record<DecisionValue, string> = {
  adopt_suggestion: '采纳新标注',
  custom_label: '改写标注',
  keep_label: '维持原标注',
  success: '判成功',
  failure: '判失败',
  restore: '恢复为可用',
  keep_rejected: '维持拒绝',
  unsure: '拿不准',
  discard: '整条弃用',
};

function describe(v: CardView): string {
  const parts = v.unapplied.map((u) => (u.decision === 'custom_label' && u.new_label ? `${DECISION_TEXT[u.decision]}「${u.new_label}」` : DECISION_TEXT[u.decision]));
  const effect = v.discarded || v.unapplied.every((u) => u.line === 'reject_appeal') ? '' : v.rerunsModel ? zh.adjudication.rerun : zh.adjudication.noRerun;
  return `${zh.report.episode(v.ep)}：${parts.join('，')}${effect ? ` → ${effect}` : ''}`;
}

function ApplyDialog({ visible, views, counts, busy, onCancel, onOk }: { visible: boolean; views: CardView[]; counts: AdjudicationCounts | null; busy: boolean; onCancel: () => void; onOk: () => void }) {
  const listed = views.filter((v) => v.unapplied.length);
  const more = Math.max(0, (counts?.unapplied ?? 0) - listed.length);
  return (
    <Modal title={zh.adjudication.applyTitle} visible={visible} onCancel={onCancel} onOk={onOk} confirmLoading={busy} okText={zh.adjudication.applyOk} cancelText={zh.common.cancel} unmountOnExit>
      <Typography.Paragraph>{zh.adjudication.applyIntro}</Typography.Paragraph>
      <ul data-testid="apply-list" style={{ paddingLeft: 18, maxHeight: 260, overflow: 'auto' }}>
        {listed.map((v) => (
          <li key={v.ep}>{describe(v)}</li>
        ))}
      </ul>
      {more ? <Typography.Paragraph type="secondary">{zh.adjudication.applyMore(more)}</Typography.Paragraph> : null}
      <Typography.Paragraph type="secondary">{zh.adjudication.applyNote}</Typography.Paragraph>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
        {zh.adjudication.applyChecks}
      </Typography.Paragraph>
    </Modal>
  );
}

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
  const appeals = useAdjudicationList(id, { tab: 'appeals', status: 'all', source: null }, Boolean(t) && tab === 'appeals');
  const decisions = useDecisions(id);
  const [applyOpen, setApplyOpen] = useState(false);
  const [applying, setApplying] = useState(false);

  const reviewCards: AdjudicationCard[] = useMemo(() => (review.data?.pages ?? []).flatMap((p) => p.items ?? []), [review.data]);
  const appealCards: AdjudicationCard[] = useMemo(() => (appeals.data?.pages ?? []).flatMap((p) => p.items ?? []), [appeals.data]);
  const shown = reviewCards.filter((c) => keepCard(c, { sources, line, onlyUnsure: sq.onlyUnsure })).map((c) => viewCard(c, decisions.local));
  const appealViews = appealCards.map((c) => viewCard(c, decisions.local));
  const counts = decisions.counts ?? (tab === 'review' ? review.data?.pages[0]?.counts : appeals.data?.pages[0]?.counts) ?? review.data?.pages[0]?.counts ?? appeals.data?.pages[0]?.counts ?? null;
  const moduleOptions = (reg.data?.modules ?? []).filter((m) => m.produces_adjudication).map((m) => ({ label: m.name_zh, value: m.id }));
  const typeOptions = (['label', 'task_verdict'] as const).map((l) => {
    const owner = reviewCards.flatMap((c) => c.questions).find((q) => q.line === l)?.source_module;
    return { label: owner ? zh.adjudication.typeOption(zh.adjudication.lineName[l], moduleName(reg.data, owner)) : zh.adjudication.lineName[l], value: l };
  });

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (!value) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };

  const apply = async () => {
    setApplying(true);
    try {
      await unwrap(api().POST('/tasks/{id}/adjudication/apply', { params: { path: { id }, header: { 'Idempotency-Key': idempotencyKey() } } }));
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
      <Alert type="info" content={zh.adjudication.ownOnly} style={{ marginBottom: 12 }} />
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
      <Tabs activeTab={tab} onChange={(k) => setParam('tab', k === 'appeals' ? 'appeals' : null)}>
        <Tabs.TabPane key="review" title={zh.adjudication.tabReview}>
          <Space wrap style={{ marginBottom: 12 }}>
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
              <Typography.Text type="secondary">{reviewCards.length || statusFilter !== 'all' ? zh.adjudication.empty : zh.adjudication.emptyAll}</Typography.Text>
            </Card>
          ) : (
            <div className="card-gap" data-testid="cards">
              {shown.map((v) => (
                <EpisodeCard key={v.ep} taskId={id} rev={rev} view={v} onDecide={(l, d, label) => decisions.save(v.ep, l, d, label ?? null)} />
              ))}
            </div>
          )}
        </Tabs.TabPane>
        <Tabs.TabPane key="appeals" title={zh.adjudication.tabAppeals}>
          <AppealsTab
            taskId={id}
            rev={rev}
            views={appealViews}
            loading={appeals.isLoading || appeals.isFetchingNextPage}
            error={appeals.error}
            hasMore={Boolean(appeals.hasNextPage)}
            pages={appeals.data?.pages.length ?? 0}
            onMore={() => void appeals.fetchNextPage()}
            onRetry={() => void appeals.refetch()}
            onDecide={(ep, l, d) => decisions.save(ep, l, d)}
          />
        </Tabs.TabPane>
      </Tabs>
      {tab === 'review' ? (
        <>
          <Sentinel onVisible={() => void review.fetchNextPage()} disabled={!review.hasNextPage || review.isFetchingNextPage} version={review.data?.pages.length ?? 0} />
          <div className="muted" style={{ textAlign: 'center', padding: 12, fontSize: 12 }} data-testid="adj-end">
            {review.isFetchingNextPage ? <Spin size={14} /> : !review.hasNextPage && reviewCards.length ? zh.adjudication.loadedAll(reviewCards.length) : null}
          </div>
        </>
      ) : null}
      <ApplyDialog visible={applyOpen} views={[...reviewCards.map((c) => viewCard(c, decisions.local)), ...appealViews]} counts={counts} busy={applying} onCancel={() => setApplyOpen(false)} onOk={() => void apply()} />
    </div>
  );
}
