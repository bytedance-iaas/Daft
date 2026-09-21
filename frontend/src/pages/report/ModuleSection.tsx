import { Alert, Button, Card, Descriptions, Space, Spin, Tag, Tooltip, Typography } from '@arco-design/web-react';
import type { ComponentType, ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { moduleById, moduleName, useModules } from '../../api/queries';
import type { ReportModuleSection, Subtask } from '../../api/types';
import { Chart, barOption, chartSummary } from '../../components/Chart';
import { LazyVisible } from '../../components/LazyVisible';
import { fieldLabel, formatValue, subtaskName } from '../../lib/reportView';
import { formatScalar, splitSummary } from '../../lib/summary';
import { zh } from '../../locales/zh';
import { RowsTable, useTablePage } from './DetailTables';

export const SECTION_STATE_COLOR: Record<string, string> = { succeeded: 'green', completed_with_errors: 'orange', failed: 'red' };

/** Tables this small are shown inside their section; bigger ones go to 明细表 at the bottom. */
export const INLINE_TABLE_ROWS = 10;

export interface SectionViewProps {
  taskId: string;
  rev: number;
  section: ReportModuleSection;
  /** Opens a table in 明细表 at the bottom of the page. */
  onTable: (table: string) => void;
}

/**
 * Section renderers by module id (06 §6.2). A module without an entry gets the default view —
 * summary scalars, {name, count} series as bar charts, small tables inline — so adding a module
 * needs no frontend change. The v1 special views (stall timeline, sync curves, verdict cards,
 * two-level skill table) need data shapes C2/C4 do not define yet (see README «契约缺口»).
 */
export const SECTION_VIEWS: Record<string, ComponentType<SectionViewProps>> = {};

function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="stat-cell">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
}

function InlineTable({ taskId, rev, table, sort }: { taskId: string; rev: number; table: string; sort: string }) {
  const q = useTablePage(taskId, rev, table, sort, 'asc', null);
  if (q.isLoading) return <Spin size={16} />;
  if (!q.data) return null;
  return <RowsTable columns={q.data.columns} items={q.data.items as Record<string, unknown>[]} testId={`inline-table-${table}`} />;
}

export function DefaultSectionView({ taskId, rev, section, onTable }: SectionViewProps) {
  const reg = useModules();
  const { scalars, series, other } = splitSummary(section.summary);
  const tables = section.tables.map((t) => ({ ...t, spec: reg.data?.modules.flatMap((m) => m.tables).find((x) => x.id === t.id) }));
  return (
    <>
      {scalars.length ? (
        <div className="stat-grid" data-testid={`summary-${section.id}`}>
          {scalars.map((s) => (
            <Stat key={s.key} label={s.label} value={formatScalar(s.value)} />
          ))}
        </div>
      ) : null}
      {series.map((s) => (
        <div key={s.key} style={{ marginTop: 16 }}>
          <Typography.Title heading={6} style={{ margin: '0 0 4px' }}>
            {s.label}
          </Typography.Title>
          <LazyVisible>
            <Chart option={barOption(s.items, { horizontal: s.items.length > 6 })} summary={`${s.label}：${chartSummary(s.items)}`} height={Math.max(160, Math.min(320, s.items.length * 32))} />
          </LazyVisible>
        </div>
      ))}
      {other.length ? (
        <Descriptions
          style={{ marginTop: 16 }}
          column={1}
          title={<span style={{ fontSize: 14 }}>{zh.report.other}</span>}
          data={other.map(([k, v]) => ({ label: fieldLabel(k), value: <span className="mono">{formatValue(v)}</span> }))}
        />
      ) : null}
      {tables.map((t) =>
        t.rows > 0 && t.rows <= INLINE_TABLE_ROWS ? (
          <div key={t.id} style={{ marginTop: 16 }}>
            <Typography.Title heading={6} style={{ margin: '0 0 4px' }}>
              {t.spec?.title_zh ?? t.id}
            </Typography.Title>
            <LazyVisible>
              <InlineTable taskId={taskId} rev={rev} table={t.id} sort={t.spec?.default_sort ?? 'episode_index'} />
            </LazyVisible>
          </div>
        ) : (
          <div key={t.id} style={{ marginTop: 12 }}>
            <Button type="text" size="small" onClick={() => onTable(t.id)} style={{ paddingLeft: 0 }}>
              {zh.report.tableLink(t.spec?.title_zh ?? t.id, t.rows)}
            </Button>
          </div>
        ),
      )}
    </>
  );
}

/** One report section per selected module, in report.json order (07 §5, 06 §6.2). */
export function ModuleSection({
  index,
  taskId,
  rev,
  section,
  subtasks,
  readOnly,
  retryBlocked,
  onRetry,
  onTable,
}: {
  index: number;
  taskId: string;
  rev: number;
  section: ReportModuleSection;
  subtasks: readonly Subtask[];
  readOnly: boolean;
  retryBlocked: string | null;
  onRetry: (moduleId: string, name: string) => void;
  onTable: (table: string) => void;
}) {
  const reg = useModules();
  const spec = moduleById(reg.data, section.id);
  const name = moduleName(reg.data, section.id);
  const pending = section.adjudication?.pending ?? 0;
  const usesVlm = ((spec?.needs ?? []) as string[]).includes('vlm');
  const fp = (section.fingerprints ?? {}) as Record<string, unknown>;
  const fromSubtask = typeof fp.from_subtask === 'string' ? fp.from_subtask : null;
  const fpRest = Object.entries(fp).filter(([k]) => k !== 'from_subtask');
  const View = SECTION_VIEWS[section.id];
  const disabledReason = readOnly ? zh.report.historyDisabled : retryBlocked;
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
  return (
    <Card
      id={`module-${section.id}`}
      className="section-anchor"
      data-testid={`section-${section.id}`}
      title={
        <div className="section-head">
          <span className="section-index">{index}</span>
          <b>{name}</b>
          <Tag size="small">{zh.gate[section.gate] ?? section.gate}</Tag>
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
      extra={adjudicate}
    >
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        {fromSubtask || fpRest.length ? (
          <Alert
            type="info"
            content={
              <span>
                {fromSubtask ? zh.report.fromSubtask(subtaskName(subtasks, fromSubtask)) : null}
                {fpRest.map(([k, v]) => (
                  <span key={k} className="mono" style={{ marginLeft: 8, fontSize: 12 }}>
                    {k} {formatValue(v)}
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
        ) : null}
        {View ? <View taskId={taskId} rev={rev} section={section} onTable={onTable} /> : <DefaultSectionView taskId={taskId} rev={rev} section={section} onTable={onTable} />}
      </Space>
    </Card>
  );
}
