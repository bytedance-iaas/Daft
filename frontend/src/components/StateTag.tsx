import { Tag, Tooltip } from '@arco-design/web-react';
import { IconLoop } from '@arco-design/web-react/icon';
import type { TaskState } from '../api/types';
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
  completed_with_errors: 'orange',
  failed: 'red',
};

/**
 * A task state label (doc 07 §8). A system pause keeps the 「已暂停」 label with an icon whose
 * tooltip says it resumes by itself (review decision 5).
 */
export function StateTag({ state, pauseReason, size = 'default' }: { state: TaskState; pauseReason?: 'user' | 'system' | null; size?: 'small' | 'default' }) {
  const system = (state === 'paused' || state === 'pausing') && pauseReason === 'system';
  const label = zh.state[state];
  return (
    <span className="nowrap" data-testid="state-tag" data-state={state}>
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
