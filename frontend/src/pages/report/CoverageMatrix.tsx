import { Space, Tag, Tooltip, Typography } from '@arco-design/web-react';
import { moduleName, useModules } from '../../api/queries';
import type { ReportV2 } from '../../api/types';
import { coverageMatrix, reportersOf, type CoverageItem } from '../../lib/findings';
import { zh } from '../../locales/zh';

const F = () => zh.findings;
const STATUS_COLOR: Record<string, string> = { covered: 'arcoblue', unassessable: 'orange', not_covered: 'gray' };

/**
 * 本次质检范围's coverage matrix (design doc 17 §5.2): the taxonomy's check items by dimension - covered by
 * this task's modules, not covered, or covered but not assessable on some episodes (with the reasons).
 * A covered item jumps to the first module section that reports it.
 */
export function CoverageMatrix({ report, onJump }: { report: ReportV2; onJump: (moduleId: string) => void }) {
  const reg = useModules();
  const m = coverageMatrix(reg.data, report.overview);
  const ids = report.modules.map((s) => s.id);
  const chip = (it: CoverageItem) => {
    const to = it.status === 'not_covered' ? [] : reportersOf(reg.data, ids, it.id);
    // the colour and the legend above say covered / not / partly (requester 2026-10-05): the tip adds only what they cannot
    const lines = [
      `${it.id} ${it.name}`,
      ...(it.episodes ? [F().coverageFound(it.episodes)] : []),
      ...it.unassessable.map((u) => F().coverageWhy(u.title, u.count)),
      ...(to.length ? [F().coverageJump(to.map((id) => moduleName(reg.data, id)).join('、'))] : []),
    ];
    const tag = (
      <Tag
        size="small"
        color={STATUS_COLOR[it.status]}
        className={to.length ? 'coverage-chip clickable' : 'coverage-chip'}
        data-testid={`coverage-${it.id}`}
        data-status={it.status}
        onClick={to.length ? () => onJump(to[0]) : undefined}
      >
        {it.id}
        {it.episodes ? <span className="coverage-count">{it.episodes}</span> : null}
      </Tag>
    );
    return (
      <Tooltip key={it.id} content={<div style={{ whiteSpace: 'pre-line' }}>{lines.join('\n')}</div>}>
        {tag}
      </Tooltip>
    );
  };
  return (
    <div className="coverage" data-testid="coverage-matrix">
      <Space size={12} wrap align="baseline">
        <Typography.Title heading={6} style={{ margin: 0 }} data-testid="coverage-head">
          {F().coverageHead(m.covered, m.total)}
        </Typography.Title>
        <span className="muted" style={{ fontSize: 12 }}>
          {F().coverageVersion(m.taxonomyVersion)}
        </span>
        <span className="muted" style={{ fontSize: 12 }}>
          {(['covered', 'unassessable', 'not_covered'] as const).map((s) => (
            <span key={s} style={{ marginRight: 12 }}>
              <Tag size="small" color={STATUS_COLOR[s]} style={{ marginRight: 4 }}>
                {' '}
              </Tag>
              {F().coverageStatus[s]}
            </span>
          ))}
        </span>
      </Space>
      <dl className="coverage-grid">
        {m.dimensions.map((d) => (
          <div key={d.id} className="coverage-row" data-testid={`coverage-dim-${d.id}`}>
            <dt>
              {d.name}
              <span className="muted">
                {' '}
                {d.items.filter((i) => i.status !== 'not_covered').length} / {d.items.length}
              </span>
            </dt>
            <dd>{d.items.map(chip)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
