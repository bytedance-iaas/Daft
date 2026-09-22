import { Popover, Spin } from '@arco-design/web-react';
import { useState } from 'react';
import { useModules, useTask } from '../../api/queries';
import type { ModuleRegistry, ModuleState, TaskListItem } from '../../api/types';
import { moduleProblems, presetLabel, presetOf, stageLabel } from '../../lib/taskView';
import { zh } from '../../locales/zh';

export const MODULE_STATE_COLOR: Record<string, string> = {
  pending: 'var(--c-text-4)',
  running: 'var(--c-primary)',
  succeeded: 'var(--c-success)',
  completed_with_errors: 'var(--c-warning)',
  failed: 'var(--c-danger)',
  skipped: 'var(--c-text-3)',
  stale: '#722ed1',
};

const PROBLEM_RANK: Record<string, number> = { failed: 0, completed_with_errors: 1, skipped: 2, stale: 3, running: 4, pending: 5, succeeded: 6 };

interface Row {
  id: string;
  name: string;
  stage: string;
  state?: string;
}

/** Modules grouped by stage (registry order), problems first inside each group. */
export function groupModules(reg: ModuleRegistry, selected: readonly string[], states?: readonly ModuleState[]): { stage: string; rows: Row[] }[] {
  const rows: Row[] = selected
    .map((id) => reg.modules.find((m) => m.id === id))
    .filter((m): m is NonNullable<typeof m> => Boolean(m))
    .map((m) => ({ id: m.id, name: m.name_zh, stage: m.stage, state: states?.find((s) => s.id === m.id)?.state }));
  return reg.stages
    .map((stage) => ({
      stage,
      rows: rows.filter((r) => r.stage === stage).sort((a, b) => (PROBLEM_RANK[a.state ?? 'pending'] ?? 9) - (PROBLEM_RANK[b.state ?? 'pending'] ?? 9)),
    }))
    .filter((g) => g.rows.length);
}

function ModuleList({ item, reg, hovered }: { item: TaskListItem; reg: ModuleRegistry; hovered: boolean }) {
  const detail = useTask(hovered ? item.id : undefined);
  const groups = groupModules(reg, item.modules, detail.data?.modules);
  return (
    <div style={{ maxHeight: 320, overflow: 'auto', minWidth: 220 }} data-testid="module-popover">
      <div style={{ fontWeight: 500, marginBottom: 6 }}>{zh.taskList.modulePopoverTitle}</div>
      {detail.isLoading ? <Spin size={14} tip={zh.taskList.modulePopoverLoading} /> : null}
      {groups.map((g) => (
        <div key={g.stage} style={{ marginBottom: 6 }}>
          <div className="muted" style={{ fontSize: 12 }}>
            {stageLabel(g.stage)}
          </div>
          {g.rows.map((r) => (
            <div key={r.id} style={{ lineHeight: '22px' }}>
              <span className="dot" style={{ background: MODULE_STATE_COLOR[r.state ?? 'pending'] }} />
              {r.name}
              {r.state ? <span className="muted"> · {zh.moduleState[r.state] ?? r.state}</span> : null}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

/**
 * The 质检模块 column (07 §4.1): only a summary — 「N 项」 plus red 「N 项错误」 or gray
 * 「N 项未运行」, the preset on a second line, the full list on hover (fetched lazily).
 */
export function ModuleSummaryCell({ item }: { item: TaskListItem }) {
  const reg = useModules();
  const [hovered, setHovered] = useState(false);
  const { errors, notRun } = moduleProblems(item.module_counts);
  return (
    <Popover
      trigger="hover"
      position="bottom"
      onVisibleChange={(v) => v && setHovered(true)}
      content={reg.data ? <ModuleList item={item} reg={reg.data} hovered={hovered} /> : <Spin size={14} />}
    >
      <div data-testid="module-summary" style={{ cursor: 'default' }}>
        <span>{zh.taskList.moduleCount(item.modules.length)}</span>
        {errors ? <span style={{ color: 'var(--c-danger)' }}> · {zh.taskList.moduleErrors(errors)}</span> : null}
        {notRun ? <span className="muted"> · {zh.taskList.moduleNotRun(notRun)}</span> : null}
        <div className="muted" style={{ fontSize: 12 }}>
          {presetLabel(presetOf(item.modules, reg.data))}
        </div>
      </div>
    </Popover>
  );
}
