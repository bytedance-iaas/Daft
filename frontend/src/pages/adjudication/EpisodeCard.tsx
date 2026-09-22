import { Button, Card, Empty, Form, Input, Radio, Space, Spin, Tag, Typography } from '@arco-design/web-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api, unwrap } from '../../api/client';
import { moduleName, qk, useModules } from '../../api/queries';
import type { AdjudicationLine, DecisionValue } from '../../api/types';
import { LazyVisible } from '../../components/LazyVisible';
import { RelTime } from '../../components/RelTime';
import { SignedImage, SignedVideo } from '../../features/media/SignedMedia';
import { catalogLine, lineDecisions, lineTitle, type CardView, type ReviewCatalog } from '../../lib/adjudication';
import { zh } from '../../locales/zh';

export const STATUS_COLOR: Record<string, string> = { pending: 'arcoblue', optional: 'cyan', decided: 'green', unsure: 'orange', applied: 'gray' };

type Question = CardView['questions'][number];
type Decide = (line: AdjudicationLine, d: DecisionValue, label?: string) => Promise<boolean>;

/** Videos and evidence frames of one episode, loaded when the card scrolls into view (03 §7). */
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
    <div data-testid={`media-${ep}`}>
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

/** A dedup appeal: this episode and the one it duplicates, side by side (07 §6, D42). */
function CompareMedia({ taskId, ep, other, rev }: { taskId: string; ep: number; other: number; rev: number }) {
  return (
    <div className="dup-compare" data-testid={`compare-${ep}`}>
      <div>
        <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>
          {zh.adjudication.thisEpisode(ep)}
        </div>
        <CardMedia taskId={taskId} ep={ep} rev={rev} />
      </div>
      <div>
        <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>
          {zh.adjudication.keptEpisode(other)}
        </div>
        <CardMedia taskId={taskId} ep={other} rev={rev} />
      </div>
    </div>
  );
}

