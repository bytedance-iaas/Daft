import { Form, Input, Message, Modal, Typography } from '@arco-design/web-react';
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { api, unwrap } from '../../api/client';
import { errorMessage, isApiError } from '../../api/errors';
import { qk } from '../../api/queries';
import type { Task } from '../../api/types';
import { zh } from '../../locales/zh';

/** 「改名称和备注」 (D20): the only fields a started task accepts; guarded by If-Match. */
export function RenameDialog({ task, visible, onClose }: { task: Task; visible: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [form] = Form.useForm<{ name: string; note: string }>();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (visible) form.setFieldsValue({ name: task.name, note: task.note ?? '' });
  }, [visible, task, form]);
  const save = async () => {
    const v = await form.validate().catch(() => null);
    if (!v) return;
    setBusy(true);
    try {
      const t = await unwrap(
        api().PATCH('/tasks/{id}', { params: { path: { id: task.id }, header: { 'If-Match': String(task.updated_at) } }, body: { name: v.name.trim(), note: (v.note ?? '').trim() } }),
      );
      qc.setQueryData(qk.task(task.id), t);
      void qc.invalidateQueries({ queryKey: qk.tasksAll });
      Message.success(zh.taskDetail.renamed);
      onClose();
    } catch (e) {
      Message.error(errorMessage(e));
      // 412: someone else changed it; take the fresh copy so the next save carries the new tag.
      if (isApiError(e, 'precondition_failed')) void qc.invalidateQueries({ queryKey: qk.task(task.id) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title={zh.taskDetail.renameTitle} visible={visible} onCancel={onClose} onOk={() => void save()} confirmLoading={busy} okText={zh.common.save} cancelText={zh.common.cancel} unmountOnExit>
      <Form form={form} layout="vertical">
        <Form.Item label={zh.taskForm.name} field="name" rules={[{ required: true, message: zh.errors.required(zh.taskForm.name) }, { maxLength: 128, message: zh.errors.maxLength(zh.taskForm.name, 128) }]}>
          <Input placeholder={zh.taskForm.namePlaceholder} />
        </Form.Item>
        <Form.Item label={zh.taskForm.note} field="note" rules={[{ maxLength: 2000, message: zh.errors.maxLength(zh.taskForm.note, 2000) }]}>
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 5 }} placeholder={zh.taskForm.notePlaceholder} />
        </Form.Item>
      </Form>
      <Typography.Text type="secondary">{task.state === 'created' ? zh.taskDetail.renameHintCreated : zh.taskDetail.renameHint}</Typography.Text>
    </Modal>
  );
}
