import { Alert, Button, Collapse, Radio, Space, Table, Tag, Typography } from '@arco-design/web-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api, unwrap } from '../../api/client';
import { moduleName, qk, useModules } from '../../api/queries';
import type { AdjudicationLine, DecisionValue } from '../../api/types';
import { PageError } from '../../components/PageError';
import { Sentinel } from '../../components/LazyVisible';
import { EpisodeDrawer } from '../../features/report/EpisodeDrawer';
import type { CardView } from '../../lib/adjudication';
import { zh } from '../../locales/zh';

/**
 * 任务失败复议 (07 §6, rule 2): only rejects attributed to task_success are listed and can be
 * appealed; the page explains why the other rejects (physical / structural hard gates, dedup)
 * are final. The server checks the rule again.
 */
export function AppealsTab({
  taskId,
  rev,
  views,
  loading,
  error,
  hasMore,
  pages,
  onMore,
  onRetry,
  onDecide,
}: {
  taskId: string;
  rev: number;
  views: CardView[];
  loading: boolean;
  error: unknown;
  hasMore: boolean;
  pages: number;
  onMore: () => void;
  onRetry: () => void;
  onDecide: (ep: number, line: AdjudicationLine, d: DecisionValue) => Promise<boolean>;
}) {
  const reg = useModules();
  const [watching, setWatching] = useState<number | null>(null);
  const report = useQuery({
    queryKey: qk.report(taskId, rev),
    queryFn: () => unwrap(api().GET('/tasks/{id}/report', { params: { path: { id: taskId }, query: { rev } } })),
    enabled: rev > 0,
  });
  const reasons = report.data?.report.overview.reject_reasons ?? [];
  const others = reasons.filter((r) => r.module !== 'task_success' && r.count > 0);
  const total = reasons.reduce((n, r) => n + r.count, 0);
  const otherCount = others.reduce((n, r) => n + r.count, 0);
  return (
    <div className="card-gap">
      <Alert type="info" content={zh.adjudication.appealsIntro} />
      {otherCount ? (
        <Collapse>
          <Collapse.Item name="why" header={zh.adjudication.whyNotHere}>
            <Typography.Paragraph>{zh.adjudication.whyNotHereBody(total, otherCount)}</Typography.Paragraph>
            <ul style={{ marginTop: 0 }} data-testid="final-rejects">
              {others.map((r) => (
                <li key={r.module}>
                  {moduleName(reg.data, r.module)}：{r.count} 条
                </li>
              ))}
            </ul>
            <Typography.Paragraph type="secondary">{zh.adjudication.whyNotHereRule}</Typography.Paragraph>
          </Collapse.Item>
        </Collapse>
      ) : null}
      {error && !views.length ? (
        <PageError error={error} onRetry={onRetry} />
      ) : (
        <Table
          rowKey="ep"
          loading={loading}
          pagination={false}
          data={views}
          data-testid="appeals"
          noDataElement={<Typography.Text type="secondary">{zh.adjudication.appealsEmpty}</Typography.Text>}
          columns={[
            { title: zh.adjudication.colEpisode, dataIndex: 'ep', width: 90, render: (ep: number) => <b>{zh.report.episode(ep)}</b> },
            { title: zh.adjudication.colText, dataIndex: 'questions', render: (_: unknown, v: CardView) => <span className="mono">{v.questions[0]?.annotation ?? '—'}</span> },
            { title: zh.adjudication.colChain, dataIndex: 'sources', render: (_: unknown, v: CardView) => <span style={{ fontSize: 12 }}>{v.questions[0]?.reason}</span> },
            {
              title: zh.adjudication.colVideo,
              dataIndex: 'status',
              width: 90,
              render: (_: unknown, v: CardView) => (
                <Button type="text" size="mini" onClick={() => setWatching(v.ep)}>
                  {zh.adjudication.watch}
                </Button>
              ),
            },
            {
              title: zh.adjudication.colVerdict,
              dataIndex: 'unapplied',
              render: (_: unknown, v: CardView) => {
                const q = v.questions.find((x) => x.line === 'reject_appeal');
                const current = q?.effective?.decision;
                return (
                  <Space>
                    <Radio.Group
                      type="button"
                      size="small"
                      value={current ?? ''}
                      aria-label={`${zh.adjudication.colVerdict} ${zh.report.episode(v.ep)}`}
                      disabled={Boolean(q?.effective?.applied)}
                      onChange={(d: DecisionValue) => void onDecide(v.ep, 'reject_appeal', d)}
                    >
                      <Radio value="restore">{zh.adjudication.restore}</Radio>
                      <Radio value="keep_rejected">{zh.adjudication.keepRejected}</Radio>
                    </Radio.Group>
                    {q?.effective?.applied ? <Tag size="small">{zh.adjudication.applied}</Tag> : null}
                  </Space>
                );
              },
            },
          ]}
        />
      )}
      <Sentinel onVisible={onMore} disabled={!hasMore || loading} version={pages} />
      <EpisodeDrawer taskId={taskId} ep={watching} rev={rev} readOnly onClose={() => setWatching(null)} />
    </div>
  );
}
