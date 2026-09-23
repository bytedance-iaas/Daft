import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MAIN_TASK } from '../../mocks/world';
import { findDrawer, pick } from '../../test/arco';
import { recordRequests } from '../../test/record';
import { currentLocation, renderApp } from '../../test/render';

const REPORT = `/tasks/${MAIN_TASK}/report`;

function sectionIds(): string[] {
  return [...document.querySelectorAll('[id^="module-"]')].map((e) => e.id.replace('module-', ''));
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
    // Key figures and charts from the summary's chart-ready aggregates.
    expect(screen.getByTestId('summary-visual_quality')).toHaveTextContent('平均分0.87');
    const scoreChart = within(within(screen.getByTestId('section-visual_quality')).getByTestId('chart-score')).getByTestId('chart');
    expect(scoreChart).toHaveAttribute('aria-label', expect.stringContaining('0.5–0.6 1'));
  });

  it('the header has a standing 人工裁决 entry and no 「小节和…一一对应」 line; integrity is Chinese side by side (D47, F6.2)', async () => {
    renderApp(REPORT);
    await screen.findByTestId('report-equation');
    const entry = screen.getByTestId('adjudicate-entry');
    expect(entry).toHaveAttribute('href', `/tasks/${MAIN_TASK}/adjudication`);
    expect(within(entry).getByRole('button', { name: '人工裁决（10）' })).toHaveClass('arco-btn-primary');
    expect(document.body).not.toHaveTextContent('一一对应');
    const integrity = screen.getByTestId('integrity');
    expect(integrity.tagName).toBe('DL');
    const pairs = [...integrity.querySelectorAll('.desc-item')].map((d) => [d.querySelector('dt')?.textContent, d.querySelector('dd')?.textContent]);
    expect(pairs).toContainEqual(['格式', 'LeRobot v3（结构校验通过）']);
    expect(pairs).toContainEqual(['Episode', '数据集 100 条，本次前 50 条（ep 0–49）']);
    expect(pairs).toContainEqual(['语义档案', '命中 droid_100（按 repo_id 匹配）']);
    expect(pairs).toContainEqual(['源文件清单', '204 个对象 · 1.42 GiB · sha256:4be1…c3d2（启动时固化）']);
    expect(integrity).not.toHaveTextContent(/[{}]|with_task|supported|matched|robot_type/);
    // The CLI does not know the wall time: the main run plus the retry that produced r0002.
    expect(screen.getByTestId('report-duration')).toHaveTextContent('主流程 + 重试 #1');
  });

  it('a task with results but nothing pending still offers 人工裁决 (D47)', async () => {
    renderApp('/tasks/task_01HXPZ2K/report');
    const entry = await screen.findByTestId('adjudicate-entry');
    const button = within(entry).getByRole('button', { name: '人工裁决' });
    expect(button).not.toHaveClass('arco-btn-primary');
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
    expect(screen.queryByRole('link', { name: '人工裁决（10）' })).toBeNull();
    expect(within(screen.getByTestId('adjudicate-entry')).queryByRole('link')).toBeNull();
    expect(screen.getByRole('button', { name: '人工裁决（10）' })).toBeDisabled();
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

  it('every module section has key figures and charts, no episode lists and no JSON (F6.2)', async () => {
    const seen = recordRequests();
    renderApp(REPORT);
    await screen.findByTestId('report-equation');
    const charts = (id: string) => within(screen.getByTestId(`section-${id}`)).getAllByTestId('chart').map((c) => c.getAttribute('aria-label') ?? '');
    const figures = (id: string) => screen.getByTestId(`summary-${id}`).textContent ?? '';
    expect(figures('timestamp_check')).toContain('不合格1');
    expect(figures('timestamp_check')).toContain('残段1');
    expect(charts('timestamp_check')).toEqual([expect.stringContaining('残段 1'), expect.stringContaining('时长分布：0–5 1')]);
    expect(figures('motion_quality')).toContain('执行器卡死4');
    expect(charts('motion_quality')[0]).toContain('子项均分：平滑度（计入总分） 0.86');
    expect(screen.getByTestId('section-motion_quality')).toHaveTextContent('执行器饱和：本数据集不适用——速度型指令和位置读数含义不同');
    expect(charts('visual_quality')[0]).toContain('逐相机分布：exterior_image_1_left 均分 0.89');
    expect(figures('video_action_sync')).toContain('已标注异常2不判废');
    expect(charts('video_action_sync')).toEqual([expect.stringContaining('判定分布：同步正常 43'), expect.stringContaining('逐相机典型滞后：exterior_image_1_left +0.03 秒')]);
    expect(within(screen.getByTestId('sync-cameras')).getByText('wrist_image_left')).toBeInTheDocument();
    expect(screen.getByTestId('sync-advice')).toHaveTextContent('数据集结论：全库逐相机中位滞后均在容差内');
    expect(figures('task_success')).toContain('任务文本来源原始标注 26 · 自产描述 21');
    expect(charts('task_success')).toEqual([
      expect.stringContaining('判定结论分布：打分层判成功 30'),
      expect.stringContaining('弃权原因：末态物证 … 在灰区 3'),
      expect.stringContaining('各层判定条数：打分层 47'),
      expect.stringContaining('出错停在哪一步：取证仲裁 2'),
    ]);
    expect(charts('dedup')).toEqual([expect.stringContaining('重复组大小：2 条一组 1')]);
    expect(figures('skill_profile')).toContain('标注分歧5高置信 3 · 人工复核 2');
    expect(charts('skill_profile')[1]).toContain('子技能分布：放置 › 放入容器 8');
    for (const id of ['timestamp_check', 'motion_quality', 'visual_quality', 'video_action_sync', 'task_success', 'dedup', 'skill_profile']) {
      const section = screen.getByTestId(`section-${id}`);
      expect(section.textContent, id).not.toMatch(/[{}"]|ep \d+/);
      expect(within(section).queryByRole('link', { name: /^ep \d+$/ })).toBeNull();
    }
    // Statistics only: the report page never pages through the detail tables any more.
    expect(screen.queryByText('明细表')).toBeNull();
    expect(seen.filter((r) => r.path.includes('/report/tables/'))).toHaveLength(0);
  });

  it('each section folds away with the toggle on its right, remembered per browser; 全部折叠 / 全部展开', async () => {
    const { user, unmount } = renderApp(REPORT);
    await screen.findByTestId('report-equation');
    await user.click(screen.getByRole('button', { name: '收起：视觉质量' }));
    expect(within(screen.getByTestId('section-visual_quality')).queryByTestId('summary-visual_quality')).toBeNull();
    expect(screen.getByRole('button', { name: '展开：视觉质量' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByTestId('summary-motion_quality')).toBeInTheDocument();
    unmount();
    // Another visit: still folded.
    const second = renderApp(REPORT);
    await screen.findByTestId('report-equation');
    expect(screen.getByRole('button', { name: '展开：视觉质量' })).toBeInTheDocument();
    await second.user.click(screen.getByRole('button', { name: '全部折叠' }));
    for (const id of ['timestamp_check', 'task_success', 'skill_profile']) expect(screen.queryByTestId(`summary-${id}`)).toBeNull();
    expect(screen.getByTestId('report-equation')).toBeInTheDocument(); // the overview never folds
    // Jumping from 本次质检范围 unfolds the section it jumps to.
    await second.user.click(within(screen.getByTestId('report-scope')).getByRole('button', { name: '精确去重' }));
    expect(await screen.findByTestId('summary-dedup')).toBeInTheDocument();
    await second.user.click(screen.getByRole('button', { name: '全部展开' }));
    for (const id of ['timestamp_check', 'visual_quality', 'skill_profile']) expect(screen.getByTestId(`summary-${id}`)).toBeInTheDocument();
  });

  it('a report written before the chart-ready aggregates still shows what it has (06 §6.2)', async () => {
    renderApp('/tasks/task_01HXPZ2K/report');
    const ts = await screen.findByTestId('section-timestamp_check');
    expect(within(ts).getAllByTestId('chart').length).toBeGreaterThan(0);
    // the so101 task's report has every v1 module, kinematics included
    expect(screen.getByTestId('summary-kinematic_limits')).toHaveTextContent('越限条数');
    expect(within(screen.getByTestId('section-kinematic_limits')).getAllByTestId('chart')[0]).toHaveAttribute('aria-label', expect.stringContaining('按越限类型：关节超速'));
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
    expect(within(drawer).getByTestId('episode-reasons')).toHaveTextContent('时间戳检查 · 未通过「时间戳检查」:全长只有 0.50 秒');
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
