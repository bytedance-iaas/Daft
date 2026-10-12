// D94 (design doc 28 §3): episodes a module wrote a record for without judging them - data integrity rejected
// them, or they have no task text - are counted apart from those it assessed, on the task detail's module cards,
// in the report sections and on the Episode tab.
import { screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import type { EpisodeView, Report } from '../../api/types';
import { server } from '../../mocks/server';
import { MAIN_TASK, episodeView, mainReport } from '../../mocks/world';
import { renderApp } from '../../test/render';

/** The main task's report with timestamp_check having skipped three episodes data integrity rejected. */
function reportWithSkips(): Report {
  const r = mainReport(2);
  const s = r.modules.find((m) => m.id === 'timestamp_check')!.summary;
  s.assessed_episodes -= 3;
  s.skipped_episodes = 3;
  s.skipped_reasons = [{ name: 'integrity_rejected', count: 3 }];
  return r;
}

function serveSkips() {
  server.use(
    http.get('*/api/v1/tasks/:id/report', () => HttpResponse.json({ revision: 2, report: reportWithSkips(), links: [] })),
  );
}

describe('skipped episodes (D94)', () => {
  it('the module card says how many it skipped and why', async () => {
    serveSkips();
    renderApp(`/tasks/${MAIN_TASK}`);
    const row = await screen.findByTestId('module-stat-timestamp_check');
    const skipped = await within(row).findByTestId('module-skipped-timestamp_check');
    expect(skipped).toHaveTextContent('跳过 3 条（数据完整性已判废）');
    expect(within(screen.getByTestId('module-stat-task_success')).queryByTestId('module-skipped-task_success')).toBeNull();
  });

  it('the report section has a 跳过条数 figure with the reasons', async () => {
    serveSkips();
    renderApp(`/tasks/${MAIN_TASK}/report?section=timestamp_check`);
    const cell = await screen.findByTestId('skipped-timestamp_check');
    expect(cell).toHaveTextContent('跳过条数');
    expect(cell).toHaveTextContent('3');
    expect(cell).toHaveTextContent('数据完整性已判废');
  });

  it('the Episode tab says in one line why a module did not judge the episode', async () => {
    const view = (rev: number): EpisodeView => {
      const v = episodeView(18, rev);
      v.modules.timestamp_check = {
        episode_index: 18, module: 'timestamp_check', status: 'ok', findings: [], assessed: [],
        unassessable: [{ item: 'STRM-3', reason: 'integrity_rejected', message_zh: '数据完整性已判废，没有评估' }],
        readings: {}, details: { skipped: 'integrity_rejected', reason: '数据完整性已判废：文件为空' }, evidence: [], elapsed_s: 0, error: null,
      };
      return v;
    };
    server.use(http.get('*/api/v1/tasks/:id/episodes/:index', () => HttpResponse.json(view(2))));
    renderApp(`/tasks/${MAIN_TASK}/report?ep=18#episodes`);
    const line = await screen.findByTestId('episode-skipped-timestamp_check');
    expect(line).toHaveTextContent('没有评估：数据完整性已判废');
    expect(line).not.toHaveTextContent('文件为空');                  // the finding itself is in the summary above
    expect(screen.queryByTestId('unassessable-timestamp_check')).toBeNull();
  });
});
