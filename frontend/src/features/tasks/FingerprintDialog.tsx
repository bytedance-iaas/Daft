import { Alert, Collapse, Modal, Space, Typography } from '@arco-design/web-react';
import type { SourceChange } from '../../api/types';
import { RelTime } from '../../components/RelTime';
import { zh } from '../../locales/zh';

/** Parses the `details` of a 409 source_changed into a SourceChange (D37, 03 §12). */
export function asSourceChange(details: Record<string, unknown> | undefined): SourceChange | null {
  if (!details || typeof details !== 'object') return null;
  const n = (k: string) => (typeof details[k] === 'number' ? (details[k] as number) : 0);
  return {
    meta_changed: Boolean(details.meta_changed),
    added: n('added'),
    removed: n('removed'),
    modified: n('modified'),
    sample_keys: Array.isArray(details.sample_keys) ? details.sample_keys.map(String) : [],
    preflighted_at: n('preflighted_at'),
  };
}

/**
 * The start-time fingerprint dialog (07 §3, D37): says what changed since the dataset was added
 * (or last preflighted); 「取消」 or 「重新预检」.
 */
export function FingerprintDialog({
  change,
  visible,
  loading,
  onCancel,
  onConfirm,
  context = 'task',
}: {
  context?: 'task' | 'dataset';
  change: SourceChange | null;
  visible: boolean;
  loading?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <Modal
      title={zh.fingerprint.title}
      visible={visible}
      onCancel={onCancel}
      onOk={onConfirm}
      okText={loading ? zh.fingerprint.running : zh.fingerprint.confirm}
      cancelText={zh.common.cancel}
      confirmLoading={loading}
      unmountOnExit
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Typography.Text>{context === 'dataset' ? zh.fingerprint.introDataset : zh.fingerprint.intro}</Typography.Text>
        {change ? (
          <Alert
            type="warning"
            content={
              <div data-testid="source-change">
                <div>{zh.fingerprint.meta(change.meta_changed)}</div>
                <div>{zh.fingerprint.files(change.added, change.removed, change.modified)}</div>
                <div>
                  {zh.fingerprint.lastPreflight}：<RelTime ms={change.preflighted_at} />
                </div>
              </div>
            }
          />
        ) : null}
        {change && change.sample_keys.length ? (
          <Collapse bordered={false} defaultActiveKey={['keys']}>
            <Collapse.Item header={zh.fingerprint.samples} name="keys">
              <ul className="mono" style={{ margin: 0, paddingLeft: 18 }}>
                {change.sample_keys.map((k) => (
                  <li key={k}>{k}</li>
                ))}
              </ul>
            </Collapse.Item>
          </Collapse>
        ) : null}
      </Space>
    </Modal>
  );
}
