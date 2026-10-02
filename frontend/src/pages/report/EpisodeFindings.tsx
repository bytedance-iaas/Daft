import { Button, Space, Tag, Tooltip } from '@arco-design/web-react';
import { Link } from 'react-router-dom';
import { moduleName, useModules } from '../../api/queries';
import type { EpisodeFinding, EpisodeView } from '../../api/types';
import { LevelTag } from '../../features/findings/FindingsView';
import { LEVELS, codeName, groupByLevel, intervalOf, itemLabel, levelTitle, scopeText } from '../../lib/findings';
import { zh } from '../../locales/zh';

const F = () => zh.findings;

/** Where a person answers about this episode: the source modules of its open questions, per tab. */
export function questionTargets(review: EpisodeView['review'], askable: (item: { module: string; kind?: string }) => boolean) {
  const out = { review: new Set<string>(), appeals: new Set<string>() };
  for (const r of review ?? []) {
    if (!askable(r)) continue;
    (r.kind === 'reject_appeal' ? out.appeals : out.review).add(r.module);
  }
  return out;
}

/**
 * 全部发现 of one episode (design doc 17 §5.4): every finding of every module, grouped blocking / review /
 * info, each with its module, code, item, sentence, scope and moment (a moment in seconds plays every
 * camera from there); an open question gets 去裁决, an appealable reject 去复议.
 */
export function EpisodeFindings({
  taskId,
  findings,
  targets,
  readOnly,
  onSeek,
}: {
  taskId: string;
  findings: readonly EpisodeFinding[];
  targets: { review: Set<string>; appeals: Set<string> };
  readOnly: boolean;
  onSeek: (at: number) => void;
}) {
  const reg = useModules();
  const groups = groupByLevel(findings);
  const go = (to: string, label: string) =>
    readOnly ? (
      <Tooltip content={zh.report.historyDisabled}>
        <Button size="mini" disabled>
          {label}
        </Button>
      </Tooltip>
    ) : (
      <Link to={to}>
        <Button size="mini" type="primary">
          {label}
        </Button>
      </Link>
    );
  const row = (f: EpisodeFinding, i: number) => {
    const where = intervalOf(f.finding);
    const scope = scopeText(f.finding.scope);
    const ask = f.level === 'review' && targets.review.has(f.module);
    const appeal = f.level === 'blocking' && f.appealable && targets.appeals.has(f.module);
    return (
      <li key={i} data-testid="episode-finding">
        <Space size={6} wrap>
          <span>{f.finding.message_zh}</span>
          {f.human ? (
            <Tag size="small" color="purple">
              {F().human}
            </Tag>
          ) : null}
          {f.appealable && f.level === 'blocking' ? (
            <Tag size="small" color="arcoblue">
              {F().appealable}
            </Tag>
          ) : null}
          {where ? (
            where.seekS !== null ? (
              <Tooltip content={F().seek}>
                <Button size="mini" type="text" onClick={() => onSeek(where.seekS!)} data-testid="finding-seek">
                  {where.text}
                </Button>
              </Tooltip>
            ) : (
              <span className="muted">{where.text}</span>
            )
          ) : null}
          {ask ? go(`/tasks/${taskId}/adjudication?source=${encodeURIComponent(f.module)}`, zh.episodeTab.goAdjudicate) : null}
          {appeal ? go(`/tasks/${taskId}/adjudication?tab=appeals&source=${encodeURIComponent(f.module)}`, zh.episodeTab.goAppeal) : null}
        </Space>
        <div className="finding-meta">
          {[moduleName(reg.data, f.module), codeName(reg.data, f.module, f.finding.code), itemLabel(reg.data, f.finding.item), scope].filter(Boolean).join(' · ')}
        </div>
      </li>
    );
  };
  return (
    <div data-testid="episode-findings">
      <Space size={8} align="baseline">
        <b>{F().title}</b>
        <span className="muted" style={{ fontSize: 12 }}>
          {F().levelNote}
        </span>
      </Space>
      {!findings.length ? <div className="muted">{F().none}</div> : null}
      {LEVELS.map((lv) =>
        groups[lv].length ? (
          <div key={lv} className="finding-group" data-testid={`findings-${lv}`}>
            <Space size={6}>
              <LevelTag level={lv} />
              <span className="muted" style={{ fontSize: 12 }} aria-label={`${levelTitle(reg.data, lv)} ${F().count(groups[lv].length)}`}>
                {F().count(groups[lv].length)}
              </span>
            </Space>
            <ul className="finding-list">{groups[lv].map(row)}</ul>
          </div>
        ) : null,
      )}
    </div>
  );
}
