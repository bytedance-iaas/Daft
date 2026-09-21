import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { findDrawer, fill, pick } from '../../test/arco';
import { fieldErrors, requiredFieldLabels } from '../../test/forms';
import { recordRequests } from '../../test/record';
import { currentLocation, renderApp } from '../../test/render';

function row(table: HTMLElement, name: string): HTMLElement {
  return within(table).getByText(name).closest('tr') as HTMLElement;
}

describe('密钥与资源管理 (07 §7)', () => {
  it('lists keys with verify states and references; secrets never appear', async () => {
    renderApp('/credentials');
    const table = await screen.findByTestId('keys-table');
    await within(table).findByText('prod-tos');
    expect(screen.getByRole('tab', { name: 'TOS 访问密钥（4）' })).toBeInTheDocument();
    expect(row(table, 'prod-tos')).toHaveTextContent('华北 2（北京）');
    expect(row(table, 'prod-tos')).toHaveTextContent('••••7Q2D');
    expect(row(table, 'prod-tos')).toHaveTextContent('9 个任务其中 1 个未结束');
    expect(row(table, 'partner-upload')).toHaveTextContent('未验证');
    expect(row(table, 'old-ci')).toHaveTextContent('验证失败');
    expect(row(table, 'old-ci')).toHaveTextContent('SignatureDoesNotMatch：Secret Access Key 不对');
    expect(document.body.textContent).not.toMatch(/secret_access_key|api_key/);
  });

  it('新建访问密钥: red * on required fields, validation, and a failed verification still saves', async () => {
    const seen = recordRequests();
    const { user } = renderApp('/credentials');
    await user.click(await screen.findByRole('button', { name: '新建访问密钥' }));
    const drawer = await findDrawer('新建访问密钥');
    expect(requiredFieldLabels(drawer)).toEqual(['名称', '地域', 'Access Key ID', 'Secret Access Key']);
    await user.click(within(drawer).getByRole('button', { name: '保存并验证' }));
    await waitFor(() => expect(fieldErrors(drawer)).toEqual(['请填写名称', '请填写 Access Key ID', '请填写 Secret Access Key']));
    await fill(user, '名称', 'lab-tos', drawer);
    await fill(user, 'Access Key ID', 'BAD-AKID', drawer);
    await fill(user, 'Secret Access Key', 's3cr3t', drawer);
    await user.click(within(drawer).getByRole('button', { name: '保存并验证' }));
    expect(await screen.findByText('已保存，但验证失败：SignatureDoesNotMatch：Secret Access Key 不对。任务开始前还会再检查')).toBeInTheDocument();
    const post = seen.find((r) => r.method === 'POST' && r.path === '/credentials');
    expect(post?.body).toEqual({ name: 'lab-tos', region: 'cn-beijing', access_key_id: 'BAD-AKID', secret_access_key: 's3cr3t' });
    const table = screen.getByTestId('keys-table');
    await waitFor(() => expect(row(table, 'lab-tos')).toHaveTextContent('验证失败'));
    expect(table).not.toHaveTextContent('s3cr3t');
  });

  it('编辑: the name is fixed and an empty secret means unchanged', async () => {
    const seen = recordRequests();
    const { user } = renderApp('/credentials');
    const table = await screen.findByTestId('keys-table');
    await within(table).findByText('partner-upload');
    await user.click(within(row(table, 'partner-upload')).getByRole('button', { name: '编辑' }));
    const drawer = await findDrawer('编辑访问密钥');
    expect(within(drawer).getByDisplayValue('partner-upload')).toBeDisabled();
    // Both secrets start empty (write only); leaving them empty keeps them.
    for (const input of within(drawer).getAllByPlaceholderText('已保存，留空表示不修改')) expect(input).toHaveValue('');
    await fill(user, '测试用存储桶（选填）', 'partner-bucket', drawer);
    await user.click(within(drawer).getByRole('button', { name: '保存并验证' }));
    expect(await screen.findByText('已保存，身份验证通过')).toBeInTheDocument();
    const put = seen.find((r) => r.method === 'PUT');
    expect(put?.path).toBe('/credentials/cred_partner');
    expect(put?.body).toEqual({ region: 'cn-shanghai', test_bucket: 'partner-bucket' });
  });

  it('删除: blocked while an unfinished task uses the key; finished references need the confirmation', async () => {
    const seen = recordRequests();
    const { user } = renderApp('/credentials');
    const table = await screen.findByTestId('keys-table');
    await within(table).findByText('prod-tos');
    await user.click(within(row(table, 'prod-tos')).getByRole('button', { name: '删除' }));
    const info = await screen.findByRole('dialog', { name: '删除访问密钥「prod-tos」' });
    expect(info).toHaveTextContent('它正被 1 个未结束的任务使用，不能删除。');
    await user.click(within(info).getByRole('button', { name: '确定' }));
    expect(seen.some((r) => r.method === 'DELETE')).toBe(false);
    await user.click(within(row(table, 'old-ci')).getByRole('button', { name: '删除' }));
    const confirm = await screen.findByRole('dialog', { name: '删除访问密钥「old-ci」' });
    expect(confirm).toHaveTextContent('它被 1 个已结束的任务引用。');
    await user.click(within(confirm).getByRole('button', { name: '删除' }));
    expect(await screen.findByText('已删除')).toBeInTheDocument();
    const del = seen.find((r) => r.method === 'DELETE');
    expect(del?.path).toBe('/credentials/cred_oldci');
    expect(del?.query.get('confirm')).toBe('true');
    await waitFor(() => expect(within(screen.getByTestId('keys-table')).queryByText('old-ci')).toBeNull());
  });

  it('重新验证 shows the result with its reason', async () => {
    const { user } = renderApp('/credentials');
    const table = await screen.findByTestId('keys-table');
    await within(table).findByText('partner-upload');
    await user.click(within(row(table, 'partner-upload')).getByRole('button', { name: '重新验证' }));
    expect(await screen.findByText('验证结果：未验证（这对密钥没有列存储桶的权限，也没填测试用存储桶）')).toBeInTheDocument();
  });

  it('VLM 后端 (#vlm): list, models in the drawer, manual model check, delete refused while in use', async () => {
    const seen = recordRequests();
    const { user } = renderApp('/credentials#vlm');
    const table = await screen.findByTestId('backends-table');
    await within(table).findByText('ark-prod');
    expect(row(table, 'ark-prod')).toHaveTextContent('方舟');
    expect(row(table, 'vllm-a100')).toHaveTextContent('连接超时：10 秒内没有响应');
    expect(row(table, 'ark-ep')).toHaveTextContent('手填');
    await user.click(within(row(table, 'ark-ep')).getByRole('button', { name: '编辑' }));
    const drawer = await findDrawer('编辑 VLM 后端');
    const models = within(drawer).getByTestId('backend-models');
    expect(models).toHaveTextContent('ep-20260915173012-x7k2p');
    await fill(user, '请输入 Model ID 或推理接入点 ID（ep-…）', 'bad-model', drawer);
    await user.click(within(drawer).getByRole('button', { name: '添加并验证' }));
    expect(await within(drawer).findByText('用「bad-model」发了一次最小请求，没调通：InvalidEndpointOrModel.NotFound')).toBeInTheDocument();
    await pick(user, 'ep-20260915173012-x7k2p 思考强度', 'high', drawer);
    await waitFor(() => expect(seen.find((r) => r.method === 'PATCH')?.body).toEqual({ reasoning_effort: 'high' }));
    await user.click(within(drawer).getByRole('button', { name: '关闭' }));
    await user.click(within(row(table, 'ark-prod')).getByRole('button', { name: '删除' }));
    const confirm = await screen.findByRole('dialog', { name: '删除 VLM 后端「ark-prod」' });
    await user.click(within(confirm).getByRole('button', { name: '删除' }));
    expect(await screen.findByText(/VLM 后端「ark-prod」正被 \d+ 个未结束的任务使用，不能删除/)).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: /TOS 访问密钥/ }));
    await waitFor(() => expect(currentLocation()).toBe('/credentials'));
  });
});
