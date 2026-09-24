import { Button, Card, Empty, Form, Input, Radio, Space, Spin, Tag, Typography } from '@arco-design/web-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api, unwrap } from '../../api/client';
import { moduleName, qk, useModules } from '../../api/queries';
import type { AdjudicationLine, Decision, DecisionValue } from '../../api/types';
import { LazyVisible } from '../../components/LazyVisible';
import { RelTime } from '../../components/RelTime';
import { EefCpuEvidence, EefCpuTable, EefWindows } from '../../features/eef/EefRecord';
import { SignedImage } from '../../features/media/SignedMedia';
import { SyncedVideos } from '../../features/media/SyncedVideos';
import { answerOn, catalogLine, lineDecisions, lineTitle, repeats, type CardView, type EffectiveDecision, type ReviewCatalog } from '../../lib/adjudication';
import { zh } from '../../locales/zh';

export const STATUS_COLOR: Record<string, string> = { pending: 'arcoblue', optional: 'cyan', decided: 'green', unsure: 'orange', applied: 'gray' };

type Question = CardView['questions'][number];
type Decide = (line: AdjudicationLine, d: DecisionValue, label?: string) => Promise<boolean>;

/** The EEF module (C1 1.9): its question and its appealable rejects show its own record. */
const EEF = 'eef_video_consistency';

/** One episode of the card's revision (C4 `EpisodeView`), shared by the media and the EEF block. */
function useEpisode(taskId: string, ep: number, rev: number) {
  return useQuery({
    queryKey: qk.episode(taskId, ep, rev),
    queryFn: () => unwrap(api().GET('/tasks/{id}/episodes/{index}', { params: { path: { id: taskId, index: ep }, query: { rev } } })),
    retry: false,
  });
}

/**
 * Videos and evidence frames of one episode, loaded when the card scrolls into view (03 §7).
 * 「同时播放」 plays the cameras in sync and never starts by itself (F6.2). `skip`: modules whose
 * evidence a question of the card shows itself.
 */
