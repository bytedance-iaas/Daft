import { act, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { EVENTS_CONFIG, setEventSourceFactory } from '../../api/events';
import type { Subtask } from '../../api/types';
import { db, findTask } from '../../mocks/db';
import { server } from '../../mocks/server';
import { finishSubtask } from '../../mocks/subtaskSim';
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

  it('shows episode pipeline results and opens a completed episode immediately', async () => {
    const { user } = renderApp(`/tasks/${MAIN}`);
    const card = await screen.findByTestId('pipeline-episodes');
    expect(await within(card).findByRole('button', { name: 'ep 49' })).toBeInTheDocument();
    await user.click(within(card).getByRole('button', { name: 'ep 49' }));
    const detail = await screen.findByTestId('pipeline-episode-detail');
    expect(detail).toHaveTextContent('漏斗保留');
    expect(detail).toHaveTextContent('task_success');
  });

  it('Episode 流水线 keeps its rows while it refreshes; the spinner turns in the header, not in the body (fourth round)', async () => {
    let calls = 0;
    let release = () => {};
    const held = new Promise<void>((r) => (release = r));
    server.use(
      http.get(`*/api/v1/tasks/${RUNNING}/pipeline/episodes`, async () => {
        calls += 1;
        if (calls > 1) await held;
      }),
    );
    renderApp(`/tasks/${RUNNING}`);
    const card = await screen.findByTestId('pipeline-episodes');
    await within(card).findAllByRole('button', { name: /^ep \d+$/ });
    const rows = card.querySelectorAll('tbody tr').length;
    const sign = within(card).getByTestId('pipeline-episodes-refreshing');
    // the next poll (every 3 s) is held: the rows stay and only the header spins
    await waitFor(() => expect(sign).toHaveAttribute('data-refreshing', 'true'), { timeout: 6000 });
    expect(card.querySelectorAll('tbody tr')).toHaveLength(rows);
    release();
    await waitFor(() => expect(sign).not.toHaveAttribute('data-refreshing'));
    expect(card.querySelectorAll('tbody tr')).toHaveLength(rows);
  }, 20000);

  it('重试 shows the subtask its answer carries at once, without waiting for the next poll (07 §4.2)', async () => {
    // Every refetch of the task is held: the banner can only come from the answer of the action.
    let release = () => {};
    const held = new Promise<void>((r) => (release = r));
    let gets = 0;
    server.use(
      http.get(`*/api/v1/tasks/${MAIN}`, async () => {
        gets += 1;
        if (gets > 1) await held;
      }),
    );
    const { user } = renderApp(`/tasks/${MAIN}`);
    expect(await screen.findByRole('heading', { name: /droid 前 50 条质检/ })).toBeInTheDocument();
    expect(screen.getByTestId('state-tag')).toHaveTextContent(/^错误$/);
    await user.click(screen.getByRole('button', { name: '更多' }));
    await user.click(await screen.findByText(/^重试（2 条）$/));
    const dialog = await screen.findByRole('dialog', { name: '重试出错的条目' });
    await user.click(within(dialog).getByRole('button', { name: '开始重试' }));
    // D46: the header says 运行中 and names the retry (the second one of this task) …
    await waitFor(() => expect(screen.getByTestId('state-tag')).toHaveTextContent(/^运行中 · 重试 #2$/));
    expect(await screen.findByText('子任务「重试 #2」排队中，完成前不能再建子任务')).toBeInTheDocument();
    // … and 分档进度 is the retry's, which has not started yet.
    expect(screen.getByTestId('stages-subtask')).toHaveTextContent('子任务 · 重试 #2');
    expect(screen.getByText('子任务还没开始，开始后这里显示它的分档进度')).toBeInTheDocument();
    expect(gets).toBeGreaterThan(1);
    release();
  });

  it("a subtask's SSE events move the subtask, never the task's own state or the main run's stages (D46)", async () => {
    const t = findTask(MAIN)!;
    const running: Subtask = {
      id: 'sub_retry2',
      task_id: MAIN,
      kind: 'retry',
      scope: { modules: ['task_success'], episodes: 'errors' },
      state: 'queued',
      state_reason: null,
      progress: { stages: [{ id: 'vlm', state: 'running', done: 1, total: 2 }, ...['final', 'report', 'verify'].map((id) => ({ id, state: 'pending' as const, done: 0, total: 0 }))] },
      created_at: Date.now(),
      started_at: null,
      finished_at: null,
      result_rev: null,
    };
    db.subtasks.get(MAIN)!.push(running);
    t.active_subtask = running;
    // Once the page is up, every reload of the task is held: what it shows then comes from the
    // events alone.
    let hold = false;
    let release = () => {};
    const held = new Promise<void>((r) => (release = r));
    server.use(
      http.get(`*/api/v1/tasks/${MAIN}`, async () => {
        if (hold) await held;
      }),
      http.get(`*/api/v1/tasks/${MAIN}/subtasks`, async () => {
        if (hold) await held;
      }),
    );
    setEventSourceFactory((url) => new FakeEventSource(url) as unknown as EventSource);
    renderApp(`/tasks/${MAIN}`);
    await screen.findByRole('heading', { name: /droid 前 50 条质检/ });
    await waitFor(() => expect(screen.getByTestId('state-tag')).toHaveTextContent(/^运行中 · 重试 #2$/));
    hold = true;
    const es = FakeEventSource.last();
    act(() => es.open());
    // The snapshot on connect: the task's own state, then its subtask's.
    act(() => {
      es.emit('state', { state: 'completed_with_errors', at: 1 });
      es.emit('state', { state: 'running', subtask_id: 'sub_retry2', at: 1 });
    });
    expect(screen.getByTestId('state-tag')).toHaveTextContent(/^运行中 · 重试 #2$/);
    expect(screen.getByTestId('stages-subtask')).toHaveTextContent('子任务 · 重试 #2');
    expect(screen.getByTestId('stage-vlm')).toHaveTextContent('1 / 2');
    expect(screen.getByTestId('stage-report_generation')).toHaveTextContent('报告生成 等待中');
    // Its stages arrive as plain progress events while it runs: they are the subtask's.
    act(() => es.emit('progress', { id: 'vlm', state: 'running', done: 2, total: 2 }));
    await waitFor(() => expect(screen.getByTestId('stage-vlm')).toHaveTextContent('2 / 2'));
    // It fails: the task keeps its own state (the parent never goes back, 01 §3) and the main
    // run's stages were never touched.
    finishSubtask(MAIN, 'failed');
    act(() => {
      es.emit('state', { state: 'failed', subtask_id: 'sub_retry2', reason: '模拟的子任务失败：当前版本原样保留', at: 2 });
      es.emit('done', { state: 'failed', subtask_id: 'sub_retry2' });
    });
    await waitFor(() => expect(screen.getByTestId('state-tag')).toHaveTextContent(/^错误$/));
    expect(screen.queryByTestId('stages-subtask')).toBeNull();
    expect(screen.getByTestId('stage-vlm')).toHaveTextContent('49 / 49');
    release();
  });

  it('stage bars explain why the total dropped; module errors expand to the episodes', async () => {
    const { user } = renderApp(`/tasks/${MAIN}`);
    expect(await screen.findByTestId('stage-frame')).toHaveTextContent('数值档拦下了 1 条（ep 18，残段），所以后面的档是 49 条');
    // 终判 + 报告 read as one bar, 导出 + 交付核验 as another (requester item 11).
    expect(screen.getByTestId('stage-report_generation')).toHaveTextContent(/^报告生成 已完成2 \/ 2 · 用时 7 秒$/);
    expect(screen.getByTestId('stage-delivery')).toHaveTextContent('交付 跳过');
    expect(screen.getByTestId('stage-delivery')).toHaveTextContent('主流程结束时没有可交付的条目，没有导出');
    for (const raw of ['final', 'report', 'export', 'verify']) expect(screen.queryByTestId(`stage-${raw}`)).toBeNull();
    const table = screen.getByTestId('modules-table');
    await user.click(await within(table).findByRole('button', { name: /2 条待补跑/ }));
    const list = await screen.findByTestId('error-episodes');
    expect(list).toHaveTextContent('ep 7');
    expect(list).toHaveTextContent('ep 31');
    expect(within(table).getByText('运动学极限')).toBeInTheDocument();
    expect(within(table).getByText('预检未读到机器人型号，创建时选择跳过')).toBeInTheDocument();
  });

  it('the timeline runs horizontally, every node with its title, time and details; each result version opens its report', async () => {
    const { user } = renderApp(`/tasks/${MAIN}`);
    const tl = await screen.findByTestId('timeline');
    // One row of nodes (requester item 21), in order.
    const nodes = [...tl.querySelectorAll('.htl-track > .htl-node')];
    expect(nodes.map((n) => n.querySelector('.htl-title')?.textContent)).toEqual([
      '创建',
      '开始',
      '系统暂停',
      '自动恢复',
      '主流程结束',
      '结果版本',
      '子任务 · 重试 #1',
      '重试 #1 结束',
      '结果版本',
    ]);
    for (const n of nodes) expect(n.querySelector('.htl-time')?.textContent).toMatch(/前$/);
    // The revision nodes carry their report link, the current one its tag, on the node itself.
    const [r1, r2] = nodes.filter((n) => n.getAttribute('data-kind') === 'revision') as HTMLElement[];
    expect(within(r1).getByRole('link', { name: '查看 r0001 报告' })).toHaveAttribute('href', `/tasks/${MAIN}/report?rev=1`);
    expect(within(r1).queryByText('当前版本')).toBeNull();
    expect(within(r2).getByRole('link', { name: '查看 r0002 报告' })).toHaveAttribute('href', `/tasks/${MAIN}/report?rev=2`);
    expect(within(r2).getByText('当前版本')).toBeInTheDocument();
    // The text shows clamped on the node, in full in a popover.
    const pause = nodes[2] as HTMLElement;
    const text = pause.querySelector('.htl-text') as HTMLElement;
    expect(text).toHaveTextContent('Daemon 升级（v2.0.3 → v2.0.4），VLM 档停在 21 / 49 条');
    await user.hover(text);
    expect(await screen.findByText('Daemon 升级（v2.0.3 → v2.0.4），VLM 档停在 21 / 49 条。不是用户操作，不需要处理。', { selector: '.htl-popover' })).toBeInTheDocument();
  });

  it('分档进度 and Token 消耗 sit side by side; Token 明细 by module or by subtask with a 合计 row, and no money (D12)', async () => {
    const { user } = renderApp(`/tasks/${MAIN}`);
    const tokens = await screen.findByTestId('token-totals');
    expect(tokens).toHaveTextContent('2.01M');
    expect(tokens).not.toHaveTextContent('¥');
    const pair = screen.getByTestId('stages').closest('.grid-2') as HTMLElement;
    expect(pair).not.toBeNull();
    expect(within(pair).getByTestId('token-totals')).toBeInTheDocument();
    // 明细 is open, by module first (requester item 13).
    const table = await screen.findByTestId('token-table');
    const rows = () => [...table.querySelectorAll('tbody tr')].map((r) => [...r.querySelectorAll('td')].map((c) => c.textContent));
    await waitFor(() => expect(rows()).toHaveLength(2));
    expect([...table.querySelectorAll('thead th')].map((th) => th.textContent)).toEqual(['模块', '请求', '输入', '输出', '思维链', '缓存命中']);
    expect(rows()).toEqual([
      ['任务成败判定', '786', '1.54M', '49.5K', '32.7K', '728K'],
      ['技能画像', '100', '466K', '18K', '10.2K', '203.8K'],
    ]);
    const total = () => [...table.querySelectorAll('tfoot td, .arco-table-tfoot td')].map((c) => c.textContent);
    expect(total()).toEqual(['合计', '886', '2.01M', '67.5K', '42.9K', '931.8K']);
    await user.click(within(pair).getByText('按子任务'));
    await waitFor(() => expect(rows().map((r) => r[0])).toEqual(['主流程', '重试 #1']));
    expect([...table.querySelectorAll('thead th')][0]).toHaveTextContent('主流程 / 子任务');
    expect(total()).toEqual(['合计', '886', '2.01M', '67.5K', '42.9K', '931.8K']);
    // The help text is inside the fold, which closes.
    const fold = table.closest('.arco-collapse-item') as HTMLElement;
    expect(within(fold).getByText(/^重试花掉的也算在合计里/)).toBeInTheDocument();
    await user.click(within(fold).getByText('明细'));
    await waitFor(() => expect(fold).not.toHaveClass('arco-collapse-item-active'));
  });

  it('the header has 查看报告 first, then 人工裁决（N）, both blue while items are pending (D47, third round)', async () => {
    const { user } = renderApp(`/tasks/${MAIN}`);
    const adj = await screen.findByTestId('header-adjudicate');
    expect(adj).toHaveTextContent(/^人工裁决（10）$/);
    expect(adj).toHaveClass('arco-btn-primary');
    const report = screen.getByRole('button', { name: '查看报告' });
    expect(report).toHaveClass('arco-btn-primary');
    expect(report.compareDocumentPosition(adj) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Not repeated under 更多.
    await user.click(screen.getByRole('button', { name: '更多' }));
    const copy = await screen.findByRole('menuitem', { name: '复制为新任务' });
    expect(within(copy.closest('.arco-dropdown-menu') as HTMLElement).queryByRole('menuitem', { name: /人工裁决/ })).toBeNull();
    await user.click(adj);
    await waitFor(() => expect(currentLocation()).toBe(`/tasks/${MAIN}/adjudication`));
  });

  it('人工裁决 stands without pending items; with only appealable rejects left it leads to 被拒复议 (D47)', async () => {
    findTask(MAIN)!.pending_adjudication = 0;
    // The review queue is empty: everything left is an appeal.
    server.use(
      http.get(`*/api/v1/tasks/${MAIN}/adjudication`, ({ request }) => {
        if (new URL(request.url).searchParams.get('tab') === 'appeals') return undefined;
        return HttpResponse.json({ items: [], next_cursor: null, has_more: false, counts: { decided: 0, pending: 0, unapplied: 0 } });
      }),
    );
    const { user } = renderApp(`/tasks/${MAIN}`);
    const adj = await screen.findByTestId('header-adjudicate');
    expect(adj).toHaveTextContent(/^人工裁决$/);
    expect(adj).not.toHaveClass('arco-btn-primary');
    expect(screen.getByRole('button', { name: '查看报告' })).toHaveClass('arco-btn-primary');
    await user.click(adj);
    // The adjudication page loads its route and queries first: slower CI runners need more than 1 s.
    const hint = await screen.findByTestId('review-empty-appeals', {}, { timeout: 5000 });
    expect(hint).toHaveTextContent('被拒的条目在「被拒复议」里，觉得判错了可以复议。');
    await user.click(within(hint).getByRole('button', { name: '去被拒复议' }));
    await waitFor(() => expect(currentLocation()).toBe(`/tasks/${MAIN}/adjudication?tab=appeals`));
    expect(await within(await screen.findByTestId('appeals', {}, { timeout: 5000 })).findByTestId('card-44', {}, { timeout: 5000 })).toBeInTheDocument();
  });

  it('a task without a result has no 人工裁决 in its header', async () => {
    renderApp(`/tasks/${RUNNING}`);
    await screen.findByRole('heading', { name: /umi_640 全量质检/ });
    expect(screen.queryByTestId('header-adjudicate')).toBeNull();
    expect(screen.getByRole('button', { name: '暂停' })).toHaveClass('arco-btn-primary');
  });

  it('no second report link and no gray notes next to the card titles (requester items 10, 21)', async () => {
    renderApp(`/tasks/${MAIN}`);
    await screen.findByTestId('report-summary');
    expect(screen.queryByText('查看详细报告')).toBeNull();
    for (const note of ['只统计用量，不换算金额', '主流程、子任务、系统暂停都记在这里', '个模块参与本次质检', '任务配置与执行计划，只读', 'planner 生成，只读', '启动后只能改名称和备注']) {
      expect(document.body).not.toHaveTextContent(note);
    }
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
    // 分档进度 gives counts and time used, never an estimate (requester item 20).
    expect(screen.getByTestId('stage-autolabel')).toHaveTextContent('640 / 640 · 用时 10 分 12 秒');
    expect(screen.getByTestId('stages')).not.toHaveTextContent('预计');
    // The streaming layers are drawn as the pipeline activity: in flight, queued, done.
    expect(screen.getByTestId('stage-vlm')).toHaveTextContent('410 / 631');
    expect(screen.getByTestId('inflight-vlm')).toHaveTextContent('32');
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
    expect(tags[0].closest('.htl-node')).toHaveTextContent('执行裁决：应用 12 条裁决');
    expect(tags[0].closest('.htl-node')).toHaveTextContent('子任务 · 执行裁决 #1');
    expect(within(timeline).getByText('重新导出：只处理变动的 episode。').closest('.htl-node')).not.toHaveTextContent('改标重判');
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
  it('shows actual inflight counts, new batch arrivals and per-episode timings from SSE', async () => {
    setEventSourceFactory((url) => new FakeEventSource(url) as unknown as EventSource);
    renderApp(`/tasks/${RUNNING}`);
    await screen.findByRole('heading', { name: /umi_640 全量质检/ });
    const es = FakeEventSource.last();
    act(() => es.open());
    const at = Date.now();
    const pipeline = { inflight: 3, queued: 2, capacity: 8, dispatches: 4,
      recent: [{ number: 4, count: 1, episodes: [6], at }],
      started_at: at - 10000, finished_at: null, updated_at: at,
      processing: { count: 2, total_s: 8, mean_s: 4, min_s: 3, max_s: 5 } };
    act(() => es.emit('progress', { id: 'vlm', state: 'running', done: 2, total: 7, pipeline }));
    expect(await screen.findByTestId('pipeline-activity')).toBeInTheDocument();
    expect(screen.getByTestId('inflight-vlm')).toHaveTextContent('3条在途');
    expect(screen.getByTestId('stage-vlm')).toHaveTextContent('2 条待进入');
    expect(screen.getByTestId('stage-vlm')).toHaveTextContent('平均每条 4.00 s');
    expect(screen.getByTestId('arrival-vlm')).toHaveTextContent('第 4 批');
    expect(screen.getByTestId('arrival-vlm')).toHaveTextContent('ep 6');
    expect(screen.getByTestId('pipeline-overlap')).toBeInTheDocument();
    act(() => es.emit('progress', { id: 'vlm', state: 'running', done: 3, total: 7,
      pipeline: { ...pipeline, inflight: 4, queued: 0, dispatches: 5,
        recent: [...pipeline.recent, { number: 5, count: 2, episodes: [7, 8], at: at + 1 }] } }));
    await waitFor(() => expect(screen.getByTestId('inflight-vlm')).toHaveTextContent('4条在途'));
    expect(screen.getByTestId('arrival-vlm')).toHaveTextContent('第 5 批');
    expect(screen.getByTestId('arrival-vlm')).toHaveTextContent('新进入 2 条');
  });

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
