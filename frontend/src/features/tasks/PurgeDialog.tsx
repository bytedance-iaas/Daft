import { Alert, Form, Input, Message, Modal, Spin, Typography } from '@arco-design/web-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { api, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { qk, useTask } from '../../api/queries';
import { bytes } from '../../lib/format';
import { zh } from '../../locales/zh';

/** The run directory purge-artifacts deletes: <delivery directory>/<run_id>/ (03 §8). */
export function runDirectory(outputUri: string, runId: string | null | undefined): string | null {
  if (!runId) return null;
  return `${outputUri.replace(/\/+$/, '')}/${runId}/`;
}

/**
 * 「清理交付产物」 (D28): shows the exact path, requires typing the task name, then echoes the
 * path back as confirm_path. C4 has no dry run, so the size is reported after the call.
 */
export function PurgeDialog({ taskId, onClose }: { taskId: string | null; onClose: () => void }) {
  const qc = useQueryClient();
  const task = useTask(taskId ?? undefined);
  const [form] = Form.useForm<{ name: string }>();
  const t = task.data;
  const path = t ? runDirectory(t.output.uri, t.run_id) : null;
  useEffect(() => {
    if (taskId) form.resetFields();
  }, [taskId, form]);
  const purge = useMutation({
    mutationFn: () => unwrap(api().POST('/tasks/{id}/purge-artifacts', { params: { path: { id: taskId! } }, body: { confirm_path: path! } })),
    onSuccess: (res) => {
      Message.success(zh.actions.done.purge(res.path, bytes(res.bytes)));
      void qc.invalidateQueries({ queryKey: qk.task(taskId!) });
      onClose();
    },
    onError: (e) => Message.error(errorMessage(e)),
  });
  return (
    <Modal
      title={zh.actions.purgeDialog.title}
      visible={Boolean(taskId)}
      onCancel={onClose}
      okText={zh.actions.purgeDialog.ok}
      okButtonProps={{ status: 'danger', disabled: !path }}
      confirmLoading={purge.isPending}
      onOk={async () => {
        await form.validate();
        purge.mutate();
      }}
      unmountOnExit
    >
      {task.isLoading ? (
        <Spin />
      ) : !t ? null : !path ? (
        <Alert type="info" content={zh.actions.purgeDialog.noRun} />
      ) : (
        <>
          <Typography.Paragraph>{zh.actions.purgeDialog.intro}</Typography.Paragraph>
          <Typography.Paragraph className="mono" copyable data-testid="purge-path">
            {path}
          </Typography.Paragraph>
          <Typography.Paragraph type="secondary">{zh.actions.purgeDialog.scope}</Typography.Paragraph>
          <Form form={form} layout="vertical">
            <Form.Item
              field="name"
              label={zh.actions.purgeDialog.confirmLabel(t.name)}
              rules={[
                { required: true, message: zh.errors.required('任务名称') },
                {
                  validator: (v: string | undefined, cb: (msg?: string) => void) => cb(v && v !== t.name ? zh.actions.purgeDialog.mismatch : undefined),
                },
              ]}
            >
              <Input placeholder={t.name} autoComplete="off" />
            </Form.Item>
          </Form>
        </>
      )}
    </Modal>
  );
}
