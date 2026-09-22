import { Form, Input, Message, Modal } from '@arco-design/web-react';
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, idempotencyKey, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { qk } from '../../api/queries';
import type { DatasetItem, SourceChange } from '../../api/types';
import { zh } from '../../locales/zh';
import { FingerprintDialog } from '../tasks/FingerprintDialog';

type Target = Pick<DatasetItem, 'id' | 'name'> & { note?: string | null };

function EditDialog({ target, onClose }: { target: Target | null; onClose: () => void }) {
  const qc = useQueryClient();
  const [form] = Form.useForm<{ name: string; note: string }>();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (target) form.setFieldsValue({ name: target.name, note: target.note ?? '' });
  }, [target, form]);
  return (
    <Modal
      title={zh.datasets.edit}
      visible={Boolean(target)}
      onCancel={onClose}
      confirmLoading={busy}
      okText={zh.common.save}
      cancelText={zh.common.cancel}
      unmountOnExit
      onOk={async () => {
        const v = await form.validate().catch(() => null);
        if (!v || !target) return;
        setBusy(true);
        try {
          await unwrap(api().PATCH('/datasets/{id}', { params: { path: { id: target.id } }, body: { name: v.name.trim(), note: v.note?.trim() || null } }));
          Message.success(zh.taskDetail.renamed);
          void qc.invalidateQueries({ queryKey: qk.datasetsAll });
          void qc.invalidateQueries({ queryKey: qk.dataset(target.id) });
          onClose();
        } catch (e) {
          Message.error(errorMessage(e));
        } finally {
          setBusy(false);
        }
      }}
    >
      <Form form={form} layout="vertical">
        <Form.Item label={zh.datasets.name} field="name" rules={[{ required: true, message: zh.errors.required(zh.datasets.name) }, { maxLength: 128, message: zh.errors.maxLength(zh.datasets.name, 128) }]}>
          <Input />
        </Form.Item>
        <Form.Item label={zh.datasets.note} field="note" rules={[{ maxLength: 2000, message: zh.errors.maxLength(zh.datasets.note, 2000) }]}>
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 5 }} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

/** 重新检查 / 重新预检 / 删除 / 改名称和备注 for registered datasets (07 §4.4). */
export function useDatasetActions(): {
  recheck: (d: Target) => void;
  repreflight: (d: Target) => void;
  remove: (d: Target) => void;
  edit: (d: Target) => void;
  newTask: (d: Target) => void;
  dialogs: ReactNode;
} {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [changed, setChanged] = useState<{ target: Target; change: SourceChange | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<Target | null>(null);
  const refresh = (id: string) => {
    void qc.invalidateQueries({ queryKey: qk.datasetsAll });
    void qc.invalidateQueries({ queryKey: qk.dataset(id) });
    void qc.invalidateQueries({ queryKey: qk.overview });
  };
  const doRepreflight = async (d: Target) => {
    setBusy(true);
    try {
      await unwrap(api().POST('/datasets/{id}/repreflight', { params: { path: { id: d.id }, header: { 'Idempotency-Key': idempotencyKey() } } }));
      Message.success(zh.datasets.repreflightDone);
      setChanged(null);
      refresh(d.id);
    } catch (e) {
      Message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return {
    recheck: (d) => {
      void (async () => {
        try {
          const c = await unwrap(api().POST('/datasets/{id}/recheck', { params: { path: { id: d.id } } }));
          refresh(d.id);
          if (c.result === 'same') Message.success(zh.datasets.recheckSame);
          else {
            Message.warning(zh.datasets.recheckChanged);
            setChanged({ target: d, change: c.change ?? null });
          }
        } catch (e) {
          Message.error(errorMessage(e));
        }
      })();
    },
    repreflight: (d) => void doRepreflight(d),
    remove: (d) =>
      Modal.confirm({
        title: zh.datasets.deleteTitle(d.name),
        content: zh.datasets.deleteContent,
        okText: zh.datasets.delete,
        cancelText: zh.common.cancel,
        okButtonProps: { status: 'danger' },
        onOk: async () => {
          try {
            await unwrap(api().DELETE('/datasets/{id}', { params: { path: { id: d.id } } }));
            Message.success(zh.datasets.deleted);
            refresh(d.id);
            navigate('/datasets');
          } catch (e) {
            Message.error(errorMessage(e));
          }
        },
      }),
    edit: (d) => setEditing(d),
    newTask: (d) => navigate(`/tasks/new?dataset_id=${encodeURIComponent(d.id)}`),
    dialogs: (
      <>
        <FingerprintDialog context="dataset" visible={Boolean(changed)} change={changed?.change ?? null} loading={busy} onCancel={() => setChanged(null)} onConfirm={() => changed && void doRepreflight(changed.target)} />
        <EditDialog target={editing} onClose={() => setEditing(null)} />
      </>
    ),
  };
}
