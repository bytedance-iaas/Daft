import { act, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { EVENTS_CONFIG, setEventSourceFactory } from '../../api/events';
import { db } from '../../mocks/db';
import { server } from '../../mocks/server';
import { pick } from '../../test/arco';
import { FakeEventSource } from '../../test/fakeEventSource';
import { fieldErrors, requiredFieldLabels } from '../../test/forms';
import { currentLocation, renderApp } from '../../test/render';

const MAIN = 'task_01HXR2D8';
const RUNNING = 'task_01HXR4M7';

beforeEach(() => {
  EVENTS_CONFIG.pollMs = 60;
  FakeEventSource.instances = [];
});
afterEach(() => {
  setEventSourceFactory(null);
  EVENTS_CONFIG.pollMs = 5000;
  server.events.removeAllListeners();
});

function countTaskGets(id: string): () => number {
  let n = 0;
  server.events.on('request:start', ({ request }) => {
    if (request.method === 'GET' && new URL(request.url).pathname.endsWith(`/api/v1/tasks/${id}`)) n += 1;
  });
  return () => n;
}

describe('任务详情 (07 §4.2)', () => {
  it('shows the header, the never-exported banner (导出, not 重新导出) and the report overview', async () => {
    renderApp(`/tasks/${MAIN}`);
    expect(await screen.findByRole('heading', { name: /droid 前 50 条质检/ })).toBeInTheDocument();
    expect(screen.getAllByText('错误').length).toBeGreaterThan(0);
    const banner = screen.getByTestId('delivery-banner');
    expect(banner).toHaveTextContent('交付数据集待导出。');
    expect(within(banner).getByRole('button', { name: '导出' })).toBeInTheDocument();
    const summary = screen.getByTestId('report-summary');
    for (const t of ['50', '41', '7', '2', '10', '82%', '含待裁决 10 条']) expect(summary).toHaveTextContent(t);
  });

  it('stage bars explain why the total dropped; module errors expand to the episodes', async () => {
    const { user } = renderApp(`/tasks/${MAIN}`);
    expect(await screen.findByTestId('stage-frame')).toHaveTextContent('数值档拦下了 1 条（ep 18，残段），所以后面的档是 49 条');
    const table = screen.getByTestId('modules-table');
    await user.click(await within(table).findByRole('button', { name: /2 条待补跑/ }));
    const list = await screen.findByTestId('error-episodes');
    expect(list).toHaveTextContent('ep 7');
    expect(list).toHaveTextContent('ep 31');
    expect(within(table).getByText('运动学极限')).toBeInTheDocument();
    expect(within(table).getByText('预检未读到机器人型号，创建时选择跳过')).toBeInTheDocument();
  });

  it('timeline links each result version to its report; tokens show no money (D12)', async () => {
    renderApp(`/tasks/${MAIN}`);
    const tl = await screen.findByTestId('timeline');
    expect(within(tl).getByRole('link', { name: '查看 r0001 报告' })).toHaveAttribute('href', `/tasks/${MAIN}/report?rev=1`);
    expect(within(tl).getByText('当前版本')).toBeInTheDocument();
    expect(within(tl).getByText('系统暂停')).toBeInTheDocument();
    const tokens = screen.getByTestId('token-totals');
    expect(tokens).toHaveTextContent('2.01M');
    expect(tokens).not.toHaveTextContent('¥');
  });

  it('改名称和备注 validates the name and PATCHes with If-Match', async () => {
    let ifMatch = '';
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'PATCH') ifMatch = request.headers.get('If-Match') ?? '';
    });
    const { user } = renderApp(`/tasks/${MAIN}`);
    await user.click(await screen.findByRole('button', { name: /改名称和备注/ }));
    const dialog = await screen.findByRole('dialog');
    expect(requiredFieldLabels(dialog)).toEqual(['任务名称']);
    const input = within(dialog).getByPlaceholderText('请输入任务名称');
    await user.clear(input);
    await user.click(within(dialog).getByRole('button', { name: '保存' }));
    await waitFor(() => expect(fieldErrors(dialog)).toEqual(['请填写任务名称']));
    await user.type(input, 'droid 50 复核');
    await user.click(within(dialog).getByRole('button', { name: '保存' }));
    expect(await screen.findByRole('heading', { name: /droid 50 复核/ })).toBeInTheDocument();
    expect(ifMatch).toMatch(/^\d+$/);
  });

  it('logs: newest first page, older lines on demand, English text as is', async () => {
    const { user } = renderApp(`/tasks/${MAIN}#logs`);
    const view = await screen.findByTestId('log-view');
    expect(await within(view).findByText(/subtask retry #1 finished \(revision 2\)/)).toBeInTheDocument();
    expect(within(view).queryByText(/task created; pre-start checks passed/)).toBeNull();
    await user.click(within(view).getByRole('button', { name: '加载更早的日志' }));
    expect(await within(view).findByText(/task created; pre-start checks passed/)).toBeInTheDocument();
  });

  it('the running task shows the header actions by state', async () => {
    const { user } = renderApp(`/tasks/${RUNNING}`);
    await screen.findByRole('heading', { name: /umi_640 全量质检/ });
    // On the detail page 「查看」 makes no sense: 暂停 becomes the primary action.
    expect(screen.getByRole('button', { name: '暂停' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '更多' }));
    expect(await screen.findByRole('menuitem', { name: '停止' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '暂停' }));
    expect(await screen.findByText('已请求暂停：在飞的 episode 跑完后停下')).toBeInTheDocument();
  });

  it('opens 编辑 of a created task in the form, and a started task refuses editing', async () => {
    renderApp('/tasks/new?edit=task_01HXR2D8');
    await waitFor(() => expect(currentLocation()).toBe(`/tasks/${MAIN}`));
    expect(await screen.findByText('任务已经开始，只能改名称和备注；要换数据、模块、模型或参数，请复制为新任务')).toBeInTheDocument();
  });

  it('the timeline says how an adjudication judged its relabels again (scope.relabel_rerun, D39)', async () => {
    renderApp('/tasks/task_01HXPZ2K');
    // The summary also counts the episodes skipped for missing source files (D40).
    expect(await screen.findByTestId('report-summary')).toHaveTextContent('缺源文件3未参与质检，不计入总数');
    const timeline = await screen.findByTestId('timeline');
    const tags = await within(timeline).findAllByTestId('relabel-rerun-tag');
    // The apply_adjudication subtask's start and end; the re-export after it has no such tag.
    expect(tags.map((t) => t.textContent)).toEqual(['改标重判：首轮完整流程', '改标重判：首轮完整流程']);
    expect(tags[0].closest('.arco-timeline-item')).toHaveTextContent('执行裁决：应用 12 条裁决');
    expect(within(timeline).getByText('重新导出：只处理变动的 episode。').closest('.arco-timeline-item')).not.toHaveTextContent('改标重判');
  });

  it('a finished task whose access key was deleted offers 重新绑定访问密钥 (rebind-credentials)', async () => {
    db.credentials = db.credentials.filter((c) => c.name !== 'readonly-tos');
    const seen: unknown[] = [];
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname.endsWith('/rebind-credentials')) seen.push(await request.clone().json());
    });
    const { user } = renderApp(`/tasks/${MAIN}`);
    const banner = await screen.findByTestId('rebind-banner');
    expect(banner).toHaveTextContent('这个任务用的访问密钥已删除');
    await user.click(within(banner).getByRole('button', { name: '重新绑定访问密钥' }));
    const dialog = await screen.findByRole('dialog', { name: '重新绑定访问密钥' });
    // Only the deleted one is asked for, and it is required.
    expect(requiredFieldLabels(dialog)).toEqual(['数据集访问密钥']);
    await user.click(within(dialog).getByRole('button', { name: '保存' }));
    await waitFor(() => expect(fieldErrors(dialog)).toEqual(['请选择数据集访问密钥']));
    await pick(user, '数据集访问密钥', 'partner-upload', dialog);
    await user.click(within(dialog).getByRole('button', { name: '保存' }));
    expect(await screen.findByText('已重新绑定')).toBeInTheDocument();
    expect(seen).toEqual([{ input_credential: 'partner-upload' }]);
    await waitFor(() => expect(screen.queryByTestId('rebind-banner')).toBeNull());
  });
});

