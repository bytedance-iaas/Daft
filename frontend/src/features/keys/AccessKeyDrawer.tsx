import { Alert, Button, Drawer, Form, Input, Message, Space } from '@arco-design/web-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { api, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { qk } from '../../api/queries';
import type { Credential, CredentialCreate, CredentialUpdate } from '../../api/types';
import { RegionSelect } from '../../components/RegionSelect';
import { VerifyTag } from '../../components/VerifyTag';
import { REGION_RE } from '../../lib/deeplink';
import { zh } from '../../locales/zh';

interface Values {
  name: string;
  region: string;
  access_key_id: string;
  secret_access_key: string;
  test_bucket: string;
  endpoint: string;
}

export function savedMessage(c: Credential): void {
  if (c.verify_state === 'ok') Message.success(zh.credentials.savedOk);
  else if (c.verify_state === 'unverified') Message.warning(zh.credentials.savedUnverified(c.last_verify_error ?? ''));
  else Message.warning(zh.credentials.savedFailed(c.last_verify_error ?? ''));
}

/**
 * Create or edit a TOS access key (07 §7, 08 §4). Saving only verifies identity and never fails
 * because of it; secrets are write-only and an empty secret on edit means «unchanged».
 */
export function AccessKeyDrawer({
  visible,
  editing,
  onClose,
  onSaved,
}: {
  visible: boolean;
  editing: Credential | null;
  onClose: () => void;
  onSaved?: (c: Credential) => void;
}) {
  const qc = useQueryClient();
  const [form] = Form.useForm<Values>();
  useEffect(() => {
    if (!visible) return;
    form.resetFields();
    form.setFieldsValue({
      name: editing?.name ?? '',
      region: editing?.meta.region ?? 'cn-beijing',
      access_key_id: '',
      secret_access_key: '',
      test_bucket: editing?.meta.test_bucket ?? '',
      endpoint: editing?.meta.endpoint ?? '',
    });
  }, [visible, editing, form]);

  const save = useMutation({
    mutationFn: async (v: Values) => {
      if (editing) {
        const body: CredentialUpdate = {
          region: v.region,
          ...(v.access_key_id?.trim() ? { access_key_id: v.access_key_id.trim() } : {}),
          ...(v.secret_access_key ? { secret_access_key: v.secret_access_key } : {}),
          test_bucket: (v.test_bucket ?? '').trim(),
          ...(v.endpoint?.trim() ? { endpoint: v.endpoint.trim() } : {}),
        };
        return unwrap(api().PUT('/credentials/{id}', { params: { path: { id: editing.id } }, body }));
      }
      const body: CredentialCreate = {
        name: v.name.trim(),
        region: v.region,
        access_key_id: v.access_key_id.trim(),
        secret_access_key: v.secret_access_key,
        ...(v.test_bucket?.trim() ? { test_bucket: v.test_bucket.trim() } : {}),
        ...(v.endpoint?.trim() ? { endpoint: v.endpoint.trim() } : {}),
      };
      return unwrap(api().POST('/credentials', { body }));
    },
    onSuccess: (c) => {
      savedMessage(c);
      // Never keep secrets around once they are saved.
      form.setFieldsValue({ access_key_id: '', secret_access_key: '' });
      void qc.invalidateQueries({ queryKey: qk.credentials });
      onSaved?.(c);
      onClose();
    },
    onError: (e) => Message.error(errorMessage(e)),
  });

  const required = (label: string) => ({ required: true, message: zh.errors.required(label) });

  return (
    <Drawer
      width={520}
      title={editing ? zh.credentials.editKey : zh.credentials.newKey}
      visible={visible}
      onCancel={onClose}
      unmountOnExit
      footer={
        <Space>
          <Button onClick={onClose}>{zh.common.cancel}</Button>
          <Button
            type="primary"
            loading={save.isPending}
            onClick={async () => {
              const v = await form.validate().catch(() => null);
              if (v) save.mutate(v);
            }}
          >
            {zh.credentials.saveVerify}
          </Button>
        </Space>
      }
    >
      <Alert type="info" content={zh.credentials.keysIntro} style={{ marginBottom: 16 }} />
      <Form form={form} layout="vertical" autoComplete="off">
        <Form.Item
          label={zh.credentials.name}
          field="name"
          rules={editing ? [] : [required(zh.credentials.name), { maxLength: 64, message: zh.errors.maxLength(zh.credentials.name, 64) }]}
          extra={zh.credentials.nameHelp}
        >
          <Input placeholder={zh.credentials.namePlaceholder} disabled={Boolean(editing)} />
        </Form.Item>
        <Form.Item
          label={zh.credentials.region}
          field="region"
          rules={[
            { required: true, message: zh.errors.requiredSelect(zh.credentials.region) },
            { validator: (v: string | undefined, cb: (m?: string) => void) => cb(v && !REGION_RE.test(v) ? zh.errors.regionBad : undefined) },
          ]}
        >
          <RegionField />
        </Form.Item>
        <Form.Item label={zh.credentials.akid} field="access_key_id" rules={editing ? [] : [required(zh.credentials.akid)]}>
          <Input placeholder={editing ? zh.credentials.keepSecret : zh.credentials.akidPlaceholder} autoComplete="off" />
        </Form.Item>
        <Form.Item label={zh.credentials.sk} field="secret_access_key" rules={editing ? [] : [required(zh.credentials.sk)]} extra={zh.credentials.skHelp}>
          <Input.Password placeholder={editing ? zh.credentials.keepSecret : zh.credentials.skPlaceholder} autoComplete="new-password" />
        </Form.Item>
        <Form.Item label={`${zh.credentials.testBucket}（${zh.common.optional}）`} field="test_bucket" extra={zh.credentials.testBucketHelp}>
          <Input placeholder="my-bucket" />
        </Form.Item>
        <Form.Item label={`${zh.credentials.endpoint}（${zh.common.optional}）`} field="endpoint" extra={zh.credentials.endpointHelpTos}>
          <Input placeholder="https://tos-cn-beijing.volces.com" />
        </Form.Item>
      </Form>
      {editing ? (
        <div>
          {zh.credentials.currentVerify}：<VerifyTag state={editing.verify_state} error={editing.last_verify_error} />
        </div>
      ) : null}
    </Drawer>
  );
}

/** RegionSelect wired to Arco Form's injected value/onChange. */
function RegionField(props: { value?: string; onChange?: (v: string) => void }) {
  return <RegionSelect value={props.value ?? ''} onChange={(v) => props.onChange?.(v)} ariaLabel={zh.credentials.region} />;
}
