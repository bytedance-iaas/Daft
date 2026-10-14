import { Radio } from '@arco-design/web-react';
import { IconLoading } from '@arco-design/web-react/icon';
import { zh } from '../locales/zh';

/**
 * 「HuggingFace 缓存桶」 in a source picker (2026-10-12): there from the start, grey with a spinner while the
 * bucket's list is read (`undefined`), grey with the reason on hover when it cannot be read (`null`).
 */
export function PublicSourceRadio({ catalog }: { catalog: readonly unknown[] | null | undefined }) {
  if (catalog) return <Radio value="public">{zh.source.public}</Radio>;
  const reading = catalog === undefined;
  return (
    <Radio value="public" disabled data-testid="public-source-off">
      <span title={reading ? zh.source.publicReading : zh.source.publicFailed}>{zh.source.public}</span>
      {reading ? <IconLoading style={{ marginLeft: 6 }} aria-hidden /> : null}
    </Radio>
  );
}