function DecidedNote({ q }: { q: Question }) {
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

function QuestionHead({ index, q, catalog }: { index: number; q: Question; catalog: ReviewCatalog | undefined }) {
  const reg = useModules();
  return <b>{zh.adjudication.questionHead(index, moduleName(reg.data, q.source_module), lineTitle(catalog, q.line))}</b>;
}

function LabelQuestion({ index, view, q, catalog, onDecide }: { index: number; view: CardView; q: Question; catalog: ReviewCatalog | undefined; onDecide: Decide }) {
  const current = q.effective?.decision;
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(q.effective?.decision === 'custom_label' ? q.effective.new_label ?? '' : '');
  const [error, setError] = useState('');
  const selected = editing ? 'custom_label' : current && current !== 'discard' ? current : undefined;
  // Button titles from the catalog; 整条弃用 has its own button at the bottom of the card.
  const options = lineDecisions(catalog, 'label', ['adopt_suggestion', 'keep_label', 'unsure', 'custom_label']).filter((d) => d.const !== 'discard');
  return (
    <div className="question" data-testid={`q-${view.ep}-label`}>
      <QuestionHead index={index} q={q} catalog={catalog} />
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
          aria-label={lineTitle(catalog, 'label')}
          onChange={(v: DecisionValue) => {
            if (v === 'custom_label') {
              setEditing(true);
              return;
            }
            setEditing(false);
            void onDecide('label', v);
          }}
        >
          {options.map((d) => (
            <Radio key={d.const} value={d.const}>
              {d.title}
            </Radio>
          ))}
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

/** Buttons for one line's decisions (catalog titles), 整条弃用 left to the card's own button. */
function Choice({ view, q, catalog, fallback, onDecide }: { view: CardView; q: Question; catalog: ReviewCatalog | undefined; fallback: readonly string[]; onDecide: Decide }) {
  const current = q.effective?.decision;
  const options = lineDecisions(catalog, q.line, fallback).filter((d) => d.const !== 'discard');
  return (
    <Radio.Group
      type="button"
      value={current && current !== 'discard' ? current : ''}
      disabled={view.discarded}
      aria-label={`${lineTitle(catalog, q.line)} ${zh.report.episode(view.ep)}`}
      onChange={(d: DecisionValue) => void onDecide(q.line, d)}
    >
      {options.map((d) => (
        <Radio key={d.const} value={d.const}>
          {d.title}
        </Radio>
      ))}
    </Radio.Group>
  );
}

function VerdictQuestion({ index, view, q, catalog, onDecide }: { index: number; view: CardView; q: Question; catalog: ReviewCatalog | undefined; onDecide: Decide }) {
  const text = view.newLabel ?? q.annotation ?? '';
  return (
    <div className={`question${view.discarded ? ' overridden' : ''}`} data-testid={`q-${view.ep}-task_verdict`}>
      <QuestionHead index={index} q={q} catalog={catalog} />
      <div className="muted" style={{ fontSize: 12, margin: '4px 0 8px' }}>
        {q.reason}
      </div>
      <div style={{ marginBottom: 8 }} data-testid={`ask-${view.ep}`}>
        {zh.adjudication.verdictAsk(text, Boolean(view.newLabel))}
      </div>
      <Space wrap>
        <Choice view={view} q={q} catalog={catalog} fallback={['success', 'failure', 'unsure']} onDecide={onDecide} />
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

/** 被拒复议 (D42): why it was rejected — for dedup, which episode it duplicates. */
function AppealQuestion({ index, view, q, catalog, onDecide }: { index: number; view: CardView; q: Question; catalog: ReviewCatalog | undefined; onDecide: Decide }) {
  return (
    <div className="question" data-testid={`q-${view.ep}-reject_appeal`}>
      <QuestionHead index={index} q={q} catalog={catalog} />
      {q.duplicate_of !== null && q.duplicate_of !== undefined ? (
        <div style={{ margin: '4px 0' }} data-testid={`duplicate-${view.ep}`}>
          {zh.adjudication.duplicateOf(q.duplicate_of)}
        </div>
      ) : null}
      <div className="muted" style={{ fontSize: 12, margin: '4px 0 8px' }}>
        {q.reason}
      </div>
      <Space wrap>
        <Choice view={view} q={q} catalog={catalog} fallback={['restore', 'keep_rejected', 'unsure']} onDecide={onDecide} />
        <DecidedNote q={q} />
      </Space>
    </div>
  );
}

/** A line without a dedicated view (D43): its catalog title, the question's reason, one button per decision. */
function GenericQuestion({ index, view, q, catalog, onDecide }: { index: number; view: CardView; q: Question; catalog: ReviewCatalog | undefined; onDecide: Decide }) {
  const known = Boolean(catalogLine(catalog, q.line));
  return (
    <div className={`question${view.discarded ? ' overridden' : ''}`} data-testid={`q-${view.ep}-${q.line}`}>
      <QuestionHead index={index} q={q} catalog={catalog} />
      <div className="muted" style={{ fontSize: 12, margin: '4px 0 8px' }}>
        {q.reason}
      </div>
      {known ? (
        <Space wrap>
          <Choice view={view} q={q} catalog={catalog} fallback={[]} onDecide={onDecide} />
          <DecidedNote q={q} />
        </Space>
      ) : (
        <Typography.Text type="warning">{zh.adjudication.unknownLine(q.line)}</Typography.Text>
      )}
    </div>
  );
}

/**
 * One card per episode (07 §6): the source modules, the videos once (both episodes for a dedup
 * appeal), then every question — label, task_verdict and reject_appeal with their own views, any
 * other line from the registry catalog (D43); 「其它原因，整条弃用」 where the lines offer it.
 */
export function EpisodeCard({
  taskId,
  rev,
  view,
  catalog,
  onDecide,
}: {
  taskId: string;
  rev: number;
  view: CardView;
  catalog: ReviewCatalog | undefined;
  onDecide: Decide;
}) {
  const reg = useModules();
  const labelQ = view.questions.find((q) => q.line === 'label');
  const annotation = view.questions.find((q) => q.annotation)?.annotation;
  const duplicateOf = view.questions.find((q) => q.duplicate_of !== null && q.duplicate_of !== undefined)?.duplicate_of;
  const statusKey = view.optional && view.status === 'pending' ? 'optional' : view.status;
  const discardLine = view.discardOn ?? view.discardLine;
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
          <Tag color={STATUS_COLOR[statusKey]} data-testid={`status-${view.ep}`}>
            {zh.adjudication.status[statusKey]}
          </Tag>
        </Space>
      }
    >
      <LazyVisible placeholder={<div style={{ height: 120 }} />}>
        {duplicateOf !== undefined && duplicateOf !== null ? <CompareMedia taskId={taskId} ep={view.ep} other={duplicateOf} rev={rev} /> : <CardMedia taskId={taskId} ep={view.ep} rev={rev} />}
      </LazyVisible>
      {view.questions.map((q, i) => {
        const props = { index: i, view, q, catalog, onDecide };
        if (q.line === 'label') return <LabelQuestion key={q.line} {...props} />;
        if (q.line === 'task_verdict') return <VerdictQuestion key={q.line} {...props} />;
        if (q.line === 'reject_appeal') return <AppealQuestion key={q.line} {...props} />;
        return <GenericQuestion key={q.line} {...props} />;
      })}
      {view.status === 'unsure' ? (
        <Typography.Paragraph style={{ color: 'var(--c-warning)', fontSize: 12, margin: '8px 0 0' }}>{view.optional ? zh.adjudication.unsureNoteOptional : zh.adjudication.unsureNote}</Typography.Paragraph>
      ) : null}
      {discardLine ? (
        <div style={{ marginTop: 12, textAlign: 'right' }}>
          <Button status={view.discarded ? 'default' : 'danger'} type={view.discarded ? 'secondary' : 'outline'} onClick={() => void onDecide(discardLine, view.discarded ? 'unsure' : 'discard')}>
            {view.discarded ? zh.adjudication.undiscard : (catalogLine(catalog, discardLine)?.decisions.find((d) => d.const === 'discard')?.title ?? zh.adjudication.discard)}
          </Button>
        </div>
      ) : null}
    </Card>
  );
}
