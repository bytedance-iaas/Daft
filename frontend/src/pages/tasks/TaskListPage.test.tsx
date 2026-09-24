import { screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { EVENTS_CONFIG } from '../../api/events';
import { db, findTask } from '../../mocks/db';
import { server } from '../../mocks/server';
import { finishSubtask } from '../../mocks/subtaskSim';
import { requiredFieldLabels } from '../../test/forms';
import { recordRequests } from '../../test/record';
import { currentLocation, renderApp } from '../../test/render';

function row(name: string): HTMLElement {
  const link = screen.getByRole('link', { name });
  return link.closest('tr') as HTMLElement;
}

describe('任务列表 (07 §4.1)', () => {
  it('shows states in Chinese, 「错误」 for completed_with_errors, and the system pause hint', async () => {
    renderApp('/tasks');
    await screen.findByRole('link', { name: 'droid 前 50 条质检' });
    expect(within(row('droid 前 50 条质检')).getByText('错误')).toBeInTheDocument();
    expect(within(row('libero-10 抽检')).getByText('待启动')).toBeInTheDocument();
    expect(within(row('umi_640 全量质检')).getByText('运行中')).toBeInTheDocument();
    const widowx = row('widowx 回归');
    expect(within(widowx).getByText('已暂停')).toBeInTheDocument();
    expect(within(widowx).getByLabelText('系统将自动恢复')).toBeInTheDocument();
    expect(within(row('agibot 预检回归')).queryByLabelText('系统将自动恢复')).toBeNull();
    // Badges: pending adjudication links to the adjudication page; never exported → 「交付待导出」
    const droid = row('droid 前 50 条质检');
    expect(within(droid).getByRole('link', { name: '待裁决 10' })).toHaveAttribute('href', '/tasks/task_01HXR2D8/adjudication');
    expect(within(droid).getByText('交付待导出')).toBeInTheDocument();
    expect(within(droid).getByText('通过 41 · 拒绝 7 · 待补跑 2')).toBeInTheDocument();
    expect(screen.getByText('有未结束的任务，每 5 秒自动刷新')).toBeInTheDocument();
  });

  it('an unfinished task shows two progress lines, the current stage and the whole task, without time estimates', async () => {
    renderApp('/tasks');
    await screen.findByRole('link', { name: 'umi_640 全量质检' });
    const umi = row('umi_640 全量质检');
    expect(within(umi).getByTestId('progress-stage')).toHaveTextContent('VLM 档410 / 631');
    // 3 of 9 stages done (报告生成 and 交付 count once each) plus 410 / 631 of the current one.
    expect(within(umi).getByTestId('progress-overall')).toHaveTextContent('总进度41%');
    expect(umi).not.toHaveTextContent(/剩余|预计/);
    // A paused task keeps both lines (its bars turn gray).
    const widowx = row('widowx 回归');
    expect(within(widowx).getByTestId('progress-stage')).toHaveTextContent('VLM 档180 / 430');
    expect(within(widowx).getByTestId('progress-overall')).toHaveTextContent('总进度38%');
  });

  it('重试 shows 运行中 at once; the list polls while it runs and flips to the recomputed state when it ends (D46)', async () => {
    EVENTS_CONFIG.pollMs = 60;
    // Only finished tasks on this page: nothing but the subtask can keep the list polling.
    db.tasks = db.tasks.filter((t) => ['succeeded', 'completed_with_errors'].includes(t.state));
    try {
      const { user } = renderApp('/tasks');
      await screen.findByRole('link', { name: 'droid 前 50 条质检' });
      expect(screen.queryByText('有未结束的任务，每 5 秒自动刷新')).toBeNull();
      await user.click(within(row('droid 前 50 条质检')).getByRole('button', { name: /更多/ }));
      await user.click(await screen.findByRole('menuitem', { name: '重试（2 条）' }));
      const dialog = await screen.findByRole('dialog', { name: '重试出错的条目' });
      await user.click(within(dialog).getByRole('button', { name: '开始重试' }));
      await waitFor(() => expect(within(row('droid 前 50 条质检')).getByTestId('state-tag')).toHaveTextContent(/^运行中$/));
      expect(within(row('droid 前 50 条质检')).getByTestId('progress-subtask')).toHaveTextContent('子任务运行中通过 41 · 拒绝 7 · 待补跑 2');
      expect(screen.getByText('有未结束的任务，每 5 秒自动刷新')).toBeInTheDocument();
      // The retry ends on the server: the next poll shows the recomputed state, and polling stops.
      finishSubtask(findTask('task_01HXR2D8')!.id);
      await waitFor(() => expect(within(row('droid 前 50 条质检')).getByTestId('state-tag')).toHaveTextContent(/^已完成$/));
      expect(row('droid 前 50 条质检')).toHaveTextContent('通过 43 · 拒绝 7');
      await waitFor(() => expect(screen.queryByText('有未结束的任务，每 5 秒自动刷新')).toBeNull());
    } finally {
      EVENTS_CONFIG.pollMs = 5000;
    }
  });

  it('「更多」 has 人工裁决 for every task with a result, with the count when items are pending (D47)', async () => {
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'droid 前 50 条质检' });
    await user.click(within(row('so101 夜间批次')).getByRole('button', { name: /更多/ }));
    // Nothing pending on so101, and still the entry (the sidebar's 人工裁决 is another one).
    const menu = await waitFor(() => {
      const m = [...document.querySelectorAll<HTMLElement>('.arco-dropdown-menu')].at(-1);
      if (!m) throw new Error('no 更多 menu');
      return m;
    });
    await user.click(await within(menu).findByRole('menuitem', { name: '人工裁决' }));
    await waitFor(() => expect(currentLocation()).toBe('/tasks/task_01HXPZ2K/adjudication'));
  });

  it('人工裁决（N） in 「更多」 when items are pending; none for a task without a result', async () => {
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'droid 前 50 条质检' });
    await user.click(within(row('droid 前 50 条质检')).getByRole('button', { name: /更多/ }));
    expect(await screen.findByRole('menuitem', { name: '人工裁决（10）' })).toBeInTheDocument();
    await user.click(within(row('aloha 手眼标定')).getByRole('button', { name: /更多/ }));
    await waitFor(() => expect(screen.getAllByRole('menuitem', { name: '复制为新任务' }).length).toBeGreaterThan(1));
    const menus = [...document.querySelectorAll('.arco-dropdown-menu')];
    expect(menus[menus.length - 1]).not.toHaveTextContent('人工裁决');
  });

  it('a task summary shows the episodes skipped for missing source files when there are any (D40)', async () => {
    renderApp('/tasks?q=so101');
    await screen.findByRole('link', { name: 'so101 夜间批次' });
    expect(row('so101 夜间批次')).toHaveTextContent('通过 968 · 拒绝 56 · 缺源文件 3');
  });

  it('summarises the modules column and names the preset (data driven from the registry)', async () => {
    renderApp('/tasks');
    await screen.findByRole('link', { name: 'droid 前 50 条质检' });
    const droid = within(row('droid 前 50 条质检')).getByTestId('module-summary');
    expect(droid).toHaveTextContent('7 项');
    expect(droid).toHaveTextContent('1 项错误');
    expect(droid).toHaveTextContent('完整质检');
    expect(within(row('libero-10 抽检')).getByTestId('module-summary')).toHaveTextContent('快速质检');
  });

  it('paginates by page number with a total and keeps the page in the URL', async () => {
    const { user } = renderApp('/tasks');
    expect(await screen.findByText('共 40 条')).toBeInTheDocument();
    await user.click(screen.getByText('2', { selector: '.arco-pagination-item' }));
    await waitFor(() => expect(currentLocation()).toContain('page=2'));
    expect(await screen.findByRole('link', { name: '历史批次 11' })).toBeInTheDocument();
  });

  it('filters by state, module and name through the API', async () => {
    const seen: string[] = [];
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname.endsWith('/api/v1/tasks')) seen.push(new URL(request.url).search);
    });
    renderApp('/tasks?state=completed_with_errors&module=task_success');
    expect(await screen.findByRole('link', { name: 'droid 前 50 条质检' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'libero-10 抽检' })).toBeNull();
    expect(seen.some((s) => s.includes('state=completed_with_errors') && s.includes('module=task_success'))).toBe(true);
    server.events.removeAllListeners();
  });

  it('searches by name', async () => {
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'droid 前 50 条质检' });
    await user.type(screen.getByPlaceholderText('搜索任务名称或 ID'), 'so101{Enter}');
    await waitFor(() => expect(currentLocation()).toContain('q=so101'));
    await waitFor(() => expect(screen.queryByRole('link', { name: 'droid 前 50 条质检' })).toBeNull());
    expect(screen.getByRole('link', { name: 'so101 夜间批次' })).toBeInTheDocument();
  });

  it('offers actions by state: a running task can be paused', async () => {
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'umi_640 全量质检' });
    await user.click(within(row('umi_640 全量质检')).getByRole('button', { name: /更多/ }));
    await user.click(await screen.findByRole('menuitem', { name: '暂停' }));
    expect(await screen.findByText('已请求暂停：在飞的 episode 跑完后停下')).toBeInTheDocument();
    await waitFor(() => expect(within(row('umi_640 全量质检')).getByText('已暂停')).toBeInTheDocument());
  });

  it('a system-paused task cannot be resumed by hand; unfinished tasks cannot be deleted', async () => {
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'widowx 回归' });
    expect(within(row('widowx 回归')).getByRole('button', { name: '恢复' })).toBeDisabled();
    await user.click(within(row('widowx 回归')).getByRole('button', { name: /更多/ }));
    const del = await screen.findByRole('menuitem', { name: '删除' });
    expect(del).toHaveClass('arco-dropdown-menu-disabled');
  });

  it('删除 deletes the record only by default; 同时清理交付产物 starts unticked (requester item 19)', async () => {
    const seen = recordRequests();
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'so101 夜间批次' });
    await user.click(within(row('so101 夜间批次')).getByRole('button', { name: /更多/ }));
    await user.click(await screen.findByRole('menuitem', { name: '删除' }));
    const dialog = await screen.findByRole('dialog', { name: '删除任务' });
    const box = await within(dialog).findByRole('checkbox', { name: '同时清理交付产物' });
    expect(box).not.toBeChecked();
    expect(dialog).toHaveTextContent('不勾选时，TOS 上的交付产物不动');
    expect(within(dialog).queryByTestId('delete-purge-path')).toBeNull();
    await user.click(within(dialog).getByRole('button', { name: '删除' }));
    expect(await screen.findByText('已删除，30 天内可以在「已删除」筛选里恢复')).toBeInTheDocument();
    const writes = seen.filter((r) => r.method !== 'GET');
    expect(writes.map((r) => `${r.method} ${r.path}`)).toEqual(['DELETE /tasks/task_01HXPZ2K']);
    await waitFor(() => expect(screen.queryByRole('link', { name: 'so101 夜间批次' })).toBeNull());
  });

  it('删除 with 同时清理交付产物 shows the exact run directory, purges it, then deletes the record', async () => {
    const seen = recordRequests();
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'droid 前 50 条质检' });
    await user.click(within(row('droid 前 50 条质检')).getByRole('button', { name: /更多/ }));
    await user.click(await screen.findByRole('menuitem', { name: '删除' }));
    const dialog = await screen.findByRole('dialog', { name: '删除任务' });
    await user.click(await within(dialog).findByRole('checkbox', { name: '同时清理交付产物' }));
    expect(within(dialog).getByTestId('delete-purge-path')).toHaveTextContent('tos://pai-kit-deliveries/droid-50/20260920-130514/');
    expect(dialog).toHaveTextContent('会删掉这个任务在 TOS 上的批次目录，删了不能恢复');
    await user.click(within(dialog).getByRole('button', { name: '删除' }));
    expect(await screen.findByText(/^已删除，并开始清理 tos:\/\/pai-kit-deliveries\/droid-50\/20260920-130514\/（212 MiB）/)).toBeInTheDocument();
    const writes = seen.filter((r) => r.method !== 'GET');
    expect(writes.map((r) => `${r.method} ${r.path}`)).toEqual(['POST /tasks/task_01HXR2D8/purge-artifacts', 'DELETE /tasks/task_01HXR2D8']);
    expect(writes[0].body).toEqual({ confirm_path: 'tos://pai-kit-deliveries/droid-50/20260920-130514/' });
    expect(findTask('task_01HXR2D8')!.deleted_at).toBeTruthy();
  });

  it('when the purge fails nothing is deleted and the dialog stays with the reason', async () => {
    server.use(http.post('*/api/v1/tasks/:id/purge-artifacts', () => HttpResponse.json({ error: { code: 'validation_failed', message: '连不上 TOS：timeout' } }, { status: 400 })));
    const seen = recordRequests();
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'droid 前 50 条质检' });
    await user.click(within(row('droid 前 50 条质检')).getByRole('button', { name: /更多/ }));
    await user.click(await screen.findByRole('menuitem', { name: '删除' }));
    const dialog = await screen.findByRole('dialog', { name: '删除任务' });
    await user.click(await within(dialog).findByRole('checkbox', { name: '同时清理交付产物' }));
    await user.click(within(dialog).getByRole('button', { name: '删除' }));
    expect(await screen.findByText('交付产物没能清理，任务也没有删除：连不上 TOS：timeout')).toBeInTheDocument();
    expect(seen.some((r) => r.method === 'DELETE')).toBe(false);
    expect(findTask('task_01HXR2D8')!.deleted_at).toBeNull();
    expect(screen.getByRole('dialog', { name: '删除任务' })).toBeInTheDocument();
  });

  it('a task that never wrote a run directory has nothing to purge', async () => {
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'libero-10 抽检' });
    await user.click(within(row('libero-10 抽检')).getByRole('button', { name: /更多/ }));
    await user.click(await screen.findByRole('menuitem', { name: '删除' }));
    const dialog = await screen.findByRole('dialog', { name: '删除任务' });
    expect(await within(dialog).findByText('这个任务还没有写过交付产物。')).toBeInTheDocument();
    expect(within(dialog).queryByRole('checkbox')).toBeNull();
  });

  it('deleted tasks are listed under 已删除 and can be restored', async () => {
    const { user } = renderApp('/tasks?state=deleted');
    await screen.findByRole('link', { name: '误建的重复任务' });
    await user.click(within(row('误建的重复任务')).getByRole('button', { name: '恢复' }));
    expect(await screen.findByText('已恢复')).toBeInTheDocument();
  });

  it('清理交付产物 shows the exact run directory and needs the task name typed', async () => {
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'droid 前 50 条质检' });
    await user.click(within(row('droid 前 50 条质检')).getByRole('button', { name: /更多/ }));
    await user.click(await screen.findByRole('menuitem', { name: '清理交付产物' }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByTestId('purge-path')).toHaveTextContent('tos://pai-kit-deliveries/droid-50/20260920-130514/');
    const input = within(dialog).getByRole('textbox');
    // Required with a red asterisk, and must match the task name.
    expect(requiredFieldLabels(dialog)).toEqual(['输入任务名称「droid 前 50 条质检」确认']);
    await user.type(input, 'wrong');
    await user.click(within(dialog).getByRole('button', { name: '清理' }));
    expect(await within(dialog).findByText('和任务名称不一致')).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, 'droid 前 50 条质检');
    await user.click(within(dialog).getByRole('button', { name: '清理' }));
    expect(await screen.findByText(/已开始清理 tos:\/\/pai-kit-deliveries\/droid-50\/20260920-130514\//)).toBeInTheDocument();
  });

  it('启动 that fails the pre-start checks lists every check with its reason and target (W8 details.checks)', async () => {
    findTask('task_01HXR6T3')!.output.credential = 'readonly-tos';
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'libero-10 抽检' });
    await user.click(within(row('libero-10 抽检')).getByRole('button', { name: '启动' }));
    const dialog = await screen.findByRole('dialog', { name: '开始前检查没过，任务没有开始' });
    const list = within(dialog).getByTestId('precheck-list');
    expect(list).toHaveTextContent('数据集能读：通过');
    expect(list).toHaveTextContent('交付目录能写：存储桶 pai-kit-deliveries 对访问密钥 readonly-tos 只读');
    expect(list).toHaveTextContent('tos://pai-kit-deliveries/libero_10-0920');
  });

  it('启动 shows the state the answer carries at once, without waiting for the next poll (07 §4.1)', async () => {
    // Every refetch of the list is held: what the row shows after the click can only come from
    // the answer of the action itself.
    let release = () => {};
    const held = new Promise<void>((r) => (release = r));
    let gets = 0;
    server.use(
      http.get('*/api/v1/tasks', async () => {
        gets += 1;
        if (gets > 1) await held;
      }),
    );
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'libero-10 抽检' });
    expect(within(row('libero-10 抽检')).getByText('待启动')).toBeInTheDocument();
    await user.click(within(row('libero-10 抽检')).getByRole('button', { name: '启动' }));
    await waitFor(() => expect(within(row('libero-10 抽检')).getByText('排队中')).toBeInTheDocument());
    expect(gets).toBeGreaterThan(1);
    release();
  });

  it('shows the page-level error with the Daemon message when the list cannot load', async () => {
    server.use(http.get('*/api/v1/tasks', () => HttpResponse.json({ error: { code: 'internal', message: '数据库暂时不可用，请稍后重试' } }, { status: 500 })));
    renderApp('/tasks');
    expect(await screen.findByText('数据库暂时不可用，请稍后重试')).toBeInTheDocument();
    expect(screen.getByText('页面数据没能取回')).toBeInTheDocument();
  });
});
