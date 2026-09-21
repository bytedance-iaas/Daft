import { Tooltip } from '@arco-design/web-react';
import { absoluteTime, relativeTimeText } from '../lib/format';

/** Local time shown as 「3 分钟前」, the absolute time on hover (doc 07 §9). */
export function RelTime({ ms }: { ms: number | null | undefined }) {
  if (!ms) return <span className="muted">—</span>;
  return (
    <Tooltip content={absoluteTime(ms)}>
      <span className="nowrap">{relativeTimeText(ms)}</span>
    </Tooltip>
  );
}
