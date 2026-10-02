import { Alert, Button, Card, Space, Tag, Tooltip, Typography } from '@arco-design/web-react';
import { IconDown, IconUp } from '@arco-design/web-react/icon';
import { Link } from 'react-router-dom';
import { moduleById, moduleName, useModules } from '../../api/queries';
import type { ReportModuleSection, ReportModuleSectionV2, Subtask } from '../../api/types';
import { FindingsView } from '../../features/findings/FindingsView';
import { LEVEL_TAG, moduleRole, placeLabel } from '../../lib/findings';
import { formatValue, subtaskName } from '../../lib/reportView';
import { zh } from '../../locales/zh';
import { DefaultSectionView, SECTION_VIEWS } from './sectionViews';

export { DefaultSectionView, SECTION_VIEWS } from './sectionViews';
export type { SectionViewProps } from './sectionViews';

// 错误 in red, like the task's state tag (fourth round)
export const SECTION_STATE_COLOR: Record<string, string> = { succeeded: 'green', completed_with_errors: 'red', failed: 'red' };

/**
 * One report section per selected module, in report.json order (07 §5, 06 §6.2): the module's
 * statistics and charts (SECTION_VIEWS), never a list of episodes. The toggle on the right folds
 * the section away (remembered per browser, F6.2). A report of the policy verdicts (C2 2.0, design doc
 * 17 §5.2) puts the generic findings statistics first (FindingsView: 检出项 by level, the readings'
 * distributions, what could not be assessed); a module's own view follows when it has one.
 */
export function ModuleSection({
  index,
  taskId,
  rev,
  section,
  v2 = null,
  preset,
  subtasks,
  readOnly,
  retryBlocked,
  collapsed,
  onToggle,
  onRetry,
}: {
  index: number;
  taskId: string;
  rev: number;
  section: ReportModuleSection;
  /** the section as a report 2.0 has it; null for a report of the funnel */
  v2?: ReportModuleSectionV2 | null;
  preset?: string;
  subtasks: readonly Subtask[];
  readOnly: boolean;
  retryBlocked: string | null;
  collapsed: boolean;
  onToggle: () => void;
  onRetry: (moduleId: string, name: string) => void;
}) {
  const reg = useModules();
  const spec = moduleById(reg.data, section.id);
  const name = moduleName(reg.data, section.id);
  const pending = section.adjudication?.pending ?? 0;
  const usesVlm = ((spec?.needs ?? []) as string[]).includes('vlm');
  const fp = (section.fingerprints ?? {}) as Record<string, unknown>;
  const fromSubtask = typeof fp.from_subtask === 'string' ? fp.from_subtask : null;
  const fpRest = Object.entries(fp).filter(([k]) => k !== 'from_subtask');
  const Own = SECTION_VIEWS[section.id];
  const View = Own ?? DefaultSectionView;
  const role = moduleRole(spec, preset);
  const disabledReason = readOnly ? zh.report.historyDisabled : retryBlocked;
  const bodyId = `section-body-${section.id}`;
  const adjudicate = pending ? (
    readOnly ? (
      <Tooltip content={zh.report.historyDisabled}>
        <Button size="small" disabled>
          {zh.report.goAdjudicate(pending)}
        </Button>
      </Tooltip>
    ) : (
      <Link to={`/tasks/${taskId}/adjudication?source=${encodeURIComponent(section.id)}`}>
        <Button size="small" type="primary">
          {zh.report.goAdjudicate(pending)}
        </Button>
      </Link>
    )
  ) : null;
  // D42: rejects attributed to this module that a person may appeal (optional, not pending).
  const appealable = section.adjudication?.appealable ?? 0;
  const appeal = appealable ? (
    readOnly ? (
      <Tooltip content={zh.report.historyDisabled}>
        <Button size="small" disabled>
          {zh.report.goAppeal(appealable)}
        </Button>
      </Tooltip>
    ) : (
      <Link to={`/tasks/${taskId}/adjudication?tab=appeals&source=${encodeURIComponent(section.id)}`}>
        <Button size="small">{zh.report.goAppeal(appealable)}</Button>
      </Link>
    )
  ) : null;
  const toggleLabel = collapsed ? zh.reportPage.expand : zh.reportPage.collapse;
  return (
    <Card
      id={`module-${section.id}`}
      className={`section-anchor section-card${collapsed ? ' collapsed' : ''}`}
      data-testid={`section-${section.id}`}
      title={
        <div className="section-head">
          <span className="section-index">{index}</span>
          <b>{name}</b>
          {v2 ? (
            <>
              <span className="muted" style={{ fontSize: 12 }}>
                {placeLabel(reg.data, spec)}
              </span>
              <Tag size="small" color={LEVEL_TAG[role]}>
                {zh.findings.role[role]}
              </Tag>
            </>
          ) : (
            <Tag size="small">{zh.gate[section.gate] ?? section.gate}</Tag>
          )}
          {usesVlm ? (
            <Tag size="small" color="purple">
              {zh.report.usesVlm}
            </Tag>
          ) : null}
          <Tag size="small" color={SECTION_STATE_COLOR[section.state]}>
            {zh.moduleState[section.state] ?? section.state}
          </Tag>
          {section.episodes_error ? (
            <Typography.Text type="warning" style={{ fontSize: 12 }}>
              {zh.report.episodesError(section.episodes_error)}
            </Typography.Text>
          ) : null}
        </div>
      }
      extra={
        <Space size={8}>
          {adjudicate}
          {appeal}
          <Button type="text" size="small" icon={collapsed ? <IconDown /> : <IconUp />} aria-expanded={!collapsed} aria-controls={bodyId} aria-label={`${toggleLabel}：${name}`} onClick={onToggle}>
            {toggleLabel}
          </Button>
        </Space>
      }
    >
      {collapsed ? null : (
        <div id={bodyId}>
          <Space direction="vertical" style={{ width: '100%' }} size={12}>
            {fromSubtask || fpRest.length ? (
              <Alert
                type="info"
                content={
                  <span>
                    {fromSubtask ? zh.report.fromSubtask(subtaskName(subtasks, fromSubtask)) : null}
                    {fpRest.map(([k, v]) => (
                      <span key={k} className="mono" style={{ marginLeft: 8, fontSize: 12 }}>
                        {zh.report.fingerprint[k] ?? k} {formatValue(v)}
                      </span>
                    ))}
                  </span>
                }
              />
            ) : null}
            {section.state === 'failed' ? (
              <Alert
                type="error"
                title={zh.report.failedTitle}
                content={
                  <Space direction="vertical">
                    <span className="mono" data-testid={`section-error-${section.id}`}>
                      {section.error}
                    </span>
                    {disabledReason ? (
                      <Tooltip content={disabledReason}>
                        <Button size="small" disabled>
                          {zh.report.retryModule}
                        </Button>
                      </Tooltip>
                    ) : (
                      <Button size="small" type="primary" status="danger" onClick={() => onRetry(section.id, name)}>
                        {zh.report.retryModule}
                      </Button>
                    )}
                    {readOnly ? <span className="muted">{zh.report.historyDisabled}</span> : null}
                  </Space>
                }
              />
            ) : v2 ? (
              <>
                <FindingsView moduleId={section.id} section={v2} ownScore={Boolean(Own) && Array.isArray((section.summary as { score_hist?: unknown }).score_hist)} />
                {Own ? <Own taskId={taskId} rev={rev} section={section} /> : null}
              </>
            ) : (
              <View taskId={taskId} rev={rev} section={section} />
            )}
          </Space>
        </div>
      )}
    </Card>
  );
}
