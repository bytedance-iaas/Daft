// The findings views of F12.5 (design doc 17 §5) on the mock world's task of the policy verdicts: the main
// task's story run as the two blocks report it (src/mocks/findings.ts). The main task is the task made before
// (C2 1.0, D59): ReportPage.test.tsx and TaskDetailPage.test.tsx keep testing that the console opens it as it was.
import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FINDINGS_TASK } from '../../mocks/findings';
import { MAIN_TASK } from '../../mocks/world';
import { pick } from '../../test/arco';
import { recordRequests } from '../../test/record';
import { currentLocation, renderApp } from '../../test/render';

const REPORT = `/tasks/${FINDINGS_TASK}/report`;

describe('质检报告 of a task of the policy verdicts (design doc 17 §5.2)', () => {
  it('the overview counts the rejects by taxonomy item and names the policy', async () => {
    renderApp(REPORT);
    await screen.findByTestId('report-equation');
    expect(screen.getByTestId('report-policy')).toHaveTextContent('默认（今天的判废规则）');
    expect(screen.getByText('判废原因分布（按检测项）')).toBeInTheDocument();
    await waitFor(() => {
      const charts = screen.getAllByTestId('chart').map((c) => c.getAttribute('aria-label') ?? '');
      expect(charts.find((l) => l.startsWith('判废原因分布（按检测项）'))).toMatch(/TASK-5 .+ 5，SET-1 .+ 1，STRM-5 .+ 1/);
    });
  });

  it('本次质检范围 has the block, what a module can do and its findings, then the coverage matrix', async () => {
    const { user } = renderApp(REPORT);
    const scope = await screen.findByTestId('report-scope');
    const ts = (await within(scope).findByRole('button', { name: '时间戳检查' })).closest('tr') as HTMLElement;
    expect(ts).toHaveTextContent('CPU 块 · 数值档');
    expect(ts).toHaveTextContent('可判废');
    expect(ts).toHaveTextContent('1 / 50 条');
    expect(within(scope).getByText('所在块')).toBeInTheDocument();
    expect(scope).not.toHaveTextContent('一票否决');
    const matrix = screen.getByTestId('coverage-matrix');
    expect(within(matrix).getByTestId('coverage-head')).toHaveTextContent(/本次覆盖分类表 \d+ \/ \d+ 项/);
    expect(within(matrix).getByTestId('coverage-ACT-4')).toHaveAttribute('data-status', 'unassessable');
    expect(within(matrix).getByTestId('coverage-TASK-5')).toHaveAttribute('data-status', 'covered');
    expect(within(matrix).getByTestId('coverage-TASK-5')).toHaveTextContent('TASK-5');
    // a covered item opens the section of the module that reports it
    await user.click(within(matrix).getByTestId('coverage-TASK-5'));
    await waitFor(() => expect(document.getElementById('module-task_success')).not.toBeNull());
  });

  it('every section starts with the findings statistics; a module\'s own view follows', async () => {
    renderApp(REPORT);
    const ts = await screen.findByTestId('section-task_success');
    expect(await within(ts).findByText('VLM 块 · VLM 档')).toBeInTheDocument();
    const stats = within(ts).getByTestId('findings-task_success');
    expect(within(stats).getByTestId('level-count-task_success-blocking')).toHaveTextContent('5');
    expect(within(stats).getByTestId('level-count-task_success-review')).toHaveTextContent('6');
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
    renderApp(`${REPORT}?section=dedup`);
    await screen.findByTestId('section-dedup');
    expect(currentLocation()).toContain('section=dedup');
  });
});