function CardMedia({ taskId, ep, rev, skip = [] }: { taskId: string; ep: number; rev: number; skip?: readonly string[] }) {
  const reg = useModules();
  const q = useEpisode(taskId, ep, rev);
  if (q.isLoading) return <Spin size={16} />;
  if (!q.data) return null;
  const v = q.data;
  const evidence = (v.evidence ?? []).filter((e) => !skip.includes(e.module));
  const origin = v.videos[0]?.origin;
  return (
    <div data-testid={`media-${ep}`}>
      {v.videos.length ? (
        <SyncedVideos
          key={`${ep}-${rev}`}
          task={taskId}
          videos={v.videos}
          extra={origin ? <span className="muted" style={{ fontSize: 12 }}>{zh.report.videoFrom(zh.report.videoOrigin[origin] ?? origin)}</span> : null}
        />
      ) : (
        <Empty description={zh.report.videos} />
      )}
      {evidence.length ? (
        <div className="evidence-grid" style={{ marginTop: 8 }}>
          {evidence.map((e) => (
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

/** Who decided and when (the server's record), 已执行 once applied, or 已保存 for this session's click. */
function DecidedNote({ latest, effective }: { latest: Decision | null | undefined; effective: EffectiveDecision | null }) {
  if (effective?.applied) return <Tag size="small">{zh.adjudication.applied}</Tag>;
  if (latest && effective && latest.decision === effective.decision && (latest.new_label ?? null) === effective.new_label) {
    return (
      <span className="muted" style={{ fontSize: 12 }}>
        {latest.decided_by} · <RelTime ms={latest.decided_at} />
      </span>
    );
  }
  if (effective) return <span className="muted" style={{ fontSize: 12 }}>{zh.adjudication.saved}</span>;
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
        <DecidedNote latest={q.latest_decision} effective={q.effective} />
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
        <DecidedNote latest={q.latest_decision} effective={q.effective} />
      </Space>
      {view.discarded ? (
        <div className="field-note" style={{ color: 'var(--c-warning)' }}>
          {zh.adjudication.discarded}
        </div>
      ) : null}
    </div>
  );
}

/** The EEF module's CPU readings, the model's windows and the marked crops, once the card is in view. */
function EefEvidence({ taskId, ep, rev }: { taskId: string; ep: number; rev: number }) {
  const q = useEpisode(taskId, ep, rev);
  if (q.isLoading) return <Spin size={16} />;
  const record = q.data?.modules?.[EEF];
  if (!record) return <div className="muted">{zh.eefDetail.noRecord}</div>;
  return (
    <div data-testid={`eef-evidence-${ep}`}>
      <EefCpuTable record={record} />
      <EefWindows taskId={taskId} record={record} />
      <EefCpuEvidence taskId={taskId} record={record} />
    </div>
  );
}

type Located = { taskId: string; rev: number };

/**
 * EEF–视频一致性 (C1 1.9, design 12 D-E13): why the module could not settle it, then what it
 * measured and what the model said on each window next to the marked crops; 一致 keeps the
 * episode, 不一致 rejects it.
 */
function EefQuestion({ index, view, q, catalog, onDecide, taskId, rev }: { index: number; view: CardView; q: Question; catalog: ReviewCatalog | undefined; onDecide: Decide } & Located) {
  return (
    <div className={`question${view.discarded ? ' overridden' : ''}`} data-testid={`q-${view.ep}-eef_check`}>
      <QuestionHead index={index} q={q} catalog={catalog} />
      <div className="episode-line warn" style={{ margin: '4px 0' }}>
        <b>{zh.eefDetail.why}：</b>
        {q.reason}
      </div>
      <div style={{ margin: '4px 0 8px' }}>{zh.eefDetail.ask}</div>
      <LazyVisible placeholder={<div style={{ height: 80 }} />}>
        <EefEvidence taskId={taskId} ep={view.ep} rev={rev} />
      </LazyVisible>
      <Space wrap style={{ marginTop: 8 }}>
        <Choice view={view} q={q} catalog={catalog} fallback={['consistent', 'inconsistent', 'unsure']} onDecide={onDecide} />
        <DecidedNote latest={q.latest_decision} effective={q.effective} />
      </Space>
    </div>
  );
}

/** 被拒复议 (D42): why it was rejected — for dedup, which episode it duplicates; for the EEF module, its windows. */
function AppealQuestion({ index, view, q, catalog, onDecide, taskId, rev }: { index: number; view: CardView; q: Question; catalog: ReviewCatalog | undefined; onDecide: Decide } & Located) {
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
      {q.source_module === EEF ? (
        <LazyVisible placeholder={<div style={{ height: 80 }} />}>
          <EefEvidence taskId={taskId} ep={view.ep} rev={rev} />
        </LazyVisible>
      ) : null}
      <Space wrap>
        <Choice view={view} q={q} catalog={catalog} fallback={['restore', 'keep_rejected', 'unsure']} onDecide={onDecide} />
        <DecidedNote latest={q.latest_decision} effective={q.effective} />
      </Space>
    </div>
  );
}

/**
 * A follow-up the card gained (registry follow_ups, C4 1.5.1) — v1's optional task verdict after
 * adopting or rewriting a label: answered, the person's verdict stands; left open or 拿不准, the
 * episode is judged again with the new label. Only the follow-up's own decisions are offered.
 * The same block whether this session opened it or the server listed it (C4 1.5.2 `follow_up_of`).
 */
function FollowUpQuestion({ index, view, f, catalog, onDecide }: { index: number; view: CardView; f: CardView['followUps'][number]; catalog: ReviewCatalog | undefined; onDecide: Decide }) {
  const verdict = f.line === 'task_verdict';
  const title = verdict ? zh.adjudication.optionalVerdict : zh.adjudication.followUpTitle(lineTitle(catalog, f.line), f.optional);
  const desc = verdict ? zh.adjudication.optionalVerdictDesc : f.question?.reason;
  return (
    <div className="question" data-testid={`followup-${view.ep}-${f.line}`}>
      <b>{zh.adjudication.followUpHead(index, title)}</b>
      {desc ? (
        <div className="muted" style={{ fontSize: 12, margin: '4px 0 8px' }}>
          {desc}
        </div>
      ) : null}
      {verdict ? <div style={{ marginBottom: 8 }}>{zh.adjudication.verdictAsk(view.newLabel ?? '', true)}</div> : null}
      <Space wrap>
        <Radio.Group type="button" value={f.effective?.decision ?? ''} aria-label={title} onChange={(d: DecisionValue) => void onDecide(f.line, d)}>
          {f.decisions.map((d) => (
            <Radio key={d.const} value={d.const}>
              {d.title}
            </Radio>
          ))}
        </Radio.Group>
        <DecidedNote latest={f.decided} effective={f.effective} />
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
          <DecidedNote latest={q.latest_decision} effective={q.effective} />
        </Space>
      ) : (
        <Typography.Text type="warning">{zh.adjudication.unknownLine(q.line)}</Typography.Text>
      )}
    </div>
  );
}

/**
 * One card per episode (07 §6): the source modules, the videos once (both episodes for a dedup
 * appeal), then every question — label, task_verdict, reject_appeal and eef_check with their own
 * views, any other line from the registry catalog (D43); 「其它原因，整条弃用」 where the lines offer it.
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
  // Nothing to send when a click only repeats the answer in force (e.g. back to 采纳新标注 from
  // the rewrite box, or saving the same text): it would lapse the follow-up's answer (C1 follow_ups).
  const decide: Decide = async (line, d, label) => (repeats(answerOn(view, line), d, label ?? null) ? true : onDecide(line, d, label));
  const labelQ = view.questions.find((q) => q.line === 'label');
  const annotation = view.questions.find((q) => q.annotation)?.annotation;
  const duplicateOf = view.questions.find((q) => q.duplicate_of !== null && q.duplicate_of !== undefined)?.duplicate_of;
  const statusKey = view.optional && view.status === 'pending' ? 'optional' : view.status;
  const discardLine = view.discardOn ?? view.discardLine;
  const eefShown = view.questions.some((q) => q.line === 'eef_check' || (q.line === 'reject_appeal' && q.source_module === EEF));
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
        {duplicateOf !== undefined && duplicateOf !== null ? (
          <CompareMedia taskId={taskId} ep={view.ep} other={duplicateOf} rev={rev} />
        ) : (
          <CardMedia taskId={taskId} ep={view.ep} rev={rev} skip={eefShown ? [EEF] : []} />
        )}
      </LazyVisible>
      {view.questions.map((q, i) => {
        const props = { index: i, view, q, catalog, onDecide: decide };
        if (q.line === 'label') return <LabelQuestion key={q.line} {...props} />;
        if (q.line === 'task_verdict') return <VerdictQuestion key={q.line} {...props} />;
        if (q.line === 'reject_appeal') return <AppealQuestion key={q.line} {...props} taskId={taskId} rev={rev} />;
        if (q.line === 'eef_check') return <EefQuestion key={q.line} {...props} taskId={taskId} rev={rev} />;
        return <GenericQuestion key={q.line} {...props} />;
      })}
      {/* Follow-ups are shown only while the answer that opens them is in force (they lapse otherwise). */}
      {view.followUps
        .filter((f) => f.open && !view.discarded)
        .map((f, i) => (
          <FollowUpQuestion key={`fu-${f.line}`} index={view.questions.length + i} view={view} f={f} catalog={catalog} onDecide={decide} />
        ))}
      {view.status === 'unsure' ? (
        <Typography.Paragraph style={{ color: 'var(--c-warning)', fontSize: 12, margin: '8px 0 0' }}>{view.optional ? zh.adjudication.unsureNoteOptional : zh.adjudication.unsureNote}</Typography.Paragraph>
      ) : null}
      {discardLine ? (
        <div style={{ marginTop: 12, textAlign: 'right' }}>
          <Button status={view.discarded ? 'default' : 'danger'} type={view.discarded ? 'secondary' : 'outline'} onClick={() => void decide(discardLine, view.discarded ? 'unsure' : 'discard')}>
            {view.discarded ? zh.adjudication.undiscard : (catalogLine(catalog, discardLine)?.decisions.find((d) => d.const === 'discard')?.title ?? zh.adjudication.discard)}
          </Button>
        </div>
      ) : null}
    </Card>
  );
}
