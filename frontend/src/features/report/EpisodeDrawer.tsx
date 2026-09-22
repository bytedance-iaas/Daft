import { Alert, Button, Card, Drawer, Empty, Space, Spin, Table, Tag, Typography } from '@arco-design/web-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { moduleName, qk, useModules } from '../../api/queries';
import type { EpisodeView, ResultRecord } from '../../api/types';
import { detailsDigest, reasonLine, recordError, seconds } from '../../lib/reportView';
import { formatScalar } from '../../lib/summary';
import { zh } from '../../locales/zh';
import { SignedImage, SignedVideo } from '../media/SignedMedia';

export const LIST_COLOR: Record<string, string> = { passed: 'green', reject: 'red', held: 'orange' };
const VERDICT_COLOR: Record<string, string> = { pass: 'green', fail: 'red', abstain: 'orange', scored: 'arcoblue', error: 'orangered' };

function Readings({ view }: { view: EpisodeView }) {
  const reg = useModules();
  const order = (id: string) => {
    const i = reg.data?.modules.findIndex((m) => m.id === id) ?? -1;
    return i < 0 ? 999 : i;
  };
  const rows = Object.entries(view.modules)
    .map(([id, r]) => ({ id, ...r }))
    .sort((a, b) => order(a.id) - order(b.id));
  return (
    <Table
      rowKey="id"
      size="small"
      pagination={false}
      data={rows}
      data-testid="episode-readings"
      columns={[
        { title: zh.taskDetail.colModule, dataIndex: 'id', width: 140, render: (id: string) => moduleName(reg.data, id) },
        { title: zh.report.colVerdict, dataIndex: 'verdict', width: 100, render: (v: string) => <Tag color={VERDICT_COLOR[v]}>{zh.report.verdict[v] ?? v}</Tag> },
        { title: zh.report.colScore, dataIndex: 'score', width: 90, render: (v: number | null) => (v === null ? '—' : formatScalar(v)) },
        {
          title: zh.report.colDetails,
          dataIndex: 'details',
          render: (_: unknown, r: ResultRecord & { id: string }) =>
            r.verdict === 'error' ? <span style={{ color: 'var(--c-danger)' }}>{recordError(r.error)}</span> : <span className="muted">{detailsDigest(r.details)}</span>,
        },
        { title: zh.report.colElapsed, dataIndex: 'elapsed_s', width: 100, render: (v: number | null) => seconds(v) },
      ]}
    />
  );
}

/**
 * 逐条下钻 (07 §5, the v1 「轨迹」 page): one episode across all modules — verdict, readings,
 * evidence frames and every camera's video, playable together.
 */
export function EpisodeDrawer({ taskId, ep, rev, readOnly, onClose }: { taskId: string; ep: number | null; rev: number; readOnly?: boolean; onClose: () => void }) {
  const reg = useModules();
  const [playSignal, setPlaySignal] = useState(0);
  const q = useQuery({
    queryKey: qk.episode(taskId, ep ?? -1, rev),
    queryFn: () => unwrap(api().GET('/tasks/{id}/episodes/{index}', { params: { path: { id: taskId, index: ep! }, query: { rev } } })),
    enabled: ep !== null,
    retry: false,
  });
  const v = q.data;
  let body;
  if (q.isLoading) body = <Spin style={{ display: 'block', margin: '48px auto' }} />;
  else if (!v) body = <Alert type="error" content={errorMessage(q.error)} />;
  else {
    const reviewSource = v.review?.map((r) => r.module).find((m): m is string => typeof m === 'string');
    body = (
      <div className="card-gap">
        <Card title={zh.report.colVerdict} size="small">
          <Space direction="vertical" style={{ width: '100%' }}>
            <Space>
              <Tag color={LIST_COLOR[v.list]} data-testid="episode-list">
                {zh.report.list[v.list] ?? v.list}
              </Tag>
              {reviewSource && !readOnly ? (
                <Link to={`/tasks/${taskId}/adjudication?source=${encodeURIComponent(reviewSource)}`}>
                  <Button size="mini">{zh.report.goAdjudicateEp}</Button>
                </Link>
              ) : null}
            </Space>
            {v.reasons?.length ? (
              <div>
                <b>{zh.report.reasons}</b>
                <ul style={{ margin: '4px 0 0', paddingLeft: 18 }} data-testid="episode-reasons">
                  {v.reasons.map((r, i) => (
                    <li key={i}>{reasonLine(r, reg.data)}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {v.review?.length ? (
              <div>
                <b>{zh.report.review}</b>
                <ul style={{ margin: '4px 0 0', paddingLeft: 18 }} data-testid="episode-review">
                  {v.review.map((r, i) => (
                    <li key={i}>{reasonLine(r, reg.data)}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {v.task_text?.text ? (
              <div>
                <b>{zh.report.taskText}</b>：<span className="mono">{v.task_text.text}</span>
                {v.task_text.source ? <span className="muted">（{zh.report.taskTextSource(v.task_text.source)}）</span> : null}
              </div>
            ) : null}
          </Space>
        </Card>
        <Card
          title={zh.report.videos}
          size="small"
          extra={
            v.videos.length > 1 ? (
              <Button size="mini" type="primary" onClick={() => setPlaySignal((n) => n + 1)}>
                {zh.report.playAll}
              </Button>
            ) : null
          }
        >
          {v.videos.length ? (
            <div className="video-grid">
              {v.videos.map((video) => (
                <SignedVideo key={`${video.camera}-${video.path}`} task={taskId} video={video} playSignal={playSignal} caption={video.origin ? zh.report.videoOrigin[video.origin] : undefined} />
              ))}
            </div>
          ) : (
            <Empty />
          )}
        </Card>
        <Card title={zh.report.evidence} size="small">
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
        <Card title={zh.report.readings} size="small">
          <Readings view={v} />
        </Card>
      </div>
    );
  }
  return (
    <Drawer width={880} visible={ep !== null} title={ep !== null ? zh.report.episode(ep) : ''} onCancel={onClose} footer={null} unmountOnExit>
      {body}
    </Drawer>
  );
}
