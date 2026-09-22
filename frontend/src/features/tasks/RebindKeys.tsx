import { Alert, Button, Form, Message, Modal, Select } from '@arco-design/web-react';
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { api, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { qk, useCredentials } from '../../api/queries';
import type { Credential, Task } from '../../api/types';
import { isTerminalState } from '../../lib/taskView';
import { zh } from '../../locales/zh';

/**
 * Which of a task's access keys are gone: C4 1.2 returns null once a key was deleted; with 1.1 the
 * name stays, so a name missing from the key list counts as deleted too.
 */
export function missingKeys(t: Pick<Task, 'input' | 'output'>, keys: readonly Pick<Credential, 'name'>[] | undefined): { input: boolean; output: boolean } {
  const gone = (name: string | null | undefined) => !name || Boolean(keys && !keys.some((k) => k.name === name));
  return { input: t.input.source === 'tos' && gone(t.input.credential), output: gone(t.output.credential) };
}

function RebindDialog({ task, missing, visible, onClose }: { task: Task; missing: { input: boolean; output: boolean }; visible: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const keys = useCredentials();
  const [form] = Form.useForm<{ input_credential?: string; output_credential?: string }>();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (visible) form.resetFields();
  }, [visible, form]);
  const options = (keys.data?.items ?? []).map((c) => ({ label: c.name, value: c.name }));
  return (
    <Modal
      title={zh.taskDetail.rebindTitle}
      visible={visible}
      onCancel={onClose}
      confirmLoading={busy}
      okText={zh.common.save}
      cancelText={zh.common.cancel}
      unmountOnExit
      onOk={async () => {
        const v = await form.validate().catch(() => null);
        if (!v) return;
        setBusy(true);
        try {
          const body = { ...(v.input_credential ? { input_credential: v.input_credential } : {}), ...(v.output_credential ? { output_credential: v.output_credential } : {}) };
          await unwrap(api().POST('/tasks/{id}/rebind-credentials', { params: { path: { id: task.id } }, body }));
          Message.success(zh.taskDetail.rebound);
          void qc.invalidateQueries({ queryKey: qk.task(task.id) });
          onClose();
        } catch (e) {
          Message.error(errorMessage(e));
        } finally {
          setBusy(false);
        }
      }}
    >
      <Alert type="info" content={zh.taskDetail.rebindIntro} style={{ marginBottom: 12 }} />
      <Form form={form} layout="vertical">
        {missing.input ? (
          <Form.Item label={zh.taskDetail.rebindInput} field="input_credential" rules={[{ required: true, message: zh.errors.requiredSelect(zh.taskDetail.rebindInput) }]}>
            <Select options={options} placeholder={zh.taskForm.credentialPlaceholder} aria-label={zh.taskDetail.rebindInput} />
          </Form.Item>
        ) : null}
        {missing.output ? (
          <Form.Item label={zh.taskDetail.rebindOutput} field="output_credential" rules={[{ required: true, message: zh.errors.requiredSelect(zh.taskDetail.rebindOutput) }]}>
            <Select options={options} placeholder={zh.taskForm.credentialPlaceholder} aria-label={zh.taskDetail.rebindOutput} />
          </Form.Item>
        ) : null}
      </Form>
    </Modal>
  );
}

/** Banner + dialog on a finished task whose access key was deleted (rebind-credentials). */
export function RebindKeysBanner({ task }: { task: Task }) {
  const keys = useCredentials();
  const [open, setOpen] = useState(false);
  if (!isTerminalState(task.state) || !keys.data) return null;
  const missing = missingKeys(task, keys.data.items);
  if (!missing.input && !missing.output) return null;
  return (
    <>
      <Alert
        type="warning"
        data-testid="rebind-banner"
        title={zh.taskDetail.keyGone}
        content={zh.taskDetail.keyGoneDesc}
        action={
          <Button size="small" type="primary" onClick={() => setOpen(true)}>
            {zh.taskDetail.rebind}
          </Button>
        }
      />
      <RebindDialog task={task} missing={missing} visible={open} onClose={() => setOpen(false)} />
    </>
  );
}
