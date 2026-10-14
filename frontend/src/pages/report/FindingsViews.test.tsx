// The findings views of F12.5 (design doc 17 §5) on the mock world's main task: its story run as the two blocks
// report it (src/mocks/world.ts).
import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MAIN_TASK } from '../../mocks/world';
import { pick } from '../../test/arco';
import { recordRequests } from '../../test/record';
import { currentLocation, renderApp } from '../../test/render';

const REPORT = `/tasks/${MAIN_TASK}/report`;

describe('质检报告: the findings (design doc 17 §5.2)', () => {
  it('the overview counts the rejects by taxonomy item and names the policy', async () => {
    renderApp(REPORT);
    await screen.findByTestId('report-equation');
    expect(screen.getByTestId('report-policy')).toHaveTextContent(/^判决策略默认$/);
    expect(screen.getByText('判废原因分布（按检测项）')).toBeInTheDocument();
    await waitFor(() => {
      const charts = screen.getAllByTestId('chart').map((c) => c.getAttribute('aria-label') ?? '');
      expect(charts.find((l) => l.startsWith('判废原因分布（按检测项）'))).toMatch(/TASK-4 .+ 5，STRM-5 .+ 1/);
    });
  });

  it('本次质检范围 has the block, what a module can do and its findings, then the coverage matrix', async () => {
    const { user } = renderApp(REPORT);
    const scope = await screen.findByTestId('report-scope');
    const ts = (await within(scope).findByRole('button', { name: '时间戳检查' })).closest('tr') as HTMLElement;
    expect(ts).toHaveTextContent('CPU 检查');                          // its group (design doc 26 §2)
    expect(ts).toHaveTextContent('可判废');
    expect(ts).toHaveTextContent('1 / 50 条');
    expect(within(scope).getByText('所在块')).toBeInTheDocument();
    expect(scope).not.toHaveTextContent('一票否决');
    const matrix = screen.getByTestId('coverage-matrix');
    expect(within(matrix).getByTestId('coverage-head')).toHaveTextContent(/本次覆盖分类表 \d+ \/ \d+ 项/);
    expect(within(matrix).getByTestId('coverage-ACT-4')).toHaveAttribute('data-status', 'unassessable');
    expect(within(matrix).getByTestId('coverage-TASK-4')).toHaveAttribute('data-status', 'covered');
    expect(within(matrix).getByTestId('coverage-TASK-4')).toHaveTextContent('TASK-4');
    // the tip says what the colour cannot: the item and why some episodes were not assessed (2026-10-05)
    await user.hover(within(matrix).getByTestId('coverage-ACT-4'));
    const tip = await screen.findByText(/^ACT-4 /);
    expect(tip).toHaveTextContent(/条）/);
    expect(tip).not.toHaveTextContent('部分条目评估不了');
    expect(tip).not.toHaveTextContent(/^ACT-4 \S+ 覆盖/);
    // a covered item opens the section of the module that reports it
    await user.click(within(matrix).getByTestId('coverage-TASK-4'));
    await waitFor(() => expect(document.getElementById('module-task_success')).not.toBeNull());
  });

  it('every section starts with the findings statistics; a module\'s own view follows', async () => {
    renderApp(REPORT);
    const ts = await screen.findByTestId('section-task_success');
    expect(await within(ts).findByText('模型检查')).toBeInTheDocument();
    const stats = within(ts).getByTestId('findings-task_success');
    expect(within(stats).getByTestId('level-count-task_success-blocking')).toHaveTextContent('5');
    expect(within(stats).getByTestId('level-count-task_success-review')).toHaveTextContent('10');   // the episodes it could not decide
    const items = within(within(stats).getByTestId('chart-items-task_success')).getByTestId('chart');
    expect(items).toHaveAttribute('aria-label', expect.stringMatching(/检出项：.*判废.*5 条/));
    // the module's own view is still there (06 §6.2's keys)
    expect(within(ts).getByTestId('summary-task_success')).toBeInTheDocument();
    const motion = screen.getByTestId('findings-motion_quality');
    expect(motion).toHaveTextContent('评估不了的原因');
    // the composite score's distribution is drawn once, by the module's own view
    expect(within(motion).queryByTestId('chart-score-motion_quality-score')).toBeNull();
    expect(within(screen.getByTestId('section-motion_quality')).getByTestId('chart-score')).toBeInTheDocument();
    expect(screen.getByTestId('dataset-findings-timestamp_check')).toHaveTextContent('时长远离全库四分位');
  });

  it('?section= opens the report on a module (the task detail\'s 去报告)', async () => {
    renderApp(`${REPORT}?section=duplicate_check`);
    await screen.findByTestId('section-duplicate_check');
    expect(currentLocation()).toContain('section=duplicate_check');
  });
});