describe('Episode 明细 of a task of the policy verdicts (design doc 17 §5.4)', () => {
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
    expect(blocking).toHaveTextContent('TASK-5');
    expect(within(blocking).getByText('可复议')).toBeInTheDocument();
    expect(within(blocking).getByRole('link', { name: '去复议' })).toHaveAttribute('href', `/tasks/${FINDINGS_TASK}/adjudication?tab=appeals&source=task_success`);
    // the moment opens the mini player on that finding (design doc 18 §4.6): its chip is the focused one
    const seen = recordRequests();
    await user.click(within(blocking).getByTestId('finding-seek'));
    const mini = await screen.findByTestId('vz-mini');
    await waitFor(() => expect(seen.some((r) => r.path === `/tasks/${FINDINGS_TASK}/episodes/6/viz`)).toBe(true));
    const focused = await within(mini).findByText((_, el) => !!el?.classList.contains('chip') && el.classList.contains('on'));
    expect(focused).toHaveTextContent('TASK-5');
    await user.click(within(mini).getAllByRole('button', { name: '关闭' }).pop()!);
    await waitFor(() => expect(screen.queryByTestId('vz-mini')).toBeNull());
    // the module block says what it found, not a verdict
    expect(within(screen.getByTestId('episode-module-task_success')).getByTestId('level-blocking')).toBeInTheDocument();
    expect(within(screen.getByTestId('episode-module-dedup')).getByText('无发现')).toBeInTheDocument();
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
    await pick(user, '检测项', /^TASK-5/);
    await waitFor(() => expect(seen.some((r) => r.path.endsWith('/episodes') && r.query.get('item') === 'TASK-5' && r.query.get('level') === 'blocking')).toBe(true));
  });

  it('a task made before has no finding filters and keeps its 1.0 blocks (D59)', async () => {
    renderApp(`/tasks/${MAIN_TASK}/report?ep=6#episodes`);
    await screen.findByTestId('episode-summary');
    expect(screen.queryByTestId('filter-level')).toBeNull();
    expect(screen.queryByTestId('episode-findings')).toBeNull();
    expect(screen.getByTestId('episode-reasons')).toBeInTheDocument();
  });
});

describe('任务详情 of a two-block run (design doc 17 §5.3)', () => {
  it('one 分档进度 beside the tokens: both blocks in one chart, autolabel first, the full-set steps and 生成报告 & 产物交付 below (2026-10-04)', async () => {
    renderApp(`/tasks/${FINDINGS_TASK}`);
    const stages = await screen.findByTestId('stages');
    // both blocks' per-episode stages in the one chart: a card each and a row of the run timeline
    const chart = within(stages).getByTestId('pipeline-activity');
    expect([...chart.querySelectorAll('.pipeline-layer')].map((e) => e.getAttribute('data-testid'))).toEqual(['stage-numeric', 'stage-frame', 'stage-vlm']);
    expect(within(chart).getByTestId('pipeline-overlap')).toBeInTheDocument();
    // then the bars: autolabel first, the full-set steps, 生成报告 & 产物交付
    const rows = [...stages.querySelectorAll('[data-testid^="stage-"]')].filter((e) => !chart.contains(e)).map((e) => e.getAttribute('data-testid'));
    expect(rows).toEqual(['stage-autolabel', 'stage-dedup', 'stage-profile', 'stage-report_delivery']);
    expect(stages.closest('.grid-2')).toHaveTextContent('Token 消耗');
    expect(screen.queryByTestId('block-cpu')).toBeNull();
    expect(screen.queryByText('判决与交付')).toBeNull();
    expect(document.body).not.toHaveTextContent('数值档拦下了');
  });

  it('模块 is one row per module with what it assessed and found; unfolded, its findings charts; 去报告 opens its section', async () => {
    const { user } = renderApp(`/tasks/${FINDINGS_TASK}`);
    const row = await screen.findByTestId('module-stat-task_success');
    await within(row).findByTestId('module-assessed-task_success');
    expect(within(row).getByTestId('module-assessed-task_success')).toHaveTextContent('评估 48 / 选中');
    expect(row).toHaveTextContent('检出 11 条');
    expect(within(row).getByTestId('module-report-task_success')).toHaveAttribute('href', `/tasks/${FINDINGS_TASK}/report?section=task_success`);
    await user.click(await within(row).findByRole('button', { name: /展开：任务成败判定/ }));
    expect(await within(row).findByTestId('findings-task_success')).toBeInTheDocument();
    expect(screen.getByTestId('module-stat-kinematic_limits')).toHaveTextContent('未运行');
    expect(screen.queryByTestId('modules-table')).toBeNull();
  });

  it('a task made before keeps its module table (D59)', async () => {
    renderApp(`/tasks/${MAIN_TASK}`);
    expect(await screen.findByTestId('modules-table')).toBeInTheDocument();
    expect(screen.queryByTestId('block-cpu')).toBeNull();
  });
});

describe('人工裁决 of a task of the policy verdicts', () => {
  it('each question names the findings it asks about by code and item (C4 2.3.0)', async () => {
    renderApp(`/tasks/${FINDINGS_TASK}/adjudication?status=all`);
    const codes = await screen.findAllByTestId('question-codes');
    expect(codes.map((c) => c.textContent).join(' ')).toMatch(/TASK-5/);
    expect(codes.map((c) => c.textContent).join(' ')).toMatch(/LABEL-5/);
  });
});
