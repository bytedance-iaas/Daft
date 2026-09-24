import { Card, Collapse, Space, Spin, Typography } from '@arco-design/web-react';
import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { api, unwrap } from '../../api/client';
import { moduleName, qk, useModules } from '../../api/queries';
import type { AdjudicationLine, DecisionValue } from '../../api/types';
import { PageError } from '../../components/PageError';
import type { CardView, ReviewCatalog } from '../../lib/adjudication';
import { zh } from '../../locales/zh';
import { EpisodeCard } from './EpisodeCard';
import { usePaged } from './paging';

/**
 * 被拒复议 (07 §6, rule 2, D42): rejects attributed to an appealable module (registry
 * `appealable`: today task_success — rejects attributed to it alone — and dedup), one card each.
 * The page explains why the other rejects are final (physical and structural gates, soft scores);
 * the server checks the rule again. Appeals are optional and never count as pending.
 */
export function AppealsTab({
  taskId,
  rev,
  views,
  catalog,
  loading,
  error,
  resetKey,
  filter,
  onRetry,
  onDecide,
}: {
  taskId: string;
  rev: number;
  views: CardView[];
  catalog: ReviewCatalog | undefined;
  loading: boolean;
  error: unknown;
  /** Other filters: back to page 1. */
  resetKey: string;
  /** The tab's own 来源模块 filter. */
  filter: ReactNode;
  onRetry: () => void;
  onDecide: (ep: number, line: AdjudicationLine, d: DecisionValue, label?: string) => Promise<boolean>;
}) {
  const reg = useModules();
  const paged = usePaged(views, resetKey, 'appeals-pager');
  const report = useQuery({
    queryKey: qk.report(taskId, rev),
    queryFn: () => unwrap(api().GET('/tasks/{id}/report', { params: { path: { id: taskId }, query: { rev } } })),
    enabled: rev > 0,
  });
  const appealable = (reg.data?.modules ?? []).filter((m) => m.appealable);
  const appealableNames = appealable.map((m) => m.name_zh).join('、');
  const reasons = report.data?.report.overview.reject_reasons ?? [];
  const final = reasons.filter((r) => r.count > 0 && !appealable.some((m) => m.id === r.module));
  const total = reasons.reduce((n, r) => n + r.count, 0);
  const finalCount = final.reduce((n, r) => n + r.count, 0);
  return (
    <div className="card-gap">
      <Space wrap>{filter}</Space>
      {finalCount ? (
        <Collapse>
          <Collapse.Item name="why" header={zh.adjudication.whyNotHere}>
            <Typography.Paragraph>{zh.adjudication.whyNotHereBody(total, finalCount)}</Typography.Paragraph>
            <ul style={{ marginTop: 0 }} data-testid="final-rejects">
              {final.map((r) => (
                <li key={r.module}>{zh.adjudication.finalReject(moduleName(reg.data, r.module), r.count)}</li>
              ))}
            </ul>
            <Typography.Paragraph type="secondary">{zh.adjudication.whyNotHereRule(appealableNames)}</Typography.Paragraph>
          </Collapse.Item>
        </Collapse>
      ) : null}
      {error && !views.length ? (
        <PageError error={error} onRetry={onRetry} />
      ) : loading && !views.length ? (
        <Spin style={{ display: 'block', margin: '48px auto' }} />
      ) : !views.length ? (
        <Card>
          <Typography.Text type="secondary" data-testid="appeals-empty">{zh.adjudication.empty}</Typography.Text>
        </Card>
      ) : (
        <div className="card-gap" data-testid="appeals">
          {paged.slice.map((v) => (
            <EpisodeCard key={v.ep} taskId={taskId} rev={rev} view={v} catalog={catalog} onDecide={(l, d, label) => onDecide(v.ep, l, d, label)} />
          ))}
          {paged.pager}
        </div>
      )}
    </div>
  );
}
