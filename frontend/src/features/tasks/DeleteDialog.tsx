import { Checkbox, Message, Modal, Spin, Typography } from '@arco-design/web-react';
import { useEffect, useState } from 'react';
import { api, idempotencyKey, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { useTask } from '../../api/queries';
import type { PurgeResult } from '../../api/types';
import { bytes } from '../../lib/format';
import { zh } from '../../locales/zh';
import { runDirectory } from './PurgeDialog';

/**
 * 删除任务 (07 §4.1, requester item 19): deletes the platform record (restorable for 30 days) and,
 * when 「同时清理交付产物」 is ticked — it starts unticked and only shows for a task that wrote a run
 * directory — first purges that directory, whose exact path is shown. The record is deleted only
 * if the purge was accepted; a failed purge deletes nothing. The list row has no run_id, so the
 * task itself is fetched (the detail page has it cached).
 */
export function DeleteDialog({ target, onClose, onDone }: { target: { id: string; name: string } | null; onClose: () => void; onDone: (id: string) => void }) {
  const task = useTask(target?.id);
  const [purge, setPurge] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (target) setPurge(false);
  }, [target]);
  const t = task.data;
  const path = t ? runDirectory(t.output.uri, t.run_id) : null;
  const run = async () => {
    if (!target) return;
    const id = target.id;
    setBusy(true);
    let purged: PurgeResult | null = null;
    try {
      if (purge && path) {
        try {
          purged = await unwrap(api().POST('/tasks/{id}/purge-artifacts', { params: { path: { id }, header: { 'Idempotency-Key': idempotencyKey() } }, body: { confirm_path: path } }));
        } catch (e) {
          // Nothing was deleted: the dialog stays, so the record alone can still be deleted.
          Message.error(zh.actions.deleteDialog.purgeFailed(errorMessage(e)));
          return;
        }
      }
      try {
        await unwrap(api().DELETE('/tasks/{id}', { params: { path: { id } } }));
        Message.success(purged ? zh.actions.done.deletePurged(purged.path, bytes(purged.bytes)) : zh.actions.done.delete);
      } catch (e) {
        Message.error(purged ? zh.actions.deleteDialog.deleteFailedAfterPurge(purged.path, errorMessage(e)) : errorMessage(e));
      }
      onDone(id);
      onClose();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={zh.actions.confirmDelete.title}
      visible={Boolean(target)}
      onCancel={onClose}
      okText={zh.actions.delete}
      cancelText={zh.common.cancel}
      okButtonProps={{ status: 'danger', disabled: task.isLoading }}
      confirmLoading={busy}
      onOk={() => void run()}
      unmountOnExit
    >
      {target ? (
        <div data-testid="delete-dialog">
          <Typography.Paragraph>{zh.actions.confirmDelete.content(target.name)}</Typography.Paragraph>
          {task.isLoading ? (
            <Spin size={16} />
          ) : path ? (
            <>
              <Checkbox checked={purge} onChange={setPurge} data-testid="delete-purge">
                {zh.actions.deleteDialog.purge}
              </Checkbox>
              {purge ? (
                <div style={{ marginTop: 8 }}>
                  <Typography.Paragraph type="warning" style={{ marginBottom: 4 }}>
                    {zh.actions.purgeDialog.intro}
                  </Typography.Paragraph>
                  <Typography.Paragraph className="mono" copyable data-testid="delete-purge-path" style={{ marginBottom: 0 }}>
                    {path}
                  </Typography.Paragraph>
                </div>
              ) : (
                <div className="muted" style={{ marginTop: 4, fontSize: 12 }}>
                  {zh.actions.deleteDialog.keepArtifacts}
                </div>
              )}
            </>
          ) : (
            <Typography.Text type="secondary">{zh.actions.deleteDialog.noArtifacts}</Typography.Text>
          )}
        </div>
      ) : null}
    </Modal>
  );
}
