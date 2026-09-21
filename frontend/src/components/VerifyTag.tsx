import { Tag, Tooltip } from '@arco-design/web-react';
import type { VerifyState } from '../api/types';
import { zh } from '../locales/zh';

const COLOR: Record<VerifyState, string> = { ok: 'green', unverified: 'gray', failed: 'red' };

/** 已验证 / 未验证 / 验证失败, with the reason (07 §7). */
export function VerifyTag({ state, error }: { state: VerifyState; error?: string | null }) {
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