describe('Episode 明细: the findings (design doc 17 §5.4)', () => {
  it('lists every finding by level, with its module, code, item and moment; the reasons left are the other ones', async () => {
    renderApp(`${REPORT}?ep=7#episodes`);
    const summary = await screen.findByTestId('episode-summary');
    const findings = await within(summary).findByTestId('episode-findings');
    // ep 7: task_success failed on it (an execution error), so the reason is the error, the findings what the others saw
    expect(within(summary).getByTestId('episode-reasons')).toHaveTextContent('执行出错');
    expect(findings).toHaveTextContent('级别按任务的判决策略给出');
  });

  it('a rejected episode has its blocking findings, each appealable one with 去复议, a moment to play from', async () => {
    const { user } = renderApp(`${REPORT}?ep=6#episodes`);
    const findings = await screen.findByTestId('episode-findings');
    const blocking = within(findings).getByTestId('findings-blocking');
    await waitFor(() => expect(blocking).toHaveTextContent('任务成败判定'));   // module names are the registry's
    // the text is coloured by the code's own level (the registry's), whatever the task's policy made of it
    const rows = within(findings).getAllByTestId('episode-finding');
    expect(rows.some((r) => r.getAttribute('data-tone') === 'blocking')).toBe(true);
    expect(blocking).toHaveTextContent('TASK-4');
    expect(within(blocking).getByText('可复议')).toBeInTheDocument();
    expect(within(blocking).getByRole('link', { name: '去复议' })).toHaveAttribute('href', `/tasks/${MAIN_TASK}/adjudication?tab=appeals&source=task_success`);
    // the moment opens the mini player on that finding (design doc 18 §4.6): its chip is the focused one
    const seen = recordRequests();
    await user.click(within(blocking).getByTestId('finding-seek'));
    const mini = await screen.findByTestId('vz-mini');
    await waitFor(() => expect(seen.some((r) => r.path === `/tasks/${MAIN_TASK}/episodes/6/viz`)).toBe(true));
    const focused = await within(mini).findByText((_, el) => !!el?.classList.contains('chip') && el.classList.contains('on'));
    expect(focused).toHaveTextContent('TASK-4');
    await user.click(within(mini).getAllByRole('button', { name: '关闭' }).pop()!);
    await waitFor(() => expect(screen.queryByTestId('vz-mini')).toBeNull());
    // the module block says what it found, not a verdict
    expect(within(screen.getByTestId('episode-module-task_success')).getByTestId('level-blocking')).toBeInTheDocument();
    expect(within(screen.getByTestId('episode-module-duplicate_check')).getByText('无发现')).toBeInTheDocument();
  });

  it('a review finding with an open question has 去裁决; what a module could not assess is said in its block', async () => {
    renderApp(`${REPORT}?ep=29#episodes`);
    const findings = await screen.findByTestId('episode-findings');
    const review = within(findings).getByTestId('findings-review');
    expect(within(review).getAllByRole('link', { name: '去裁决' }).length).toBeGreaterThan(0);
    expect(screen.getByTestId('unassessable-motion_quality')).toHaveTextContent('ACT-4');
  });

  it('filters by level and by taxonomy item through the API', async () => {
    const seen = recordRequests();
    const { user } = renderApp(`${REPORT}#episodes`);
    await screen.findByTestId('episode-findings');
    await pick(user, '级别', '判废');
    await waitFor(() => expect(seen.some((r) => r.path.endsWith('/episodes') && r.query.get('level') === 'blocking')).toBe(true));
    await pick(user, '检测项', /^TASK-4/);
    await waitFor(() => expect(seen.some((r) => r.path.endsWith('/episodes') && r.query.get('item') === 'TASK-4' && r.query.get('level') === 'blocking')).toBe(true));
  });
});

