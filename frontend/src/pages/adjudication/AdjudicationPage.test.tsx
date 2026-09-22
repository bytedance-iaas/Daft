import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { decisionsOf, findTask } from '../../mocks/db';
import { MAIN_TASK } from '../../mocks/world';
import { pick } from '../../test/arco';
import { fieldErrors } from '../../test/forms';
import { recordRequests, type SeenRequest } from '../../test/record';
import { renderApp } from '../../test/render';
import { ADJ_CONFIG } from './useAdjudication';

const PAGE = `/tasks/${MAIN_TASK}/adjudication`;
const card = (ep: number) => screen.getByTestId(`card-${ep}`);
const posts = (seen: SeenRequest[]) => seen.filter((r) => r.method === 'POST' && r.path === `/tasks/${MAIN_TASK}/adjudication`).map((r) => (r.body as { decisions: unknown[] }).decisions[0]);
const shownCards = (root: ParentNode = document) => [...root.querySelectorAll('[data-testid^="card-"]')].map((e) => Number(e.getAttribute('data-testid')!.slice(5)));

afterEach(() => {
  ADJ_CONFIG.pageSize = 20;
});

describe('人工裁决 (07 §6, F3.3)', () => {
  it('one card per episode with its source modules; counts of the operation on top', async () => {
    renderApp(PAGE);
    await screen.findByTestId('card-29');
    expect(screen.getByTestId('adj-counts')).toHaveTextContent('已裁 3 条 · 待裁 7 条 · 3 条尚未应用');
    // Default filter: pending (拿不准 included).
    expect(shownCards()).toEqual([29, 36, 22, 16, 33, 40, 47]);
    expect(card(29)).toHaveTextContent('来源：技能画像、任务成败判定');
    expect(within(card(29)).getByTestId('q-29-label')).toHaveTextContent('① 来源：技能画像 · 标注分歧');
    expect(within(card(29)).getByTestId('q-29-task_verdict')).toHaveTextContent('② 来源：任务成败判定 · 任务成败弃权');
    expect(within(card(16)).getByTestId('status-16')).toHaveTextContent('拿不准');
    expect(screen.getByText('裁决只属于这个任务：不会带到别的任务，也不会从别的任务带过来。同一个数据集再建任务，要重新裁。')).toBeInTheDocument();
  });

  it('every click is saved; 采纳新标注 turns the verdict question onto the new label', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-29');
    await user.click(within(card(29)).getByText('采纳新标注'));
    await waitFor(() => expect(posts(seen)).toEqual([{ episode_index: 29, line: 'label', decision: 'adopt_suggestion' }]));
    expect(within(card(29)).getByTestId('ask-29')).toHaveTextContent('按新标注「pour rice into the green bowl」，这条做成了吗？');
    expect(await screen.findByTestId('adj-counts')).toHaveTextContent('已裁 4 条 · 待裁 6 条 · 4 条尚未应用');
    await user.click(within(card(29)).getByText('判失败'));
    await waitFor(() => expect(posts(seen)).toHaveLength(2));
    expect(posts(seen)[1]).toEqual({ episode_index: 29, line: 'task_verdict', decision: 'failure' });
    // Still on screen with the default filter: cards never jump away after a click.
    expect(within(card(29)).getByTestId('status-29')).toHaveTextContent('已裁');
  });

  it('rule 1: 整条弃用 overrides the verdict (buttons disabled, reason given) and can be withdrawn', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-29');
    await user.click(within(card(29)).getByRole('button', { name: '其它原因，整条弃用' }));
    await waitFor(() => expect(posts(seen)).toEqual([{ episode_index: 29, line: 'label', decision: 'discard' }]));
    const verdict = within(card(29)).getByTestId('q-29-task_verdict');
    expect(within(verdict).getByText('这一条已整条弃用，不再判成败（「这条不要了」和「判它成功」互相矛盾）。')).toBeInTheDocument();
    for (const r of within(verdict).getAllByRole('radio')) expect(r).toBeDisabled();
    expect(within(card(29)).getByTestId('status-29')).toHaveTextContent('已裁');
    await user.click(within(card(29)).getByRole('button', { name: '撤销整条弃用' }));
    await waitFor(() => expect(posts(seen)[1]).toEqual({ episode_index: 29, line: 'label', decision: 'unsure' }));
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
    expect(screen.getByTestId('adj-counts')).toHaveTextContent('待裁 7 条 · 3 条尚未应用');
  });

  it('自行改写标注 needs the new label; a card only answers the questions it has (C4 1.5)', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-36');
    await user.click(within(card(36)).getByText('自行改写标注'));
    const q = within(card(36)).getByTestId('q-36-label');
    await user.click(within(q).getByRole('button', { name: '保存' }));
    expect(fieldErrors(q)).toEqual(['请填写改写后的标注']);
    await user.type(within(q).getByLabelText('改写后的标注'), 'push the plate back');
    await user.click(within(q).getByRole('button', { name: '保存' }));
    await waitFor(() => expect(posts(seen)).toEqual([{ episode_index: 36, line: 'label', decision: 'custom_label', new_label: 'push the plate back' }]));
    // No task-verdict buttons on a card without that question: the Daemon would refuse the decision.
    expect(within(card(36)).queryByText('判成功')).toBeNull();
    expect(within(card(36)).queryByTestId('q-36-task_verdict')).toBeNull();
  });

  it('filters: ?source= from the report preselects on the server; several sources filter on the page', async () => {
    const seen = recordRequests();
    const { user } = renderApp(`${PAGE}?source=task_success`);
    await screen.findByTestId('card-16');
    expect(shownCards()).toEqual([29, 16, 33, 40, 47]);
    const list = seen.filter((r) => r.method === 'GET' && r.path === `/tasks/${MAIN_TASK}/adjudication`);
    expect(list.at(-1)?.query.get('source')).toBe('task_success');
    await pick(user, '来源模块', '技能画像');
    await waitFor(() => expect(shownCards()).toEqual([29, 36, 22, 16, 33, 40, 47]));
    const last = seen.filter((r) => r.method === 'GET' && r.path === `/tasks/${MAIN_TASK}/adjudication`).at(-1)!;
    expect(last.query.get('source')).toBeNull();
    await pick(user, '问题类型', /^标注分歧/);
    await waitFor(() => expect(shownCards()).toEqual([29, 36, 22]));
    await pick(user, '状态', '拿不准');
    await waitFor(() => expect(shownCards()).toEqual([]));
    expect(screen.getByText('没有符合筛选条件的条目')).toBeInTheDocument();
  });

  it('the queue pages with cursors as it scrolls', async () => {
    ADJ_CONFIG.pageSize = 3;
    const seen = recordRequests();
    renderApp(PAGE);
    expect(await screen.findByText('已加载全部 7 条')).toBeInTheDocument();
    const calls = seen.filter((r) => r.method === 'GET' && r.path === `/tasks/${MAIN_TASK}/adjudication`);
    expect(calls.map((c) => Boolean(c.query.get('cursor')))).toEqual([false, true, true]);
    expect(shownCards()).toHaveLength(7);
  });

  it('rule 2: 被拒复议 lists rejects of appealable modules as optional cards and explains the final ones (D42)', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-29');
    await user.click(screen.getByRole('tab', { name: '被拒复议' }));
    const list = await screen.findByTestId('appeals');
    await within(list).findByTestId('card-6');
    expect(shownCards(list)).toEqual([6, 11, 23, 38, 45]);
    // Appeals are optional: the cards say so and the pending count does not move.
    expect(within(card(6)).getByTestId('status-6')).toHaveTextContent('可复议');
    await user.click(screen.getByText('为什么有的被拒条目不在这里'));
    const final = await screen.findByTestId('final-rejects');
    expect(final).toHaveTextContent('时间戳检查：1 条');
    // dedup is appealable now: not among the final ones.
    expect(final).not.toHaveTextContent('精确去重');
    expect(final).not.toHaveTextContent('任务成败判定');
    expect(screen.getByText(/物理与结构硬门.*和软分拒绝是终局/)).toHaveTextContent('可复议的只有：任务成败判定、精确去重');
    // Buttons come from the catalog: 恢复为可用 / 维持拒绝 / 拿不准, no 整条弃用.
    const q = within(card(6)).getByTestId('q-6-reject_appeal');
    expect(within(q).getAllByRole('radio').map((r) => r.closest('label')?.textContent)).toEqual(['恢复为可用', '维持拒绝', '拿不准']);
    expect(within(card(6)).queryByRole('button', { name: '其它原因，整条弃用' })).toBeNull();
    await user.click(within(q).getByText('恢复为可用'));
    await waitFor(() => expect(posts(seen)).toEqual([{ episode_index: 6, line: 'reject_appeal', decision: 'restore' }]));
    expect(screen.getByTestId('adj-counts')).toHaveTextContent('待裁 7 条');
  });

  it('执行裁决 confirms what will be applied and which relabels are judged again, then builds the subtask once (D39)', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-29');
    await user.click(within(card(29)).getByText('采纳新标注'));
    await waitFor(() => expect(screen.getByTestId('adj-counts')).toHaveTextContent('4 条尚未应用'));
    await user.click(within(card(40)).getByText('判成功'));
    await waitFor(() => expect(screen.getByTestId('adj-counts')).toHaveTextContent('5 条尚未应用'));
    await user.click(screen.getByRole('button', { name: '执行裁决' }));
    const dialog = await screen.findByRole('dialog', { name: '执行裁决' });
    // Counted over every unapplied card, not only the loaded (pending) ones: ep 4, 13, 9 were decided earlier.
    const summary = await within(dialog).findByTestId('apply-summary');
    expect(summary).toHaveTextContent('本次应用 5 条裁决（5 条 episode）。');
    expect(summary).toHaveTextContent('其中 2 条改了标，要按新标注重判任务成败。');
    expect(summary).toHaveTextContent('人已判了成功或失败的改标条目不重判。');
    const items = within(dialog).getByTestId('apply-list');
    expect(items).toHaveTextContent('ep 29：采纳新标注 → 按新标注重跑任务成败判定');
    expect(items).toHaveTextContent('ep 4：采纳新标注 → 按新标注重跑任务成败判定');
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
    expect(findTask(MAIN_TASK)!.delivery_stale).toBe(true);
  });

  it('按首轮的完整流程重判 sends relabel_rerun: full; a relabel a person judged is not counted as re-judged', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-29');
    await user.click(within(card(29)).getByText('采纳新标注'));
    await waitFor(() => expect(screen.getByTestId('adj-counts')).toHaveTextContent('4 条尚未应用'));
    await user.click(within(card(29)).getByText('判失败'));
    await waitFor(() => expect(posts(seen)).toHaveLength(2));
    await user.click(screen.getByRole('button', { name: '执行裁决' }));
    const dialog = await screen.findByRole('dialog', { name: '执行裁决' });
    const summary = await within(dialog).findByTestId('apply-summary');
    expect(summary).toHaveTextContent('本次应用 5 条裁决（4 条 episode）。');
    expect(summary).toHaveTextContent('其中 1 条改了标，要按新标注重判任务成败。');
    expect(summary).toHaveTextContent('人已判了成功或失败的改标条目不重判（本次 1 条）。');
    await user.click(within(dialog).getByText('按首轮的完整流程重判'));
    await user.click(within(dialog).getByRole('button', { name: '执行' }));
    expect(await screen.findByText('已创建执行裁决的子任务')).toBeInTheDocument();
    const apply = seen.find((r) => r.method === 'POST' && r.path === `/tasks/${MAIN_TASK}/adjudication/apply`);
    expect(apply?.body).toEqual({ relabel_rerun: 'full' });
    expect(findTask(MAIN_TASK)!.active_subtask?.scope.relabel_rerun).toBe('full');
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

  it('card videos are signed on demand and signed again when a URL fails', async () => {
    const seen = recordRequests();
    const { user } = renderApp(PAGE);
    await screen.findByTestId('card-29');
    const c = card(29);
    await user.click(await within(c).findByRole('button', { name: '同时播放' }));
    const video = await within(c).findByTestId('video-exterior_image_1_left');
    await waitFor(() => expect(seen.filter((r) => r.path === '/media/sign' && r.query.get('path')?.includes('episode_000029'))).toHaveLength(3));
    const src = video.getAttribute('src');
    fireEvent.error(video);
    await waitFor(() => expect(within(c).getByTestId('video-exterior_image_1_left').getAttribute('src')).not.toBe(src));
    expect(seen.filter((r) => r.path === '/media/sign' && r.query.get('path')?.includes('exterior_image_1_left/episode_000029'))).toHaveLength(2);
  });
});
