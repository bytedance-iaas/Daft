import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ReasoningLevel } from '../../api/types';
import { db } from '../../mocks/db';
import { findDrawer, fill, pick } from '../../test/arco';
import { fieldErrors, requiredFieldLabels } from '../../test/forms';
import { recordRequests } from '../../test/record';
import { currentLocation, renderApp } from '../../test/render';

function row(table: HTMLElement, name: string): HTMLElement {
  return within(table).getByText(name).closest('tr') as HTMLElement;
}

const ADD_MODEL = '搜索已有模型，或填 Model ID、推理接入点 ID（ep-…）添加';
const modelRows = (models: HTMLElement) => [...models.querySelectorAll('tbody tr')].map((r) => r.querySelector('.mono')?.textContent ?? '');

const findCredential = (id: string) => db.credentials.find((c) => c.id === id);

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

  it('删除 a backend only finished tasks use: 409 confirm_required, a second dialog, then confirm=true (W8)', async () => {
    const seen = recordRequests();
    const { user } = renderApp('/credentials#vlm');
    const table = await screen.findByTestId('backends-table');
    await within(table).findByText('ark-ep');
    await user.click(within(row(table, 'ark-ep')).getByRole('button', { name: '删除' }));
    const first = await screen.findByRole('dialog', { name: '删除 VLM 后端「ark-ep」' });
    await user.click(within(first).getByRole('button', { name: '删除' }));
    const again = await screen.findByTestId('delete-confirm-again');
    expect(again).toHaveTextContent('VLM 后端「ark-ep」被 1 个已结束的任务引用，删除要确认');
    const deletes = () => seen.filter((r) => r.method === 'DELETE' && r.path === '/vlm-backends/vb_ark_ep');
    expect(deletes().map((r) => r.query.get('confirm'))).toEqual([null]);
    await user.click(screen.getByRole('button', { name: '仍然删除' }));
    expect(await screen.findByText('已删除')).toBeInTheDocument();
    expect(deletes().map((r) => r.query.get('confirm'))).toEqual([null, 'true']);
    await waitFor(() => expect(within(screen.getByTestId('backends-table')).queryByText('ark-ep')).toBeNull());
  });

  it('a key whose references changed since the list loaded still gets the second confirmation', async () => {
    const seen = recordRequests();
    const { user } = renderApp('/credentials');
    const table = await screen.findByTestId('keys-table');
    await within(table).findByText('partner-upload');
    // A task finished with this key after the list was loaded.
    findCredential('cred_partner')!.references = { active_tasks: 0, historical_tasks: 2 };
    await user.click(within(row(table, 'partner-upload')).getByRole('button', { name: '删除' }));
    const first = await screen.findByRole('dialog', { name: '删除访问密钥「partner-upload」' });
    expect(first).toHaveTextContent('删除后不能恢复。');
    await user.click(within(first).getByRole('button', { name: '删除' }));
    expect(await screen.findByTestId('delete-confirm-again')).toHaveTextContent('被 2 个已结束的任务引用');
    await user.click(screen.getByRole('button', { name: '仍然删除' }));
    expect(await screen.findByText('已删除')).toBeInTheDocument();
    expect(seen.filter((r) => r.method === 'DELETE').map((r) => r.query.get('confirm'))).toEqual([null, 'true']);
  });

  it('重新验证 shows the result with its reason', async () => {
    const { user } = renderApp('/credentials');
    const table = await screen.findByTestId('keys-table');
    await within(table).findByText('partner-upload');
    await user.click(within(row(table, 'partner-upload')).getByRole('button', { name: '重新验证' }));
    expect(await screen.findByText('验证结果：未验证（这对密钥没有列存储桶的权限，也没填测试用存储桶）')).toBeInTheDocument();
  });

  it('模型列表: 10 条一页，搜索框同时筛选和添加，设为默认只留一个 (C4 1.6.0)', async () => {
    const backend = db.backends.find((b) => b.id === 'vb_ark_prod')!;
    backend.models = [
      ...backend.models,
      ...Array.from({ length: 12 }, (_, i) => ({
        id: `vm_extra_${i}`,
        is_default: false,
        model_name: `doubao-seed-1-6-lite-2510${String(i).padStart(2, '0')}`,
        reasoning_effort: null,
        max_concurrency: null,
        capabilities: { vision: true, reasoning_effort_levels: ['minimal', 'low', 'medium', 'high'] as ReasoningLevel[] },
        source: 'listed' as const,
      })),
    ];
    const seen = recordRequests();
    const { user } = renderApp('/credentials#vlm');
    const table = await screen.findByTestId('backends-table');
    await within(table).findByText('ark-prod');
    await user.click(within(row(table, 'ark-prod')).getByRole('button', { name: '编辑' }));
    const drawer = await findDrawer('编辑 VLM 后端');
    const models = within(drawer).getByTestId('backend-models');
    // 15 models, 10 per page.
    expect(modelRows(models)).toHaveLength(10);
    expect(within(models).getByText('共 15 条')).toBeInTheDocument();
    // The box filters as you type — the letters only have to appear in order.
    await fill(user, ADD_MODEL, 'd20lite', drawer);
    await waitFor(() => expect(modelRows(models)).toEqual(['doubao-seed-2-0-lite-260215']));
    expect(within(drawer).getByText('筛出 1 / 15 个模型；列表里没有就直接添加')).toBeInTheDocument();
    // A name the list already has is not added again.
    await fill(user, ADD_MODEL, 'doubao-seed-2-0-lite-260215', drawer);
    await waitFor(() => expect(within(drawer).getByRole('button', { name: '添加并验证' })).toBeDisabled());
    expect(within(drawer).getByText('这个模型已经在列表里了')).toBeInTheDocument();
    // 设为默认 moves the flag: the seeded default (pro) loses it.
    await fill(user, ADD_MODEL, 'doubao-seed-1-6-251015', drawer);
    await waitFor(() => expect(modelRows(models)).toEqual(['doubao-seed-1-6-251015']));
    await user.click(within(models).getByRole('button', { name: '设为默认' }));
    await waitFor(() => expect(seen.find((r) => r.method === 'PATCH')?.body).toEqual({ is_default: true }));
    expect(await screen.findByText('新建任务默认用「doubao-seed-1-6-251015」')).toBeInTheDocument();
    await waitFor(() => expect(db.backends.flatMap((b) => b.models).filter((m) => m.is_default).map((m) => m.model_name)).toEqual(['doubao-seed-1-6-251015']));
    await waitFor(() => expect(within(models).getByText('默认')).toBeInTheDocument());
    await user.click(within(models).getByRole('button', { name: '取消默认' }));
    await waitFor(() => expect(db.backends.flatMap((b) => b.models).some((m) => m.is_default)).toBe(false));
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
    await fill(user, ADD_MODEL, 'bad-model', drawer);
    await user.click(within(drawer).getByRole('button', { name: '添加并验证' }));
    expect(await within(drawer).findByText('用「bad-model」发了一次最小请求，没调通：InvalidEndpointOrModel.NotFound')).toBeInTheDocument();
    await fill(user, ADD_MODEL, '', drawer); // the box also filters the list: clear it to see every model
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