describe('任务详情 (design doc 17 §5.3)', () => {
  it('one 详细进度 beside the tokens: CPU 检查 and 模型检查 in one chart, 生成报告 below (design doc 26 §2)', async () => {
    renderApp(`/tasks/${MAIN_TASK}`);
    const stages = await screen.findByTestId('stages');
    // the episodes' stages in their groups in the one chart: a card each and a row of the run timeline
    const chart = within(stages).getByTestId('pipeline-activity');
    expect([...chart.querySelectorAll('.pipeline-layer')].map((e) => e.getAttribute('data-testid'))).toEqual(['stage-cpu', 'stage-model']);
    expect(within(chart).getByTestId('stage-cpu')).toHaveTextContent('CPU 检查');
    expect(within(chart).getByTestId('stage-cpu')).toHaveTextContent('50 / 50');
    expect([...chart.querySelectorAll('.pipeline-time-row')].map((e) => e.textContent)).toEqual(['CPU 检查', '模型检查']);
    expect(within(chart).getByTestId('pipeline-overlap')).toBeInTheDocument();
    // then the one bar after both blocks
    const rows = [...stages.querySelectorAll('[data-testid^="stage-"]')].filter((e) => !chart.contains(e)).map((e) => e.getAttribute('data-testid'));
    expect(rows).toEqual(['stage-report']);
    expect(within(stages).getByTestId('stage-report')).toHaveTextContent('生成报告');
    expect(stages).not.toHaveTextContent(/产物交付|数值档|帧档/);
    expect(stages.closest('.grid-2')).toHaveTextContent('Token 消耗');
    expect(screen.queryByTestId('block-cpu')).toBeNull();
    expect(screen.queryByText('判决与交付')).toBeNull();
    expect(document.body).not.toHaveTextContent('数值档拦下了');
  });

  it('an episode of Episode 流水线 opens the mini player with its findings as chips, as Episode 明细 does (2026-10-04)', async () => {
    const seen = recordRequests();
    const { user } = renderApp(`/tasks/${MAIN_TASK}`);
    const card = await screen.findByTestId('pipeline-episodes');
    // ep 44: a duplicate of ep 43, reported only (D87) (the newest 15 rows are eps 35-49)
    await user.click(await within(card).findByRole('button', { name: 'ep 44' }));
    const mini = await screen.findByTestId('vz-mini');
    await waitFor(() => expect(seen.some((r) => r.path === `/tasks/${MAIN_TASK}/episodes/44`)).toBe(true));
    const chips = await within(mini).findAllByTestId('vz-chip');
    expect(chips.length).toBeGreaterThan(0);
    // no line of instructions above the player
    expect(mini).not.toHaveTextContent('点发现芯片');
  });

  it('模块 is one row per module with what it assessed and found; unfolded, its findings charts; 去报告 opens its section', async () => {
    const { user } = renderApp(`/tasks/${MAIN_TASK}`);
    const row = await screen.findByTestId('module-stat-task_success');
    await within(row).findByTestId('module-assessed-task_success');
    expect(within(row).getByTestId('module-assessed-task_success')).toHaveTextContent('评估 48 / 选中');
    expect(row).toHaveTextContent('检出 15 条');
    expect(within(row).getByTestId('module-report-task_success')).toHaveAttribute('href', `/tasks/${MAIN_TASK}/report?section=task_success`);
    await user.click(await within(row).findByRole('button', { name: /展开：任务成败判定/ }));
    expect(await within(row).findByTestId('findings-task_success')).toBeInTheDocument();
    expect(screen.getByTestId('module-stat-kinematic_limits')).toHaveTextContent('未运行');
    expect(screen.queryByTestId('modules-table')).toBeNull();
  });
});

describe('人工裁决: the findings asked about', () => {
  it('each question names the findings it asks about by code and item (C4 2.3.0)', async () => {
    renderApp(`/tasks/${MAIN_TASK}/adjudication?status=all`);
    const codes = await screen.findAllByTestId('question-codes');
    expect(codes.map((c) => c.textContent).join(' ')).toMatch(/TASK-4/);
    expect(codes.map((c) => c.textContent).join(' ')).not.toMatch(/LABEL-4/);     // no label guard any more (D73)
  });
});
