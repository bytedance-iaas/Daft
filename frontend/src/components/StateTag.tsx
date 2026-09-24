import { Tag, Tooltip } from '@arco-design/web-react';
import { IconLoop } from '@arco-design/web-react/icon';
import type { Subtask, TaskState } from '../api/types';
import { displayState } from '../lib/taskView';
import { zh } from '../locales/zh';

const COLORS: Record<TaskState, string> = {
  created: 'arcoblue',
  queued: 'gray',
  running: 'arcoblue',
  pausing: 'gray',
  paused: 'gray',
  stopping: 'gray',
  stopped: 'gray',
  succeeded: 'green',
  completed_with_errors: 'red', // 错误 in red, like 失败 (fourth round)
  failed: 'red',
};

/**
 * A task state label (doc 07 §8). A system pause keeps the 「已暂停」 label with an icon whose
 * tooltip says it resumes by itself (review decision 5). `subtask` names the subtask the state
 * belongs to: 「运行中 · 重试 #1」 (D46).
 */
export function StateTag({
  state,
  pauseReason,
  subtask,
  size = 'default',
}: {
  state: TaskState;
  pauseReason?: 'user' | 'system' | null;
  subtask?: string | null;
  size?: 'small' | 'default';
}) {
  const system = (state === 'paused' || state === 'pausing') && pauseReason === 'system';
  const label = subtask ? zh.taskList.stateWithSubtask(zh.state[state], subtask) : zh.state[state];
  return (
    <span className="nowrap" data-testid="state-tag" data-state={state} data-subtask={subtask ?? undefined}>
      <Tag color={COLORS[state]} size={size} bordered={state === 'created'}>
        {state === 'running' ? <span className="dot" style={{ background: 'var(--c-primary)' }} /> : null}
        {label}
      </Tag>
      {system ? (
        <Tooltip content={zh.state.systemPauseTip}>
          <IconLoop aria-label={zh.state.systemPauseTip} style={{ marginLeft: 4, color: 'var(--c-text-3)' }} />
        </Tooltip>
      ) : null}
    </span>
  );
}

/**
 * A task's state as the pages show it (D46): 「运行中」 while a subtask is under way, naming the
 * subtask where the page knows it (the detail page does, a list row only has its id).
 */
export function TaskStateTag({
  task,
  subtasks,
  size,
}: {
  task: { state: TaskState; pause_reason?: 'user' | 'system' | null; active_subtask?: Subtask | string | null };
  subtasks?: readonly Subtask[];
  size?: 'small' | 'default';
}) {
  const d = displayState(task, subtasks);
  return <StateTag state={d.state} pauseReason={d.pauseReason} subtask={d.subtask} size={size} />;
}
