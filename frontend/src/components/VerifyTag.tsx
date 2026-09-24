import { Tag, Tooltip } from '@arco-design/web-react';
import type { VerifyState } from '../api/types';
import { zh } from '../locales/zh';
import { OneLine } from './OneLine';

const COLOR: Record<VerifyState, string> = { ok: 'green', unverified: 'gray', failed: 'red' };

/**
 * 已验证 / 未验证 / 验证失败, with the reason (07 §7). `inline` keeps a table row on one line:
 * the reason follows the tag, cut with an ellipsis, whole in a tooltip (fourth round).
 */
export function VerifyTag({ state, error, inline }: { state: VerifyState; error?: string | null; inline?: boolean }) {
  if (inline) {
    return (
      <span className="one-line-with-tag verify-inline">
        <Tag color={COLOR[state]} size="small">
          {zh.verify[state]}
        </Tag>
        {error && state !== 'ok' ? (
          <span className="muted verify-reason">
            <OneLine text={error} />
          </span>
        ) : null}
      </span>
    );
  }
  return (
    <span>
      <Tooltip content={error} disabled={!error}>
        <Tag color={COLOR[state]} size="small">
          {zh.verify[state]}
        </Tag>
      </Tooltip>
      {error && state !== 'ok' ? (
        <div className="muted" style={{ fontSize: 12, maxWidth: 260 }}>
          {error}
        </div>
      ) : null}
    </span>
  );
}