describe('SSE and the polling fallback (03 §5, F3.2 ②)', () => {
  it('falls back to 5 s polling when EventSource is not available at all', async () => {
    const gets = countTaskGets(RUNNING);
    renderApp(`/tasks/${RUNNING}`);
    await screen.findByRole('heading', { name: /umi_640 全量质检/ });
    expect(screen.getByTestId('live-mode')).toHaveAttribute('data-mode', 'polling');
    const before = gets();
    await waitFor(() => expect(gets()).toBeGreaterThan(before + 1), { timeout: 3000 });
  });

  it('uses the stream while it is open, polls while it is down, and applies cumulative events', async () => {
    setEventSourceFactory((url) => new FakeEventSource(url) as unknown as EventSource);
    const gets = countTaskGets(RUNNING);
    renderApp(`/tasks/${RUNNING}`);
    await screen.findByRole('heading', { name: /umi_640 全量质检/ });
    const es = FakeEventSource.last();
    expect(es.url).toBe(`/events/tasks/${RUNNING}`);
    act(() => es.open());
    await waitFor(() => expect(screen.getByTestId('live-mode')).toHaveAttribute('data-mode', 'sse'));
    // While the stream is open there is no polling.
    const quiet = gets();
    await new Promise((r) => setTimeout(r, 300));
    expect(gets()).toBe(quiet);
    // A progress event updates the stage bar in place.
    act(() => es.emit('progress', { id: 'vlm', state: 'running', done: 500, total: 631, elapsed_s: 1200, eta_s: 300 }));
    await waitFor(() => expect(screen.getByTestId('stage-vlm')).toHaveTextContent('500 / 631'));
    act(() => es.emit('usage', { prompt_tokens: 9_990_000, completion_tokens: 1, reasoning_tokens: 1, cached_tokens: 1, requests: 1, requests_unknown_usage: 0 }));
    await waitFor(() => expect(screen.getByTestId('token-totals')).toHaveTextContent('9.99M'));
    // The stream drops: polling takes over until it is back.
    act(() => es.fail());
    await waitFor(() => expect(screen.getByTestId('live-mode')).toHaveAttribute('data-mode', 'polling'));
    const dropped = gets();
    await waitFor(() => expect(gets()).toBeGreaterThan(dropped), { timeout: 3000 });
    act(() => es.open());
    await waitFor(() => expect(screen.getByTestId('live-mode')).toHaveAttribute('data-mode', 'sse'));
  });

  it('under the /curation prefix the stream URL carries the prefix', async () => {
    window.__CURATOR_BASE__ = '/curation';
    try {
      setEventSourceFactory((url) => new FakeEventSource(url) as unknown as EventSource);
      renderApp(`/tasks/${RUNNING}`);
      await screen.findByRole('heading', { name: /umi_640 全量质检/ });
      expect(FakeEventSource.last().url).toBe(`/curation/events/tasks/${RUNNING}`);
    } finally {
      delete window.__CURATOR_BASE__;
    }
  });
});
