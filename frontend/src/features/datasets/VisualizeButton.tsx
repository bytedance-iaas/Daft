import { Button, Tooltip } from '@arco-design/web-react';
import type { DatasetItem } from '../../api/types';
import { rerunViewerUrl } from '../../lib/rerun';
import { zh } from '../../locales/zh';

/**
 * 「可视化」 (requester item 22): opens the dataset in the ReRun web viewer of the same deployment,
 * in a new tab. A locally mounted dataset cannot be opened there: the button is disabled and says why.
 */
export function VisualizeButton({ d, type = 'text', size = 'small' }: { d: Pick<DatasetItem, 'uri' | 'region'>; type?: 'text' | 'secondary'; size?: 'small' | 'default' }) {
  const url = rerunViewerUrl(d);
  if (!url) {
    return (
      <Tooltip content={zh.datasets.visualizeLocal}>
        <Button type={type} size={size} disabled>
          {zh.datasets.visualize}
        </Button>
      </Tooltip>
    );
  }
  return (
    <Button type={type} size={size} href={url} anchorProps={{ target: '_blank', rel: 'noopener noreferrer' }}>
      {zh.datasets.visualize}
    </Button>
  );
}
