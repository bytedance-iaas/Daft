import { Button, Card, Empty, Form, Input, Radio, Space, Spin, Tag, Typography } from '@arco-design/web-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api, unwrap } from '../../api/client';
import { moduleName, qk, useModules } from '../../api/queries';
import type { AdjudicationLine, DecisionValue } from '../../api/types';
import { LazyVisible } from '../../components/LazyVisible';
import { RelTime } from '../../components/RelTime';
import { SignedImage, SignedVideo } from '../../features/media/SignedMedia';
import type { CardView } from '../../lib/adjudication';
import { zh } from '../../locales/zh';

export const STATUS_COLOR: Record<string, string> = { pending: 'arcoblue', decided: 'green', unsure: 'orange', applied: 'gray' };

/** Videos and evidence frames of a card, loaded when the card scrolls into view (03 §7). */
function CardMedia({ taskId, ep, rev }: { taskId: string; ep: number; rev: number }) {
  const reg = useModules();
  const [playSignal, setPlaySignal] = useState(0);
  const q = useQuery({
    queryKey: qk.episode(taskId, ep, rev),
    queryFn: () => unwrap(api().GET('/tasks/{id}/episodes/{index}', { params: { path: { id: taskId, index: ep }, query: { rev } } })),
    retry: false,
  });
  if (q.isLoading) return <Spin size={16} />;
  if (!q.data) return null;
  const v = q.data;
  const origin = v.videos[0]?.origin;
  return (
    <div>
      {v.videos.length ? (
        <>
          <div className="video-grid">
            {v.videos.map((video) => (
              <SignedVideo key={`${video.camera}-${video.path}`} task={taskId} video={video} playSignal={playSignal} />
            ))}
          </div>
          <Space style={{ marginTop: 6 }}>
            {v.videos.length > 1 ? (
              <Button size="mini" onClick={() => setPlaySignal((n) => n + 1)}>
                {zh.report.playAll}
              </Button>
            ) : null}
            {origin ? <span className="muted" style={{ fontSize: 12 }}>{zh.report.videoFrom(zh.report.videoOrigin[origin] ?? origin)}</span> : null}
          </Space>
        </>
      ) : (
        <Empty description={zh.report.videos} />
      )}
      {v.evidence?.length ? (
        <div className="evidence-grid" style={{ marginTop: 8 }}>
          {v.evidence.map((e) => (
            <SignedImage key={e.path} task={taskId} scope="delivery" path={e.path} alt={`${moduleName(reg.data, e.module)} · ${e.path.split('/').pop() ?? ''}`} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function DecidedNote({ q }: { q: CardView['questions'][number] }) {
  const d = q.latest_decision;
  if (q.effective?.applied) return <Tag size="small">{zh.adjudication.applied}</Tag>;
  if (d && q.effective && d.decision === q.effective.decision && (d.new_label ?? null) === q.effective.new_label) {
    return (
      <span className="muted" style={{ fontSize: 12 }}>
        {d.decided_by} · <RelTime ms={d.decided_at} />
      </span>
    );
  }
  if (q.effective) return <span className="muted" style={{ fontSize: 12 }}>{zh.adjudication.saved}</span>;
  return null;
}

function LabelQuestion({ index, view, q, onDecide }: { index: number; view: CardView; q: CardView['questions'][number]; onDecide: (line: AdjudicationLine, d: DecisionValue, label?: string) => Promise<boolean> }) {
  const reg = useModules();
  const current = q.effective?.decision;
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(q.effective?.decision === 'custom_label' ? q.effective.new_label ?? '' : '');
  const [error, setError] = useState('');
  const selected = editing ? 'custom_label' : current && current !== 'discard' ? current : undefined;
  return (
    <div className="question" data-testid={`q-${view.ep}-label`}>
      <b>{zh.adjudication.questionHead(index, moduleName(reg.data, q.source_module), zh.adjudication.lineName.label)}</b>
      <div className="muted" style={{ fontSize: 12, margin: '4px 0 8px' }}>
        {q.reason}
      </div>
      <dl className="kv" style={{ margin: '0 0 8px' }}>
        <dt>{zh.adjudication.annotation}</dt>
        <dd className="mono">{q.annotation ?? '—'}</dd>
        <dt>{zh.adjudication.caption}</dt>
        <dd className="mono">{q.caption ?? '—'}</dd>
        {q.suggestion && q.suggestion !== q.caption ? (
          <>
            <dt>{zh.adjudication.suggestion}</dt>
            <dd className="mono">{q.suggestion}</dd>
          </>
        ) : null}
      </dl>
      <Space wrap>
        <Radio.Group
          type="button"
          value={selected ?? ''}
          disabled={view.discarded}
          aria-label={zh.adjudication.lineName.label}
          onChange={(v: DecisionValue) => {
            if (v === 'custom_label') {
              setEditing(true);
              return;
            }
            setEditing(false);
            void onDecide('label', v);
          }}
        >
          <Radio value="adopt_suggestion">{zh.adjudication.adopt}</Radio>
          <Radio value="keep_label">{zh.adjudication.keep}</Radio>
          <Radio value="unsure">{zh.adjudication.unsure}</Radio>
          <Radio value="custom_label">{zh.adjudication.custom}</Radio>
        </Radio.Group>
        <DecidedNote q={q} />
      </Space>
      {editing || current === 'custom_label' ? (
        <Form layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item label={zh.adjudication.customLabel} required validateStatus={error ? 'error' : undefined} help={error || undefined} style={{ marginBottom: 0 }}>
            <Space style={{ width: '100%' }} align="start">
              <Input
                value={text}
                onChange={(v) => {
                  setText(v);
                  setError('');
                }}
                placeholder={zh.adjudication.customPlaceholder}
                aria-label={zh.adjudication.customLabel}
                disabled={view.discarded}
                style={{ width: 420 }}
                maxLength={500}
              />
              <Button
                type="primary"
                disabled={view.discarded}
                onClick={async () => {
                  if (!text.trim()) {
                    setError(zh.adjudication.customRequired);
                    return;
                  }
                  if (await onDecide('label', 'custom_label', text.trim())) setEditing(false);
                }}
              >
                {zh.adjudication.customSave}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      ) : null}
    </div>
  );
}

function VerdictChoice({ view, value, withUnsure, onDecide }: { view: CardView; value: DecisionValue | undefined; withUnsure: boolean; onDecide: (line: AdjudicationLine, d: DecisionValue) => Promise<boolean> }) {
  return (
    <Radio.Group type="button" value={value ?? ''} disabled={view.discarded} aria-label={zh.adjudication.lineName.task_verdict} onChange={(v: DecisionValue) => void onDecide('task_verdict', v)}>
      <Radio value="success">{zh.adjudication.success}</Radio>
      <Radio value="failure">{zh.adjudication.failure}</Radio>
      {withUnsure ? <Radio value="unsure">{zh.adjudication.unsure}</Radio> : null}
    </Radio.Group>
  );
}

function VerdictQuestion({ index, view, q, onDecide }: { index: number; view: CardView; q: CardView['questions'][number]; onDecide: (line: AdjudicationLine, d: DecisionValue) => Promise<boolean> }) {
  const reg = useModules();
  const current = q.effective?.decision;
  const text = view.newLabel ?? q.annotation ?? '';
  return (
    <div className={`question${view.discarded ? ' overridden' : ''}`} data-testid={`q-${view.ep}-task_verdict`}>
      <b>{zh.adjudication.questionHead(index, moduleName(reg.data, q.source_module), zh.adjudication.lineName.task_verdict)}</b>
      <div className="muted" style={{ fontSize: 12, margin: '4px 0 8px' }}>
        {q.reason}
      </div>
      <div style={{ marginBottom: 8 }} data-testid={`ask-${view.ep}`}>
        {zh.adjudication.verdictAsk(text, Boolean(view.newLabel))}
      </div>
      <Space wrap>
        <VerdictChoice view={view} value={current && current !== 'discard' ? current : undefined} withUnsure onDecide={onDecide} />
        <DecidedNote q={q} />
      </Space>
      {view.discarded ? (
        <div className="field-note" style={{ color: 'var(--c-warning)' }}>
          {zh.adjudication.discarded}
        </div>
      ) : null}
    </div>
  );
}

/**
 * One card per episode (07 §6): the source modules, the videos once, then every question with
 * its own buttons; 「其它原因，整条弃用」 at the bottom overrides the verdict (rule 1).
 */
export function EpisodeCard({
  taskId,
  rev,
  view,
  onDecide,
}: {
  taskId: string;
  rev: number;
  view: CardView;
  onDecide: (line: AdjudicationLine, d: DecisionValue, label?: string) => Promise<boolean>;
}) {
  const reg = useModules();
  const labelQ = view.questions.find((q) => q.line === 'label');
  const annotation = view.questions.find((q) => q.annotation)?.annotation;
  const optionalVerdict = labelQ && !view.hasVerdictQuestion && view.newLabel;
  return (
    <Card
      className={`adj-card ${view.status}`}
      data-testid={`card-${view.ep}`}
      title={
        <Space wrap>
          <b>{zh.report.episode(view.ep)}</b>
          {annotation ? <span className="mono">{annotation}</span> : null}
          {labelQ?.priority ? <Tag size="small">{labelQ.priority}</Tag> : null}
        </Space>
      }
      extra={
        <Space>
          <span className="muted">{zh.adjudication.sources(view.sources.map((s) => moduleName(reg.data, s)).join('、'))}</span>
          <Tag color={STATUS_COLOR[view.status]} data-testid={`status-${view.ep}`}>
            {zh.adjudication.status[view.status]}
          </Tag>
        </Space>
      }
    >
      <LazyVisible placeholder={<div style={{ height: 120 }} />}>
        <CardMedia taskId={taskId} ep={view.ep} rev={rev} />
      </LazyVisible>
      {view.questions.map((q, i) =>
        q.line === 'label' ? (
          <LabelQuestion key={q.line} index={i} view={view} q={q} onDecide={onDecide} />
        ) : q.line === 'task_verdict' ? (
          <VerdictQuestion key={q.line} index={i} view={view} q={q} onDecide={onDecide} />
        ) : null,
      )}
      {optionalVerdict ? (
        <div className={`question${view.discarded ? ' overridden' : ''}`} data-testid={`optional-verdict-${view.ep}`}>
          <b>{`${'①②③④⑤'[view.questions.length] ?? ''} ${zh.adjudication.optionalVerdict}`}</b>
          <div className="muted" style={{ fontSize: 12, margin: '4px 0 8px' }}>
            {zh.adjudication.optionalVerdictDesc}
          </div>
          <VerdictChoice view={view} value={view.humanVerdict ?? undefined} withUnsure={false} onDecide={onDecide} />
        </div>
      ) : null}
      {view.status === 'unsure' ? (
        <Typography.Paragraph style={{ color: 'var(--c-warning)', fontSize: 12, margin: '8px 0 0' }}>{zh.adjudication.unsureNote}</Typography.Paragraph>
      ) : null}
      <div style={{ marginTop: 12, textAlign: 'right' }}>
        <Button
          status={view.discarded ? 'default' : 'danger'}
          type={view.discarded ? 'secondary' : 'outline'}
          onClick={() => void onDecide(view.discardOn ?? view.discardLine, view.discarded ? 'unsure' : 'discard')}
        >
          {view.discarded ? zh.adjudication.undiscard : zh.adjudication.discard}
        </Button>
      </div>
    </Card>
  );
}
