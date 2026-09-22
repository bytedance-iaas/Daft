import { Alert, Modal, Radio, Space, Spin, Typography } from '@arco-design/web-react';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { api, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { qk } from '../../api/queries';
import type { AdjudicationCard, AdjudicationPage, DecisionValue } from '../../api/types';
import { applySummary, decisionTitle, viewCard, type CardView, type LocalDecisions, type ReviewCatalog } from '../../lib/adjudication';
import { zh } from '../../locales/zh';

export type RelabelRerun = 'v1' | 'full';

function describe(v: CardView, catalog: ReviewCatalog | undefined): string {
  const text = (line: string, d: DecisionValue) => (d === 'custom_label' ? zh.adjudication.decisionText.custom_label : decisionTitle(catalog, line, d));
  const parts = v.unapplied.map((u) => (u.decision === 'custom_label' && u.new_label ? `${text(u.line, u.decision)}「${u.new_label}」` : text(u.line, u.decision)));
  const effect = v.discarded || v.unapplied.every((u) => u.line === 'reject_appeal') ? '' : v.rerunsModel ? zh.adjudication.rerun : zh.adjudication.noRerun;
  return `${zh.report.episode(v.ep)}：${parts.join('，')}${effect ? ` → ${effect}` : ''}`;
}

/** Every card with unapplied decisions, both tabs, whatever the page's filters (cursor pages). */
async function unappliedCards(taskId: string): Promise<AdjudicationCard[]> {
  const out: AdjudicationCard[] = [];
  for (const tab of ['review', 'appeals'] as const) {
    let cursor: string | null = null;
    do {
      const page: AdjudicationPage = await unwrap(
        api().GET('/tasks/{id}/adjudication', { params: { path: { id: taskId }, query: { tab, status: 'unapplied', limit: 200, ...(cursor ? { cursor } : {}) } } }),
      );
      out.push(...(page.items ?? []));
      cursor = page.has_more ? page.next_cursor ?? null : null;
    } while (cursor);
  }
  return out;
}

/**
 * 执行裁决 (07 §6, D39): how many decisions will be applied, how many relabels are judged again,
 * that relabels a person already judged are not, and — when there are relabels to re-judge — the
 * protocol: v1's two layers by default, or the first run's full flow.
 */
export function ApplyDialog({
  taskId,
  visible,
  local,
  catalog,
  busy,
  onCancel,
  onOk,
}: {
  taskId: string;
  visible: boolean;
  local: LocalDecisions;
  catalog: ReviewCatalog | undefined;
  busy: boolean;
  onCancel: () => void;
  onOk: (relabelRerun: RelabelRerun) => void;
}) {
  const [choice, setChoice] = useState<RelabelRerun>('v1');
  useEffect(() => {
    if (visible) setChoice('v1');
  }, [visible]);
  const q = useQuery({
    queryKey: [...qk.adjudicationAll(taskId), 'apply-summary'],
    queryFn: () => unappliedCards(taskId),
    enabled: visible,
    staleTime: 0,
    gcTime: 0,
  });
  const summary = q.data ? applySummary(q.data.map((c) => viewCard(c, local, catalog))) : null;
  const hasRelabels = Boolean(summary && (summary.rerun || summary.humanJudged));
  return (
    <Modal
      title={zh.adjudication.applyTitle}
      visible={visible}
      onCancel={onCancel}
      onOk={() => onOk(summary?.rerun ? choice : 'v1')}
      okButtonProps={{ disabled: !summary }}
      confirmLoading={busy}
      okText={zh.adjudication.applyOk}
      cancelText={zh.common.cancel}
      unmountOnExit
    >
      {q.isLoading ? (
        <Spin tip={zh.adjudication.applyLoading} />
      ) : !summary ? (
        <Alert type="error" content={errorMessage(q.error)} />
      ) : (
        <div data-testid="apply-summary">
          <Typography.Paragraph style={{ marginBottom: 4 }}>{zh.adjudication.applyCount(summary.decisions, summary.episodes)}</Typography.Paragraph>
          {summary.rerun ? <Typography.Paragraph style={{ marginBottom: 4 }}>{zh.adjudication.applyRerun(summary.rerun)}</Typography.Paragraph> : null}
          {hasRelabels ? <Typography.Paragraph style={{ marginBottom: 4 }}>{zh.adjudication.applyHumanJudged(summary.humanJudged)}</Typography.Paragraph> : null}
          <ul data-testid="apply-list" style={{ paddingLeft: 18, maxHeight: 220, overflow: 'auto' }}>
            {summary.views.map((v) => (
              <li key={v.ep}>{describe(v, catalog)}</li>
            ))}
          </ul>
          {summary.rerun ? (
            <div style={{ margin: '8px 0 12px' }} data-testid="relabel-rerun">
              <b>{zh.adjudication.rerunChoice}</b>
              <Radio.Group direction="vertical" value={choice} onChange={(v: RelabelRerun) => setChoice(v)} aria-label={zh.adjudication.rerunChoice} style={{ display: 'block', marginTop: 6 }}>
                <Radio value="v1">
                  <Space direction="vertical" size={0}>
                    <span>{zh.adjudication.rerunV1}</span>
                    <span className="muted" style={{ fontSize: 12 }}>
                      {zh.adjudication.rerunV1Desc}
                    </span>
                  </Space>
                </Radio>
                <Radio value="full">
                  <Space direction="vertical" size={0}>
                    <span>{zh.adjudication.rerunFull}</span>
                    <span className="muted" style={{ fontSize: 12 }}>
                      {zh.adjudication.rerunFullDesc}
                    </span>
                  </Space>
                </Radio>
              </Radio.Group>
            </div>
          ) : null}
          <Typography.Paragraph type="secondary">{zh.adjudication.applyNote}</Typography.Paragraph>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
            {zh.adjudication.applyChecks}
          </Typography.Paragraph>
        </div>
      )}
    </Modal>
  );
}
