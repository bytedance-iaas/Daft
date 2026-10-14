import { Button, Card, Form, Input, Radio, Space, Spin, Tag, Typography } from '@arco-design/web-react';
import { useQuery } from '@tanstack/react-query';
import { createContext, useContext, useState } from 'react';
import { api, unwrap } from '../../api/client';
import { moduleName, qk, useModules } from '../../api/queries';
import type { AdjudicationLine, Decision, DecisionValue, EpisodeView } from '../../api/types';
import { LazyVisible } from '../../components/LazyVisible';
import { RelTime } from '../../components/RelTime';
import { EefCpuEvidence, EefCpuTable, EefDatasetRecord, EefOutput, EefWindows } from '../../features/eef/EefRecord';
import { SignedImage } from '../../features/media/SignedMedia';
import { MiniPlayerModal } from '../../features/visualizer/MiniPlayerModal';
import { SyncEvidence } from './SyncQuestion';
import { answerOn, catalogLine, lineDecisions, lineTitle, repeats, type CardView, type EffectiveDecision, type ReviewCatalog } from '../../lib/adjudication';
import { codeName, codeSpec, itemLabel } from '../../lib/findings';
import { zh } from '../../locales/zh';
import { asLegacyRecord } from '../../lib/records';

export const STATUS_COLOR: Record<string, string> = { pending: 'arcoblue', optional: 'cyan', decided: 'green', unsure: 'orange', applied: 'gray' };

type Question = CardView['questions'][number];
type Decide = (line: AdjudicationLine, d: DecisionValue, label?: string) => Promise<boolean>;

/** The EEF module (C1 1.9): its question and its appealable rejects show its own record. */
const EEF = 'eef_video_consistency';

/** One episode of the card's revision (C4 `EpisodeView`), shared by the media and the EEF block. */
function useEpisode(taskId: string, ep: number, rev: number) {
  return useQuery({
    queryKey: qk.episode(taskId, ep, rev),
    queryFn: async () => (await unwrap(api().GET('/tasks/{id}/episodes/{index}', { params: { path: { id: taskId, index: ep }, query: { rev } } }))) as EpisodeView,
    retry: false,
  });
}

/**
 * The episode's 「可视化」 and its evidence frames, loaded when the card scrolls into view (03 §7). The
 * mini player (design doc 18 §4.6) opens on the first finding of the card's source modules. `skip`:
 * modules whose evidence a question of the card shows itself.
 */
