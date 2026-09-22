import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { findTask } from '../../mocks/db';
import { MAIN_TASK } from '../../mocks/world';
import { findDrawer, pick } from '../../test/arco';
import { recordRequests } from '../../test/record';
import { currentLocation, renderApp } from '../../test/render';

const REPORT = `/tasks/${MAIN_TASK}/report`;

function sectionIds(): string[] {
  return [...document.querySelectorAll('[id^="module-"]')].map((e) => e.id.replace('module-', ''));
}

function bodyRows(el: HTMLElement): HTMLElement[] {
  return [...el.querySelectorAll('tbody tr')].filter((r) => !r.classList.contains('arco-table-empty-row')) as HTMLElement[];
}

describe('质检报告 (07 §5)', () => {
  it('overview, integrity, scope and one section per module in report.json order', async () => {
    renderApp(REPORT);
    const eq = await screen.findByTestId('report-equation');
    expect(within(eq).getByTestId('eq-total')).toHaveTextContent('50');
    expect(within(eq).getByTestId('eq-rejected')).toHaveTextContent('7');
    expect(within(eq).getByTestId('eq-passed')).toHaveTextContent('41其中 10 条待人工确认');
    expect(within(eq).getByTestId('eq-held')).toHaveTextContent('2暂不交付');
    expect(screen.getByText('2 条待补跑')).toBeInTheDocument();
    expect(screen.getByTestId('integrity')).toHaveTextContent('28 条有；22 条没有');
    expect(screen.getByTestId('integrity')).toHaveTextContent('机器人型号未读到');
    const scope = screen.getByTestId('report-scope');
    const skipped = within(scope).getByText('运动学极限').closest('tr') as HTMLElement;
    expect(skipped).toHaveTextContent('未运行');
    expect(skipped).toHaveTextContent('预检没读到机器人型号');
    expect(sectionIds()).toEqual(['timestamp_check', 'motion_quality', 'visual_quality', 'video_action_sync', 'task_success', 'dedup', 'skill_profile']);
    const ts = screen.getByTestId('section-task_success');
    expect(within(ts).getByText('错误')).toBeInTheDocument();
    expect(within(ts).getByText('调用模型')).toBeInTheDocument();
    expect(within(ts).getByText('出错 2 条（待补跑）')).toBeInTheDocument();
    expect(within(ts).getByRole('link', { name: '去裁决（6）' })).toHaveAttribute('href', `/tasks/${MAIN_TASK}/adjudication?source=task_success`);
    expect(screen.getByTestId('section-skill_profile')).toHaveTextContent('这一节的结果来自子任务「重试 #1」。');
    // Summary scalars and {name, count} series.
    expect(screen.getByTestId('summary-visual_quality')).toHaveTextContent('平均分0.87');
    expect(within(screen.getByTestId('section-visual_quality')).getByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('分数分布：0.5–0.6 1'));
    // Small tables are inline, with episodes linking to the drawer.
    const inline = await screen.findByTestId('inline-table-timestamp_check');
    expect(within(inline).getByRole('link', { name: 'ep 18' })).toHaveAttribute('href', `${REPORT}?ep=18`);
  });

  it('a history revision is read only and shows the failed module with its error', async () => {
    renderApp(`${REPORT}?rev=1`);
    expect(await screen.findByTestId('history-banner')).toHaveTextContent('你在看历史版本 r0001。当前生效的是 r0002。');
    const sp = await screen.findByTestId('section-skill_profile');
    expect(within(sp).getByTestId('section-error-skill_profile')).toHaveTextContent('429 QuotaExceeded');
    expect(within(sp).getByRole('button', { name: '重试此模块' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '重试这 43 条' })).toBeDisabled();
    expect(within(screen.getByTestId('section-task_success')).getByRole('button', { name: '去裁决（6）' })).toBeDisabled();
    expect(within(screen.getByTestId('section-task_success')).getByRole('button', { name: '可复议（5）' })).toBeDisabled();
    expect(screen.queryByRole('link', { name: '去裁决（10）' })).toBeNull();
  });

  it('a section with appealable rejects offers 可复议（N） next to 去裁决（N）, filtered to that module (D42)', async () => {
    const { user } = renderApp(REPORT);
    const ts = await screen.findByTestId('section-task_success');
    expect(within(ts).getByRole('link', { name: '去裁决（6）' })).toHaveAttribute('href', `/tasks/${MAIN_TASK}/adjudication?source=task_success`);
    expect(within(ts).getByRole('link', { name: '可复议（5）' })).toHaveAttribute('href', `/tasks/${MAIN_TASK}/adjudication?tab=appeals&source=task_success`);
    const dedup = screen.getByTestId('section-dedup');
    // Nothing pending for dedup, one reject a person may appeal.
    expect(within(dedup).queryByRole('link', { name: /去裁决/ })).toBeNull();
    await user.click(within(dedup).getByRole('link', { name: '可复议（1）' }));
    await waitFor(() => expect(currentLocation()).toBe(`/tasks/${MAIN_TASK}/adjudication?tab=appeals&source=dedup`));
    const list = await screen.findByTestId('appeals');
    expect(await within(list).findByTestId('card-44')).toBeInTheDocument();
    expect(within(list).queryByTestId('card-6')).toBeNull();
    expect(screen.getByRole('tab', { name: '被拒复议' })).toHaveAttribute('aria-selected', 'true');
  });

  it('switching revisions and back to the current one', async () => {
    const { user } = renderApp(REPORT);
    await screen.findByTestId('report-equation');
    await pick(user, '结果版本', /^r0001/);
    await waitFor(() => expect(currentLocation()).toBe(`${REPORT}?rev=1`));
    expect(await screen.findByTestId('history-banner')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '回到当前版本' }));
    await waitFor(() => expect(currentLocation()).toBe(REPORT));
    await waitFor(() => expect(screen.queryByTestId('history-banner')).toBeNull());
  });

  it('重试这 N 条 creates a retry subtask for every error episode', async () => {
    const seen = recordRequests();
    const { user } = renderApp(REPORT);
    await user.click(await screen.findByRole('button', { name: '重试这 2 条' }));
    const dialog = await screen.findByRole('dialog', { name: '重试出错的条目' });
    await user.click(within(dialog).getByRole('button', { name: '开始重试' }));
    expect(await screen.findByText('已创建重试子任务，只补跑出错的条目')).toBeInTheDocument();
    const call = seen.find((r) => r.method === 'POST' && r.path === `/tasks/${MAIN_TASK}/retry`);
    expect(call?.body).toEqual({});
    expect(call?.headers['idempotency-key']).toBeTruthy();
  });

  it('detail tables: 100 rows a page, cursor paging, whitelisted sort, result_changed back to page one', async () => {
    const seen = recordRequests();
    const { user } = renderApp(REPORT);
    const vq = await screen.findByTestId('section-visual_quality');
    await user.click(within(vq).getByRole('button', { name: '明细：逐机位打分（147 行）' }));
    const table = await screen.findByTestId('detail-table');
    await waitFor(() => expect(bodyRows(table)).toHaveLength(100));
    expect(screen.getByTestId('table-page')).toHaveTextContent('第 1 / 2 页');
    await user.click(screen.getByRole('button', { name: '下一页' }));
    await waitFor(() => expect(bodyRows(screen.getByTestId('detail-table'))).toHaveLength(47));
    expect(screen.getByTestId('table-page')).toHaveTextContent('第 2 / 2 页');
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled();
    const tableCalls = seen.filter((r) => r.path === `/tasks/${MAIN_TASK}/report/tables/visual_quality`);
    expect(tableCalls.at(-1)?.query.get('cursor')).toBeTruthy();
    expect(tableCalls.at(-1)?.query.get('limit')).toBe('100');

    // Sort options are the registry whitelist only.
    await user.click(screen.getByRole('combobox', { name: '排序' }));
    const options = (await screen.findAllByRole('option')).map((o) => o.textContent);
    expect(options).toEqual(expect.arrayContaining(['Episode', '分数', '相机']));
    expect(options).not.toContain('清晰度');
    await user.click(screen.getAllByRole('option').find((o) => o.textContent === '分数')!);
    await user.click(screen.getByText('降序'));
    await waitFor(() => expect(screen.getByTestId('table-page')).toHaveTextContent('第 1 / 2 页'));
    await waitFor(() => {
      const last = seen.filter((r) => r.path.endsWith('/tables/visual_quality')).at(-1)!;
      expect([last.query.get('sort'), last.query.get('order'), last.query.get('cursor')]).toEqual(['score', 'desc', null]);
    });
    await waitFor(() => expect(bodyRows(screen.getByTestId('detail-table'))[0]).toHaveTextContent('0.94'));

    // A new result revision while paging: the table starts again from the first page.
    findTask(MAIN_TASK)!.result_rev = 3;
    await user.click(screen.getByRole('button', { name: '下一页' }));
    expect(await screen.findByText('结果版本已经更新，明细表回到第一页')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('table-page')).toHaveTextContent('第 1 / 2 页'));
  });

  it('逐条下钻: verdict, readings, evidence and every camera, played together and re-signed on failure', async () => {
    const seen = recordRequests();
    const { user } = renderApp(`${REPORT}?ep=29`);
    const drawer = await findDrawer('ep 29');
    expect(await within(drawer).findByTestId('episode-list')).toHaveTextContent('交付');
    expect(within(drawer).getByTestId('episode-review')).toHaveTextContent('技能画像 · 标注与画面归入不同技能族');
    expect(within(drawer).getByTestId('episode-readings')).toHaveTextContent('任务成败判定');
    expect(await within(drawer).findByAltText(/任务成败判定 · ep000029_0\.jpg/)).toHaveAttribute('src', expect.stringContaining('X-Tos-Signature'));
    // Nothing signed for videos until asked.
    expect(seen.filter((r) => r.path === '/media/sign' && r.query.get('path')?.includes('videos'))).toHaveLength(0);
    await user.click(within(drawer).getByRole('button', { name: '同时播放' }));
    const video = await within(drawer).findByTestId('video-wrist_image_left');
    await waitFor(() => expect(seen.filter((r) => r.path === '/media/sign' && r.query.get('path')?.includes('videos'))).toHaveLength(3));
    const first = video.getAttribute('src');
    expect(first).toContain('episode_000029.mp4');
    // 403 / expired: signed again automatically, then gives up after the configured retries.
    fireEvent.error(video);
    await waitFor(() => expect(within(drawer).getByTestId('video-wrist_image_left').getAttribute('src')).not.toBe(first));
    fireEvent.error(within(drawer).getByTestId('video-wrist_image_left'));
    await waitFor(() => expect(seen.filter((r) => r.path === '/media/sign' && r.query.get('path')?.includes('wrist_image_left'))).toHaveLength(3));
    fireEvent.error(within(drawer).getByTestId('video-wrist_image_left'));
    expect(await within(drawer).findByText('视频加载失败：播放地址已重签仍打不开')).toBeInTheDocument();
    // 重新加载 starts over with a fresh URL.
    await user.click(within(drawer).getByRole('button', { name: '重新加载' }));
    expect(await within(drawer).findByTestId('video-wrist_image_left')).toBeInTheDocument();
  });

  it('a v3 source video plays only its episode (#t=from,to)', async () => {
    const { user } = renderApp(`${REPORT}?ep=18`);
    const drawer = await findDrawer('ep 18');
    expect(await within(drawer).findByTestId('episode-list')).toHaveTextContent('判废');
    expect(within(drawer).getByTestId('episode-reasons')).toHaveTextContent('时间戳检查 · 残段：全程 0.5 秒（8 帧）');
    await user.click(within(drawer).getByRole('button', { name: '点击加载视频：exterior_image_1_left' }));
    const video = await within(drawer).findByTestId('video-exterior_image_1_left');
    expect(video.getAttribute('src')).toMatch(/file-000\.mp4\?.*#t=252,266$/);
  });

  it('性能剖析: latency by call kind with the v1 labels, switchable to the main run or a subtask', async () => {
    const seen = recordRequests();
    const { user } = renderApp(`${REPORT}#perf`);
    const latency = await screen.findByTestId('perf-latency');
    expect(within(latency).getByText('打分 · probe')).toBeInTheDocument();
    expect(within(latency).getByText('取证仲裁 · arbitration')).toBeInTheDocument();
    expect(latency).toHaveTextContent('392');
    expect(screen.getByText('因中断而重做')).toBeInTheDocument();
    await user.click(screen.getByText('仅主流程'));
    await waitFor(() => expect(within(screen.getByTestId('perf-latency')).getAllByRole('row')[1]).toHaveTextContent('345'));
    await user.click(screen.getByText('子任务 · 重试 #1'));
    await waitFor(() => {
      const last = seen.filter((r) => r.path === `/tasks/${MAIN_TASK}/perf`).at(-1)!;
      expect([last.query.get('scope'), last.query.get('subtask'), last.query.get('rev')]).toEqual(['subtask', 'sub_retry1', '2']);
    });
    expect(await screen.findByTestId('perf-tokens')).toHaveTextContent('打标 · caption');
    await user.click(screen.getByRole('tab', { name: '报告' }));
    await waitFor(() => expect(currentLocation()).toBe(REPORT));
  });

  it('episodes skipped for missing source files are counted apart and listed, with no retry (D40)', async () => {
    renderApp('/tasks/task_01HXPZ2K/report');
    const note = await screen.findByTestId('skipped-note');
    expect(note).toHaveTextContent('另有 3 条缺源文件，未参与质检。');
    expect(note).toHaveTextContent('补齐文件后另建任务');
    expect(within(note).queryByRole('button')).toBeNull();
    // Not part of the equation: the input stays the checked total.
    expect(within(screen.getByTestId('report-equation')).getByTestId('eq-total')).toHaveTextContent('1024');
    const list = screen.getByTestId('skipped-episodes');
    expect(list).toHaveTextContent('缺源文件、未参与质检的 episode（3 条）');
    const row = (ep: number) => within(list).getByText(`ep ${ep}`).closest('tr') as HTMLElement;
    expect(row(212)).toHaveTextContent('videos/chunk-000/observation.images.wrist/episode_000212.mp4');
    expect(row(587)).toHaveTextContent('data/chunk-000/episode_000587.parquet');
    expect(within(row(901)).getAllByText(/episode_000901\.mp4$/)).toHaveLength(2);
    // The generic integrity rows do not repeat the list as JSON.
    expect(screen.getByTestId('integrity')).not.toHaveTextContent('skipped_episodes');
  });

  it('a report without skipped episodes says nothing about them', async () => {
    renderApp(REPORT);
    await screen.findByTestId('report-equation');
    expect(screen.queryByTestId('skipped-note')).toBeNull();
    expect(screen.queryByTestId('skipped-episodes')).toBeNull();
  });

  it('a task without results says so', async () => {
    renderApp('/tasks/task_01HXR6T3/report');
    expect(await screen.findByText('任务还没有生成报告：主流程跑完后才有。')).toBeInTheDocument();
  });
});
