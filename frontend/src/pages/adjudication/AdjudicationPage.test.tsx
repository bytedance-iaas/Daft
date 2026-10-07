import { screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { db, decisionsOf, findTask } from '../../mocks/db';
import { eefRecord } from '../../mocks/eef';
import { MAIN_TASK } from '../../mocks/world';
import { pick } from '../../test/arco';
import { recordRequests, type SeenRequest } from '../../test/record';
import { renderApp } from '../../test/render';
import { ADJ_CONFIG } from './useAdjudication';

const PAGE = `/tasks/${MAIN_TASK}/adjudication`;
const card = (ep: number) => screen.getByTestId(`card-${ep}`);
const posts = (seen: SeenRequest[]) => seen.filter((r) => r.method === 'POST' && r.path === `/tasks/${MAIN_TASK}/adjudication`).map((r) => (r.body as { decisions: unknown[] }).decisions[0]);
const EEF = 'eef_video_consistency';

const shownCards = (root: ParentNode = document) => [...root.querySelectorAll('[data-testid^="card-"]')].map((e) => Number(e.getAttribute('data-testid')!.slice(5)));

afterEach(() => {
  ADJ_CONFIG.pageSize = 200;
  ADJ_CONFIG.cardsPerPage = 10;
});

describe('人工裁决 (07 §6, F3.3)', () => {
  it('one card per episode with its source modules; counts of the operation on top', async () => {
    renderApp(PAGE);
    await screen.findByTestId('card-29');
    expect(screen.getByTestId('adj-counts')).toHaveTextContent('已裁 2 条 · 待裁 8 条 · 3 条尚未应用');
    // Default filter: pending (拿不准 included).
    expect(shownCards()).toEqual([4, 16, 22, 29, 33, 36, 40, 47]);
    expect(card(29)).toHaveTextContent('来源：任务成败判定');
    // One card, one question: the suspected label conflict is part of the task verdict.
    const q29 = within(card(29)).getByTestId('q-29-task_verdict');
    expect(q29).toHaveTextContent('① 来源：任务成败判定 · 任务成败');
    expect(q29).toHaveTextContent('原始标注');
    expect(q29).toHaveTextContent('画面描述');
    expect(within(card(29)).queryByTestId('q-29-label')).toBeNull();
    expect(within(card(16)).getByTestId('status-16')).toHaveTextContent('拿不准');
    expect(screen.queryByText(/裁决只属于这个任务/)).toBeNull();          // no page note (fourth round)
  });

  it('every click is saved; a verdict carries the task text the person rewrote', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-29');
    // The model suspects this text, so the rewrite box is open with its description filled in.
    const box = within(card(29)).getByLabelText('改写后的任务描述（选填）');
    expect(box).toHaveValue('pour rice into the green bowl');
    expect(within(card(29)).getByTestId('ask-29')).toHaveTextContent('按改写后的描述「pour rice into the green bowl」，这条做成了吗？');
    await user.click(within(card(29)).getByText('判失败'));
    await waitFor(() => expect(posts(seen)).toEqual([{ episode_index: 29, line: 'task_verdict', decision: 'failure', new_label: 'pour rice into the green bowl' }]));
    expect(await screen.findByTestId('adj-counts')).toHaveTextContent('已裁 3 条 · 待裁 7 条 · 4 条尚未应用');
    // Still on screen with the default filter: cards never jump away after a click.
    expect(within(card(29)).getByTestId('status-29')).toHaveTextContent('已裁');
    // A verdict without a rewrite sends no new_label.
    await user.click(within(card(33)).getByText('判成功'));
    await waitFor(() => expect(posts(seen)).toHaveLength(2));
    expect(posts(seen)[1]).toEqual({ episode_index: 33, line: 'task_verdict', decision: 'success' });
  });

  it('rule 1: 整条弃用 overrides the verdict (buttons disabled, reason given) and can be withdrawn', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-29');
    await user.click(within(card(29)).getByRole('button', { name: '其它原因，整条弃用' }));
    await waitFor(() => expect(posts(seen)).toEqual([{ episode_index: 29, line: 'task_verdict', decision: 'discard' }]));
    const verdict = within(card(29)).getByTestId('q-29-task_verdict');
    expect(within(verdict).getByText('这一条已整条弃用，不再判成败（「这条不要了」和「判它成功」互相矛盾）。')).toBeInTheDocument();
    for (const r of within(verdict).getAllByRole('radio')) expect(r).toBeDisabled();
    expect(within(card(29)).getByTestId('status-29')).toHaveTextContent('已裁');
    await user.click(within(card(29)).getByRole('button', { name: '撤销整条弃用' }));
    await waitFor(() => expect(posts(seen)[1]).toEqual({ episode_index: 29, line: 'task_verdict', decision: 'unsure' }));
    for (const r of within(within(card(29)).getByTestId('q-29-task_verdict')).getAllByRole('radio')) expect(r).not.toBeDisabled();
  });

  it('rule 3: 拿不准 is recorded, the card stays pending (orange) and is not executed', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-33');
    await user.click(within(card(33)).getByText('拿不准'));
    await waitFor(() => expect(posts(seen)).toEqual([{ episode_index: 33, line: 'task_verdict', decision: 'unsure' }]));
    expect(within(card(33)).getByTestId('status-33')).toHaveTextContent('拿不准');
    expect(card(33)).toHaveClass('unsure');
    expect(within(card(33)).getByText('拿不准也是答案：只记一笔，条目留在队列里，仍算待裁，执行时不动它。')).toBeInTheDocument();
    expect(screen.getByTestId('adj-counts')).toHaveTextContent('待裁 8 条 · 3 条尚未应用');
  });

  it('rewriting the task text rides on the verdict: 拿不准 leaves it to the model, a verdict stands', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-36');
    const box = within(card(36)).getByLabelText('改写后的任务描述（选填）');
    await user.clear(box);
    await user.type(box, 'push the plate back');
    await user.click(within(card(36)).getByText('拿不准'));
    await waitFor(() => expect(posts(seen)).toEqual([{ episode_index: 36, line: 'task_verdict', decision: 'unsure', new_label: 'push the plate back' }]));
    // Rewritten, the verdict left open: the episode stays in the queue and is judged again.
    expect(within(card(36)).getByTestId('status-36')).toHaveTextContent('拿不准');
    await waitFor(() => expect(screen.getByTestId('adj-counts')).toHaveTextContent('待裁 8 条 · 4 条尚未应用'));
    // The same text with a verdict: the person's conclusion stands.
    await user.click(within(card(36)).getByText('判成功'));
    await waitFor(() => expect(posts(seen)).toHaveLength(2));
    expect(posts(seen)[1]).toEqual({ episode_index: 36, line: 'task_verdict', decision: 'success', new_label: 'push the plate back' });
    expect(within(card(36)).getByTestId('status-36')).toHaveTextContent('已裁');
    // Clicking the same answer again with the same text sends nothing.
    await user.click(within(card(36)).getByText('判成功'));
    expect(posts(seen)).toHaveLength(2);
  });

  it('a card the model does not suspect asks the verdict alone, with the rewrite box folded away', async () => {
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-33');
    expect(within(card(33)).queryByLabelText('改写后的任务描述（选填）')).toBeNull();
    expect(within(card(33)).getByTestId('q-33-task_verdict')).not.toHaveTextContent('画面描述');
    await user.click(within(card(33)).getByRole('button', { name: '改写任务描述' }));
    expect(within(card(33)).getByLabelText('改写后的任务描述（选填）')).toHaveValue('');
  });

  it('filters: ?source= from the report preselects on the server; several sources filter on the page', async () => {
    const seen = recordRequests();
    const { user } = renderApp(`${PAGE}?source=task_success`);
    await screen.findByTestId('card-16');
    expect(shownCards()).toEqual([4, 16, 22, 29, 33, 36, 40, 47]);
    const list = seen.filter((r) => r.method === 'GET' && r.path === `/tasks/${MAIN_TASK}/adjudication`);
    expect(list.at(-1)?.query.get('source')).toBe('task_success');
    await pick(user, '问题类型', /^任务成败/);
    await waitFor(() => expect(shownCards()).toEqual([4, 16, 22, 29, 33, 36, 40, 47]));
    await pick(user, '状态', '拿不准');
    await waitFor(() => expect(shownCards()).toEqual([4, 16]));
    const last = seen.filter((r) => r.method === 'GET' && r.path === `/tasks/${MAIN_TASK}/adjudication`).at(-1)!;
    expect(last.query.get('status')).toBe('pending');          // 拿不准 is filtered on the page
  });

  it('each tab filters by its own source modules; another tab starts the filter over (third round)', async () => {
    const { user } = renderApp(`${PAGE}?source=task_success`);
    await screen.findByTestId('card-16');
    expect(shownCards()).toEqual([4, 16, 22, 29, 33, 36, 40, 47]);
    await user.click(screen.getByRole('tab', { name: '被拒复议' }));
    const list = await screen.findByTestId('appeals');
    await within(list).findByTestId('card-6');
    expect(shownCards(list)).toEqual([6, 11, 23, 38, 45, 44]);
    // One filter on the page, the appeals tab's: the appealable modules only.
    expect(screen.getAllByRole('combobox', { name: '来源模块' })).toHaveLength(1);
    await user.click(screen.getByRole('combobox', { name: '来源模块' }));
    const names = await waitFor(() => {
      const o = screen.getAllByRole('option').map((x) => x.textContent?.trim());
      if (!o.length) throw new Error('no options');
      return o;
    });
    expect(names).toEqual(['EEF–视频一致性', '任务成败判定', '精确去重']);
    expect(screen.queryByText(/这里列出可复议模块拒掉的条目/)).toBeNull();
  });

  it('an empty appeals tab says the same as an empty review tab (fourth round)', async () => {
    const { user } = renderApp(`${PAGE}?tab=appeals&source=eef_video_consistency`);
    expect(await screen.findByTestId('appeals-empty', {}, { timeout: 5000 })).toHaveTextContent(/^没有符合筛选条件的条目$/);
    await user.click(screen.getByRole('tab', { name: '待裁决' }));
    await screen.findByTestId('card-29');
  });

  it('the whole queue is fetched with cursors in the background', async () => {
    ADJ_CONFIG.pageSize = 3;
    const seen = recordRequests();
    renderApp(PAGE);
    expect(await screen.findByText('已加载全部 8 条')).toBeInTheDocument();
    const calls = seen.filter((r) => r.method === 'GET' && r.path === `/tasks/${MAIN_TASK}/adjudication`);
    expect(calls.map((c) => Boolean(c.query.get('cursor')))).toEqual([false, true, true]);
    expect(shownCards()).toHaveLength(8);
    expect(screen.queryByTestId('review-pager')).toBeNull();
  });

  it('cards come ten a page with a pager (third round); a filter goes back to page 1', async () => {
    ADJ_CONFIG.cardsPerPage = 3;
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-4');
    expect(shownCards()).toEqual([4, 16, 22]);
    const pager = screen.getByTestId('review-pager');
    expect(pager).toHaveTextContent('共 8 条');
    await user.click(within(pager).getByText('3', { selector: '.arco-pagination-item' }));
    expect(shownCards()).toEqual([40, 47]);
    await user.click(within(pager).getByText('2', { selector: '.arco-pagination-item' }));
    expect(shownCards()).toEqual([29, 33, 36]);
    await pick(user, '状态', '拿不准');
    await waitFor(() => expect(shownCards()).toEqual([4, 16]));
    expect(screen.queryByTestId('review-pager')).toBeNull();
    await pick(user, '状态', '待裁（含拿不准）');
    await waitFor(() => expect(shownCards()).toEqual([4, 16, 22]));
    expect(screen.getByTestId('review-pager')).toBeInTheDocument();
  });

  it('rule 2: 被拒复议 lists rejects of appealable modules as optional cards and explains the final ones (D42)', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-29');
    await user.click(screen.getByRole('tab', { name: '被拒复议' }));
    const list = await screen.findByTestId('appeals');
    await within(list).findByTestId('card-6');
    // task_success rejects and the dedup reject (D42).
    expect(shownCards(list)).toEqual([6, 11, 23, 38, 45, 44]);
    // Appeals are optional: the cards say so and the pending count does not move.
    expect(within(card(6)).getByTestId('status-6')).toHaveTextContent('可复议');
    await user.click(screen.getByText('为什么有的被拒条目不在这里'));
    const final = await screen.findByTestId('final-rejects');
    expect(final).toHaveTextContent('时间戳检查：1 条');
    // dedup is appealable now: not among the final ones.
    expect(final).not.toHaveTextContent('精确去重');
    expect(final).not.toHaveTextContent('任务成败判定');
    expect(screen.getByText(/物理与结构硬门.*和软分拒绝是终局/)).toHaveTextContent('可复议的只有：EEF–视频一致性、任务成败判定、精确去重');
    // Buttons come from the catalog: 恢复为可用 / 维持拒绝 / 拿不准, no 整条弃用.
    const q = within(card(6)).getByTestId('q-6-reject_appeal');
    expect(within(q).getAllByRole('radio').map((r) => r.closest('label')?.textContent)).toEqual(['恢复为可用', '维持拒绝', '拿不准']);
    expect(within(card(6)).queryByRole('button', { name: '其它原因，整条弃用' })).toBeNull();
    await user.click(within(q).getByText('恢复为可用'));
    await waitFor(() => expect(posts(seen)).toEqual([{ episode_index: 6, line: 'reject_appeal', decision: 'restore' }]));
    expect(screen.getByTestId('adj-counts')).toHaveTextContent('待裁 8 条');
  });

  it('a dedup appeal names the episode it duplicates and shows both side by side (D42)', async () => {
    const seen = recordRequests();
    const { user } = renderApp(`${PAGE}?tab=appeals&source=dedup`);
    const c = await screen.findByTestId('card-44');
    // ?source=dedup filters on the server: only the dedup card.
    expect(shownCards(screen.getByTestId('appeals'))).toEqual([44]);
    const lists = seen.filter((r) => r.method === 'GET' && r.path === `/tasks/${MAIN_TASK}/adjudication`);
    expect([lists.at(-1)?.query.get('tab'), lists.at(-1)?.query.get('source')]).toEqual(['appeals', 'dedup']);
    expect(c).toHaveTextContent('来源：精确去重');
    expect(within(c).getByTestId('duplicate-44')).toHaveTextContent('与 episode 43 字节级完全重复');
    const compare = within(c).getByTestId('compare-44');
    expect(compare).toHaveTextContent('ep 44（本条，被判重复）');
    expect(compare).toHaveTextContent('ep 43（保留的那条）');
    // Both episodes' videos: the rejected one from the source, the kept one from the delivery.
    expect(await within(compare).findByTestId('media-44')).toBeInTheDocument();
    expect(await within(compare).findByTestId('media-43')).toBeInTheDocument();
    const q = within(c).getByTestId('q-44-reject_appeal');
    await user.click(within(q).getByText('维持拒绝'));
    await waitFor(() => expect(posts(seen)).toEqual([{ episode_index: 44, line: 'reject_appeal', decision: 'keep_rejected' }]));
    expect(within(c).getByTestId('status-44')).toHaveTextContent('已裁');
  });

  it('a line without a dedicated view renders from the registry catalog, and counts as pending as the catalog says (D43)', async () => {
    // Test fixture only: a catalog entry and a question on it; the mock world has neither.
    db.extraReviewLines = [
      {
        id: 'grip_check',
        review_kind: 'grip_check',
        title_zh: '夹爪状态核对',
        applies_to: 'passed',
        counts_as_pending: true,
        decisions: [
          { const: 'grip_ok', title: '夹爪正常' },
          { const: 'grip_broken', title: '夹爪异常' },
          { const: 'unsure', title: '拿不准' },
        ],
      },
    ];
    db.extraQuestions = new Map([
      [
        MAIN_TASK,
        new Map([
          [12, [{ line: 'grip_check', source_module: 'motion_quality', reason: '夹爪开度读数 3 秒不变，画面里夹爪在动', latest_decision: null }]],
          // A line the catalog does not declare: shown, but no buttons to press.
          [14, [{ line: 'mystery_line', source_module: 'motion_quality', reason: '未知问题', latest_decision: null }]],
        ]),
      ],
    ]);
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    const c = await screen.findByTestId('card-12');
    // Two more pending cards: grip_check counts as pending, and the undeclared line too.
    expect(screen.getByTestId('adj-counts')).toHaveTextContent('待裁 10 条');
    const q = within(c).getByTestId('q-12-grip_check');
    expect(q).toHaveTextContent('① 来源：运动质量 · 夹爪状态核对');
    expect(q).toHaveTextContent('夹爪开度读数 3 秒不变，画面里夹爪在动');
    expect(within(q).getAllByRole('radio').map((r) => r.closest('label')?.textContent)).toEqual(['夹爪正常', '夹爪异常', '拿不准']);
    // Not a line that offers 整条弃用.
    expect(within(c).queryByRole('button', { name: '其它原因，整条弃用' })).toBeNull();
    await user.click(within(q).getByText('夹爪异常'));
    await waitFor(() => expect(posts(seen)).toEqual([{ episode_index: 12, line: 'grip_check', decision: 'grip_broken' }]));
    await waitFor(() => expect(screen.getByTestId('adj-counts')).toHaveTextContent('已裁 3 条 · 待裁 9 条'));
    expect(within(c).getByTestId('status-12')).toHaveTextContent('已裁');
    // The question type filter lists the catalog's lines for this tab.
    await pick(user, '问题类型', /^夹爪状态核对/);
    await waitFor(() => expect(shownCards()).toEqual([12]));
    const unknown = screen.queryByTestId('q-14-mystery_line');
    expect(unknown).toBeNull();
    await pick(user, '问题类型', '全部');
    expect(await screen.findByTestId('q-14-mystery_line')).toHaveTextContent('注册表里没有「mystery_line」这种复核，这一问没法作答');
    expect(within(screen.getByTestId('q-14-mystery_line')).queryByRole('radio')).toBeNull();
  });

  it('an EEF question shows why, the CPU readings and every window with its marked crops; 一致 answers eef_check (C1 1.9, F5.11)', async () => {
    // Test fixture only: the mock world's tasks do not select the EEF module.
    const why = '「位置」（相机 ext）CPU 判为可疑，模型多数认为一致（支持 2、反对 0）';
    db.extraQuestions = new Map([[MAIN_TASK, new Map([[12, [{ line: 'eef_check', source_module: EEF, reason: why, latest_decision: null }]]])]]);
    db.extraRecords = new Map([[MAIN_TASK, new Map([[12, { [EEF]: eefRecord(12, why) }]])]]);
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    const c = await screen.findByTestId('card-12');
    expect(screen.getByTestId('adj-counts')).toHaveTextContent('待裁 9 条');
    const q = within(c).getByTestId('q-12-eef_check');
    expect(q).toHaveTextContent('① 来源：EEF–视频一致性 · EEF 与画面核对');
    expect(q).toHaveTextContent(`为什么转人工：${why}`);
    const ev = await within(q).findByTestId('eef-evidence-12');
    const cpu = within(ev).getByTestId('eef-cpu');
    expect(cpu).toHaveTextContent('可疑');
    expect(cpu).toHaveTextContent('状态运动（整条）：正常');
    const cand = within(ev).getByTestId('eef-window-ext-0');
    expect(cand).toHaveTextContent('候选段 · 位置');
    expect(cand).toHaveTextContent('帧 121–151（3 帧）');                  // frames counted from 1
    expect(cand).toHaveTextContent('点 block_center · 方向 gripper_x');
    expect(cand).toHaveTextContent('位置：支持');
    expect(cand).toHaveTextContent('与 CPU 冲突：位置 CPU 可疑，模型支持');
    expect(cand).toHaveTextContent('「红圈压在夹爪指尖中间」');
    await waitFor(() => expect(within(cand).getAllByAltText(/ext · .*_frame_0001[2-5]0\.jpg/)).toHaveLength(2));
    expect(within(ev).getByTestId('eef-window-ext-1')).toHaveTextContent('模型超时');
    // The window crops are shown once, next to their answer; the CPU's own frame separately.
    expect(within(ev).getByText('CPU 证据帧')).toBeInTheDocument();
    expect(within(within(c).getByTestId('media-12')).queryAllByAltText(/EEF/)).toHaveLength(0);
    // Last, the trajectory against the dataset's own record (design doc 12 §8.7): it only informs.
    const rec = within(ev).getByTestId('eef-record');
    expect(rec).toHaveTextContent('只报告，不参与判过 / 判废');
    expect(within(rec).getByTestId('eef-record-joints')).toHaveTextContent('恒定差与映射声明的关系不符；有随时间变化的差');
    await waitFor(() => expect(within(ev).getAllByAltText(/ext_frame_000148/)).toHaveLength(1));
    expect(within(q).getAllByRole('radio').map((r) => r.closest('label')?.textContent)).toEqual(['一致，判过', '不一致，判废', '拿不准']);
    expect(within(c).queryByRole('button', { name: '其它原因，整条弃用' })).toBeNull();
    await user.click(within(q).getByText('一致，判过'));
    await waitFor(() => expect(posts(seen)).toEqual([{ episode_index: 12, line: 'eef_check', decision: 'consistent' }]));
    expect(within(c).getByTestId('status-12')).toHaveTextContent('已裁');
  });

  it('执行裁决 confirms what will be applied and which texts are judged again, then builds the subtask once (D39)', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-29');
    // ep 29: the suggested text taken as it is, the verdict left to the model.
    await user.click(within(card(29)).getByText('拿不准'));
    await waitFor(() => expect(screen.getByTestId('adj-counts')).toHaveTextContent('4 条尚未应用'));
    await user.click(within(card(40)).getByText('判成功'));
    await waitFor(() => expect(screen.getByTestId('adj-counts')).toHaveTextContent('5 条尚未应用'));
    await user.click(screen.getByRole('button', { name: '执行裁决' }));
    const dialog = await screen.findByRole('dialog', { name: '执行裁决' });
    // Counted over every unapplied card, not only the loaded ones: ep 4, 13, 9 were decided earlier.
    const summary = await within(dialog).findByTestId('apply-summary');
    expect(summary).toHaveTextContent('本次应用 5 条裁决（5 条 episode）。');
    expect(summary).toHaveTextContent('其中 2 条改了标，要按新标注重判任务成败。');
    expect(summary).toHaveTextContent('人已判了成功或失败的改标条目不重判。');
    const items = within(dialog).getByTestId('apply-list');
    expect(items).toHaveTextContent('ep 29：拿不准，改写为「pour rice into the green bowl」 → 按新描述重跑任务成败判定');
    expect(items).toHaveTextContent('ep 4：拿不准，改写为「fold the cloth and put it on the table」 → 按新描述重跑任务成败判定');
    expect(items).toHaveTextContent('ep 40：判成功 → 不跑模型，人说了算');
    // The protocol: v1's two layers by default, the first run's full flow on request.
    const choice = within(dialog).getByTestId('relabel-rerun');
    expect(within(choice).getByRole('radio', { name: /与旧版一致（默认）/ })).toBeChecked();
    expect(choice).toHaveTextContent('只跑多视角打分和逐机位复核两层');
    expect(choice).toHaveTextContent('同样的裁决可能得出和旧版不同的结论');
    await user.click(within(dialog).getByRole('button', { name: '执行' }));
    expect(await screen.findByText('已创建执行裁决的子任务')).toBeInTheDocument();
    const applies = seen.filter((r) => r.method === 'POST' && r.path === `/tasks/${MAIN_TASK}/adjudication/apply`);
    expect(applies).toHaveLength(1);
    expect(applies[0].body).toEqual({ relabel_rerun: 'v1' });
    expect(applies[0].headers['idempotency-key']).toBeTruthy();
    expect(findTask(MAIN_TASK)!.active_subtask?.scope.relabel_rerun).toBe('v1');
    await waitFor(() => expect(screen.getByTestId('adj-counts')).toHaveTextContent('0 条尚未应用'));
    expect(screen.getByRole('button', { name: '执行裁决' })).toBeDisabled();
  });

  it('按首轮的完整流程重判 sends relabel_rerun: full; a text the person judged with is not re-judged', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-29');
    await user.click(within(card(29)).getByText('判失败'));
    await waitFor(() => expect(posts(seen)).toHaveLength(1));
    await user.click(screen.getByRole('button', { name: '执行裁决' }));
    const dialog = await screen.findByRole('dialog', { name: '执行裁决' });
    const summary = await within(dialog).findByTestId('apply-summary');
    expect(summary).toHaveTextContent('本次应用 4 条裁决（4 条 episode）。');
    expect(summary).toHaveTextContent('其中 1 条改了标，要按新标注重判任务成败。');
    expect(summary).toHaveTextContent('人已判了成功或失败的改标条目不重判（本次 1 条）。');
    expect(within(dialog).getByTestId('apply-list')).toHaveTextContent('ep 29：判失败，改写为「pour rice into the green bowl」 → 不跑模型，人说了算');
    await user.click(within(dialog).getByText('按首轮的完整流程重判'));
    await user.click(within(dialog).getByRole('button', { name: '执行' }));
    expect(await screen.findByText('已创建执行裁决的子任务')).toBeInTheDocument();
    const apply = seen.find((r) => r.method === 'POST' && r.path === `/tasks/${MAIN_TASK}/adjudication/apply`);
    expect(apply?.body).toEqual({ relabel_rerun: 'full' });
    expect(findTask(MAIN_TASK)!.active_subtask?.scope.relabel_rerun).toBe('full');
  });

  it('an answer recorded earlier comes back in the rewrite box and counts the same after a reload', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-4');
    // ep 4 was answered before this visit: 拿不准 with a rewritten text (seedDecisions).
    const c4 = card(4);
    expect(within(c4).getByLabelText('改写后的任务描述（选填）')).toHaveValue('fold the cloth and put it on the table');
    expect(within(c4).getByRole('radio', { name: '拿不准' })).toBeChecked();
    expect(within(c4).getByTestId('status-4')).toHaveTextContent('拿不准');
    expect(c4).toHaveTextContent('galbot');
    // Judging it now keeps the text and stops the re-judge.
    await user.click(within(c4).getByText('判成功'));
    await waitFor(() => expect(posts(seen)).toEqual([{ episode_index: 4, line: 'task_verdict', decision: 'success', new_label: 'fold the cloth and put it on the table' }]));
    await user.click(screen.getByRole('button', { name: '执行裁决' }));
    const dialog = await screen.findByRole('dialog', { name: '执行裁决' });
    const summary = await within(dialog).findByTestId('apply-summary');
    expect(summary).toHaveTextContent('本次应用 3 条裁决（3 条 episode）。');
    expect(summary).toHaveTextContent('人已判了成功或失败的改标条目不重判（本次 1 条）。');
    expect(summary).not.toHaveTextContent('改了标，要按新标注重判');
    expect(within(dialog).getByTestId('apply-list')).toHaveTextContent('ep 4：判成功，改写为「fold the cloth and put it on the table」 → 不跑模型，人说了算');
    await user.click(within(dialog).getByRole('button', { name: '执行' }));
    expect(await screen.findByText('已创建执行裁决的子任务')).toBeInTheDocument();
    expect(decisionsOf(MAIN_TASK).filter((d) => d.episode_index === 4).map((d) => [d.decision, d.new_label, d.applied])).toEqual([
      ['unsure', 'fold the cloth and put it on the table', false],
      ['success', 'fold the cloth and put it on the table', true],
    ]);
  });

  it('without relabels to judge again there is no protocol to pick; the body still says v1', async () => {
    // The earlier relabel (ep 4) was applied already.
    for (const d of decisionsOf(MAIN_TASK)) if (d.episode_index === 4) d.applied = true;
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-29');
    await waitFor(() => expect(screen.getByTestId('adj-counts')).toHaveTextContent('2 条尚未应用'));
    await user.click(screen.getByRole('button', { name: '执行裁决' }));
    const dialog = await screen.findByRole('dialog', { name: '执行裁决' });
    const summary = await within(dialog).findByTestId('apply-summary');
    expect(summary).toHaveTextContent('本次应用 2 条裁决（2 条 episode）。');
    expect(summary).not.toHaveTextContent('改了标');
    expect(within(dialog).queryByTestId('relabel-rerun')).toBeNull();
    await user.click(within(dialog).getByRole('button', { name: '执行' }));
    expect(await screen.findByText('已创建执行裁决的子任务')).toBeInTheDocument();
    expect(seen.find((r) => r.method === 'POST' && r.path.endsWith('/adjudication/apply'))?.body).toEqual({ relabel_rerun: 'v1' });
  });

  it('the card opens the mini player of its episode, and reads no camera before (design doc 18 §4.6)', async () => {
    HTMLMediaElement.prototype.canPlayType = (type: string) => (/avc1|av01/.test(type) ? 'probably' : '');
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-29');
    const c = card(29);
    expect(seen.filter((r) => r.path.includes('/viz'))).toHaveLength(0);
    await user.click(await within(c).findByTestId('open-mini-29'));
    const mini = await screen.findByTestId('vz-mini');
    await waitFor(() => expect(seen.some((r) => r.path.endsWith('/episodes/29/viz'))).toBe(true));
    await waitFor(() => expect(mini.querySelectorAll('video[data-testid^="vz-video-"]').length).toBeGreaterThan(0));
    expect(within(mini).getByText('ep 29')).toBeInTheDocument();
  });
});