function CardMedia({ taskId, ep, rev, skip = [], modules = [] }: { taskId: string; ep: number; rev: number; skip?: readonly string[]; modules?: readonly string[] }) {
  const reg = useModules();
  const q = useEpisode(taskId, ep, rev);
  const [mini, setMini] = useState<{ focus: number | null } | null>(null);
  if (q.isLoading) return <Spin size={16} />;
  if (!q.data) return null;
  const v = q.data;
  const evidence = (v.evidence ?? []).filter((e) => !skip.includes(e.module));
  const origin = v.videos[0]?.origin;
  // the player's chips: what the card is about - no info notes (AV-1 疑似错位, a weak camera); the sync check's
  // findings become one chip in the player itself (playerChips, 2026-10-13)
  const shown = { ...v, findings: (v.findings ?? []).filter((f) => f.level !== 'info') };
  const focusOf = () => {
    const i = shown.findings.findIndex((f) => modules.includes(f.module));
    return i >= 0 ? i : null;
  };
  return (
    <div data-testid={`media-${ep}`}>
      <Space size={8}>
        <Button type="primary" size="small" title={zh.viz.mini.openTitle} onClick={() => setMini({ focus: focusOf() })} data-testid={`open-mini-${ep}`}>
          {zh.viz.mini.open}
        </Button>
        {origin ? <span className="muted" style={{ fontSize: 12 }}>{zh.report.videoFrom(zh.report.videoOrigin[origin] ?? origin)}</span> : null}
      </Space>
      {mini ? <MiniPlayerModal taskId={taskId} view={shown} focus={mini.focus} onFocus={(i) => setMini({ focus: i })} onClose={() => setMini(null)} /> : null}
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

/**
 * A question's head: its source module and line, and the findings it asks about (C4 2.3.0, design doc 17 §4.4) -
 * each code by the registry's name, with its taxonomy item.
 */
/** How many modules asked on this card: a question names its module only when there is more than one. */
const CardSources = createContext(1);

function QuestionHead({ index, q, catalog }: { index: number; q: Question; catalog: ReviewCatalog | undefined }) {
  const reg = useModules();
  const codes = q.codes ?? [];
  const several = useContext(CardSources) > 1;
  return (
    <>
      <b>{zh.adjudication.questionHead(index, several ? moduleName(reg.data, q.source_module) : null, lineTitle(catalog, q.line))}</b>
      {codes.length ? (
        <div className="finding-meta" data-testid="question-codes">
          {codes.map((c) => `${codeName(reg.data, q.source_module, c)}（${itemLabel(reg.data, codeSpec(reg.data, q.source_module, c)?.item ?? null)}）`).join('、')}
        </div>
      ) : null}
    </>
  );
}

/** Buttons for one line's decisions (catalog titles), 整条弃用 left to the card's own button. */
function Choice({ view, q, catalog, fallback, newLabel, onDecide }: { view: CardView; q: Question; catalog: ReviewCatalog | undefined; fallback: readonly string[]; newLabel?: string; onDecide: Decide }) {
  const current = q.effective?.decision;
  const options = lineDecisions(catalog, q.line, fallback).filter((d) => d.const !== 'discard');
  return (
    <Radio.Group
      type="button"
      value={current && current !== 'discard' ? current : ''}
      disabled={view.discarded}
      aria-label={`${lineTitle(catalog, q.line)} ${zh.report.episode(view.ep)}`}
      onChange={(d: DecisionValue) => void onDecide(q.line, d, newLabel)}
    >
      {options.map((d) => (
        <Radio key={d.const} value={d.const}>
          {d.title}
        </Radio>
      ))}
    </Radio.Group>
  );
}

/**
 * 任务成败 (C1 1.3): one question - is this episode usable - and the person may rewrite the task
 * text in the same answer (`new_label`). The rewrite box opens with an earlier rewrite; the verdict
 * buttons send whatever the box holds. A verdict stands as it is; 拿不准 with a rewritten text leaves
 * the episode to the model, which judges it again under the new text (D39).
 */
function VerdictQuestion({ index, view, q, catalog, onDecide }: { index: number; view: CardView; q: Question; catalog: ReviewCatalog | undefined; onDecide: Decide }) {
  const [editing, setEditing] = useState(Boolean(view.newLabel));
  const [text, setText] = useState(view.newLabel ?? '');
  const typed = text.trim();
  const relabel = typed && typed !== (q.annotation ?? '') ? typed : undefined;
  const asked = relabel ?? q.annotation ?? '';
  return (
    <div className={`question${view.discarded ? ' overridden' : ''}`} data-testid={`q-${view.ep}-task_verdict`}>
      <QuestionHead index={index} q={q} catalog={catalog} />
      <div className="muted" style={{ fontSize: 12, margin: '4px 0 8px' }}>
        {q.reason}
      </div>
      <div style={{ marginBottom: 8 }} data-testid={`ask-${view.ep}`}>
        {zh.adjudication.verdictAsk(asked, Boolean(relabel))}
      </div>
      <Space wrap>
        <Choice view={view} q={q} catalog={catalog} fallback={['success', 'failure', 'unsure']} newLabel={relabel} onDecide={onDecide} />
        <DecidedNote latest={q.latest_decision} effective={q.effective} />
      </Space>
      {editing ? (
        <Form layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item label={zh.adjudication.customLabel} extra={zh.adjudication.customHelp} style={{ marginBottom: 0 }}>
            <Input
              value={text}
              onChange={setText}
              placeholder={zh.adjudication.customPlaceholder}
              aria-label={zh.adjudication.customLabel}
              disabled={view.discarded}
              style={{ width: 420 }}
              maxLength={500}
            />
          </Form.Item>
        </Form>
      ) : (
        <div style={{ marginTop: 8 }}>
          <Button size="small" type="text" disabled={view.discarded} onClick={() => setEditing(true)}>
            {zh.adjudication.custom}
          </Button>
        </div>
      )}
      {view.discarded ? (
        <div className="field-note" style={{ color: 'var(--c-warning)' }}>
          {zh.adjudication.discarded}
        </div>
      ) : null}
    </div>
  );
}

/** The EEF module's CPU readings, the model's windows and the marked crops, once the card is in view; then the
 * trajectory against the dataset's own record (design doc 12 §8.7), which only informs. */
function EefEvidence({ taskId, ep, rev }: { taskId: string; ep: number; rev: number }) {
  const q = useEpisode(taskId, ep, rev);
  const reg = useModules();
  if (q.isLoading) return <Spin size={16} />;
  const raw = q.data?.modules?.[EEF];
  const record = raw ? asLegacyRecord(raw, reg.data) : undefined;
  if (!record) return <div className="muted">{zh.eefDetail.noRecord}</div>;
  return (
    <div data-testid={`eef-evidence-${ep}`}>
      {/* registry 5.0 (design doc 25 §7.3): a card is a conflict - both sides first, their evidence below */}
      <EefOutput record={record} />
      <EefCpuTable record={record} />
      <EefWindows taskId={taskId} record={record} />
      <EefCpuEvidence taskId={taskId} record={record} />
      <EefDatasetRecord taskId={taskId} record={record} />
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

/**
 * 同步存疑 (registry 7.2): why the sync check could not settle it, each camera's readings, the curves and the
 * cameras side by side; 可用 keeps the episode, 确有错位 rejects it, 整条平移 keeps it for a correction to come (7.5).
 */
function SyncQuestion({ index, view, q, catalog, onDecide, taskId, rev }: { index: number; view: CardView; q: Question; catalog: ReviewCatalog | undefined; onDecide: Decide } & Located) {
  return (
    <div className={`question${view.discarded ? ' overridden' : ''}`} data-testid={`q-${view.ep}-sync_check`}>
      <QuestionHead index={index} q={q} catalog={catalog} />
      <div className="episode-line warn" style={{ margin: '4px 0' }}>
        <b>{zh.syncDetail.why}：</b>
        {q.reason}
      </div>
      <div style={{ margin: '4px 0 8px' }}>{zh.syncDetail.ask}</div>
      <LazyVisible placeholder={<div style={{ height: 80 }} />}>
        <SyncEvidence taskId={taskId} ep={view.ep} rev={rev} />
      </LazyVisible>
      <Space wrap style={{ marginTop: 8 }}>
        <Choice view={view} q={q} catalog={catalog} fallback={['usable', 'misaligned', 'shifted', 'unsure']} onDecide={onDecide} />
        <DecidedNote latest={q.latest_decision} effective={q.effective} />
      </Space>
    </div>
  );
}

/** 被拒复议 (D42): why it was rejected — for the EEF module, its windows. */
function AppealQuestion({ index, view, q, catalog, onDecide, taskId, rev }: { index: number; view: CardView; q: Question; catalog: ReviewCatalog | undefined; onDecide: Decide } & Located) {
  return (
    <div className="question" data-testid={`q-${view.ep}-reject_appeal`}>
      <QuestionHead index={index} q={q} catalog={catalog} />
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
 * One card per episode (07 §6): the source modules, the videos once, then every question — task_verdict,
 * reject_appeal and eef_check with their own views, any other line from the registry catalog (D43);
 * 「其它原因，整条弃用」 where the lines offer it.
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
  // Nothing to send when a click only repeats the answer in force, the rewritten text included.
  const decide: Decide = async (line, d, label) => (repeats(answerOn(view, line), d, label ?? null) ? true : onDecide(line, d, label));
  const annotation = view.questions.find((q) => q.annotation)?.annotation;
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
        <CardMedia taskId={taskId} ep={view.ep} rev={rev} skip={eefShown ? [EEF] : []} modules={view.sources} />
      </LazyVisible>
      <CardSources.Provider value={new Set(view.questions.map((q) => q.source_module)).size}>
      {view.questions.map((q, i) => {
        const props = { index: i, view, q, catalog, onDecide: decide };
        if (q.line === 'task_verdict') return <VerdictQuestion key={q.line} {...props} />;
        if (q.line === 'reject_appeal') return <AppealQuestion key={q.line} {...props} taskId={taskId} rev={rev} />;
        if (q.line === 'eef_check') return <EefQuestion key={q.line} {...props} taskId={taskId} rev={rev} />;
        if (q.line === 'sync_check') return <SyncQuestion key={q.line} {...props} taskId={taskId} rev={rev} />;
        return <GenericQuestion key={q.line} {...props} />;
      })}
      </CardSources.Provider>
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
