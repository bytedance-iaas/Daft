import { Button, Menu, Tooltip } from '@arco-design/web-react';
import type { DatasetItem } from '../../api/types';
import { getBase } from '../../base';
import { rerunViewerUrl } from '../../lib/rerun';
import { zh } from '../../locales/zh';

type Size = 'small' | 'default';
type Kind = 'text' | 'secondary';

/**
 * 「可视化」 (design doc 18 §5.0, D63): the visualize page with this dataset, in a new window. A
 * dataset no reader serves is disabled; an mcap one without a confirmed mapping is too, saying why.
 */
export function VisualizeButton({ d, type = 'text', size = 'small' }: { d: Pick<DatasetItem, 'id' | 'viz'>; type?: Kind; size?: Size }) {
  const state = d.viz?.state ?? 'unsupported';
  if (state !== 'ready') {
    return (
      <Tooltip content={d.viz?.reason ?? ''}>
        <Button type={type} size={size} disabled>
          {zh.datasets.visualize}
        </Button>
      </Tooltip>
    );
  }
  return (
    <Button
      type={type}
      size={size}
      title={zh.datasets.visualizeTitle}
      href={`${getBase()}/visualize?dataset=${encodeURIComponent(d.id)}`}
      anchorProps={{ target: '_blank', rel: 'noopener noreferrer' }}
    >
      {zh.datasets.visualize}
    </Button>
  );
}

/**
 * 「ReRun」 in a 「更多」 menu (requester item 22, D63; named so since 2026-10-11, 「可视化（旧）」 before): the dataset in
 * the ReRun web viewer of the same deployment, in a new tab. A locally mounted dataset cannot be opened there: the item
 * is disabled and says why. A private TOS dataset's link names the registration, so the viewer reads it with
 * Daemon-signed URLs (design doc 15). Called as a function: Arco's menu wants its items as direct children.
 */
export function rerunMenuItem(d: Pick<DatasetItem, 'id' | 'source' | 'uri' | 'region'>, key = 'rerun') {
  const url = rerunViewerUrl(d);
  return (
    <Menu.Item key={key} disabled={!url}>
      {url ? (
        <a href={url} target="_blank" rel="noopener noreferrer" title={zh.datasets.visualizeLegacyTitle}>
          {zh.datasets.visualizeLegacy}
        </a>
      ) : (
        <span title={zh.datasets.visualizeLocal}>{zh.datasets.visualizeLegacy}</span>
      )}
    </Menu.Item>
  );
}
