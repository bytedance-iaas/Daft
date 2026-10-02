import { Space, Tag, Typography } from '@arco-design/web-react';
import type { ReactNode } from 'react';
import { useModules } from '../../api/queries';
import type { ReportModuleSectionV2 } from '../../api/types';
import { StatCell } from '../../components/StatCell';
import { percent } from '../../lib/format';
import { LEVELS, LEVEL_COLOR, LEVEL_TAG, itemLabel, levelTitle, moduleItems, moduleLevels, readingName, scoreHists, unassessableTitle, type Level } from '../../lib/findings';
import { zh } from '../../locales/zh';
import { ChartBlock, type ChartSpec } from '../../pages/report/sectionViews';

const F = () => zh.findings;

/** A finding's level as a tag (the registry's title). */
export function LevelTag({ level }: { level: string }) {
  const reg = useModules();
  return (
    <Tag size="small" color={LEVEL_TAG[level as Level] ?? 'gray'} data-testid={`level-${level}`}>
      {levelTitle(reg.data, level)}
    </Tag>
  );
}

/** The bars of a section's 检出项 (summary.items): one per finding code, by level, with its share of the assessed. */
export function itemsChart(moduleId: string, summary: ReportModuleSectionV2['summary'], reg: Parameters<typeof moduleItems>[0]): ChartSpec | null {
  const rows = moduleItems(reg, moduleId, summary);
  if (!rows.length) return null;
  const cams = rows.filter((r) => r.byCamera.length);
  return {
    key: `items-${moduleId}`,
    title: F().itemsTitle,
    horizontal: true,
    items: rows.map((r) => ({ name: `${r.name}（${r.item ?? F().platformItem}）`, value: r.episodes })),
    colors: rows.map((r) => LEVEL_COLOR[r.level]),
    summary: rows.map((r) => `${r.name} ${levelTitle(reg, r.level)} ${F().episodesShare(r.episodes, r.share === null ? null : percent(r.share, 1))}`).join('，'),
    foot: (
      <Space direction="vertical" size={2}>
        <span>
          {LEVELS.filter((lv) => rows.some((r) => r.level === lv)).map((lv) => (
            <span key={lv} className="nowrap" style={{ marginRight: 12 }}>
              <span className="dot" style={{ background: LEVEL_COLOR[lv] }} />
              {levelTitle(reg, lv)}
            </span>
          ))}
        </span>
        {rows.map((r) => (
          <span key={r.code}>
            {r.name}：{F().episodesShare(r.episodes, r.share === null ? null : percent(r.share, 1))}
            {r.byCamera.length && cams.length ? `；${F().byCamera} ${r.byCamera.map((c) => `${c.camera} ${c.episodes}`).join('、')}` : ''}
          </span>
        ))}
      </Space>
    ),
  };
}

/**
 * What every module section of a report 2.0 has (design doc 17 §5.2): the episodes it assessed, those it
 * found something in, per level; its 检出项 (codes by level, with their share and cameras); the
 * distributions of its 0-1 readings; why it could not assess some episodes; its dataset-level findings.
 * Drawn from the section's summary alone, so a new module needs no frontend change.
 */
export function FindingsView({
  moduleId,
  section,
  extra,
  ownScore = false,
}: {
  moduleId: string;
  section: ReportModuleSectionV2;
  extra?: ReactNode;
  /** the module's own view draws the composite score's distribution already (06 §6.2's score_hist) */
  ownScore?: boolean;
}) {
  const reg = useModules();
  const s = section.summary;
  const levels = moduleLevels(s);
  const flagged = s.flagged_episodes;
  const unassessed = s.unassessable.reduce((a, u) => a + u.count, 0);
  const charts: ChartSpec[] = [];
  const items = itemsChart(moduleId, s, reg.data);
  if (items) charts.push(items);
  for (const h of scoreHists(s).filter((x) => !(ownScore && x.reading === 'score'))) {
    charts.push({ key: `score-${moduleId}-${h.reading}`, title: F().scoreTitle(readingName(h.reading)), items: h.bins });
  }
  if (s.unassessable.length) {
    charts.push({
      key: `unassessable-${moduleId}`,
      title: F().unassessableTitle,
      horizontal: true,
      items: s.unassessable.map((u) => ({ name: `${unassessableTitle(reg.data, u.reason)}${u.item ? `（${u.item}）` : ''}`, value: u.count })),
    });
  }
  const dataset = s.dataset_findings ?? [];
  return (
    <div className="section-view" data-testid={`findings-${moduleId}`}>
      <div className="stat-grid">
        <StatCell label={F().assessed} value={s.assessed_episodes} />
        {flagged !== undefined ? (
          <StatCell label={F().flagged} value={flagged} foot={s.assessed_episodes ? percent(flagged / s.assessed_episodes, 1) : undefined} testId={`flagged-${moduleId}`} />
        ) : null}
        {LEVELS.filter((lv) => levels[lv] > 0).map((lv) => (
          <StatCell key={lv} label={levelTitle(reg.data, lv)} value={levels[lv]} tone={lv === 'blocking' ? 'bad' : lv === 'review' ? 'warn' : undefined} testId={`level-count-${moduleId}-${lv}`} />
        ))}
        {unassessed ? <StatCell label={F().unassessableTitle} value={unassessed} /> : null}
      </div>
      {!items ? <Typography.Text type="secondary">{F().itemsNone}</Typography.Text> : null}
      {charts.length ? (
        <div className="section-charts">
          {charts.map((c) => (
            <ChartBlock key={c.key} spec={c} />
          ))}
        </div>
      ) : null}
      {dataset.length ? (
        <div className="section-block" data-testid={`dataset-findings-${moduleId}`}>
          <div className="section-sub">{F().datasetFindings}</div>
          <ul className="episode-items">
            {dataset.map((f, i) => (
              <li key={i}>
                <Space size={6} wrap>
                  <span>{f.message_zh}</span>
                  <span className="muted">{itemLabel(reg.data, f.item)}</span>
                </Space>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {extra}
    </div>
  );
}
