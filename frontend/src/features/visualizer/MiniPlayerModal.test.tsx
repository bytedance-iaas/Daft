import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { StrictMode, useState } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { api, unwrap } from '../../api/client';
import type { EpisodeView } from '../../api/types';
import { zh } from '../../locales/zh';
import { FINDINGS_TASK } from '../../mocks/findings';
import { server } from '../../mocks/server';
import { renderWithProviders } from '../../test/render';
import { MiniPlayerModal } from './MiniPlayerModal';

async function episode(index: number): Promise<EpisodeView> {
  return (await unwrap(api().GET('/tasks/{id}/episodes/{index}', { params: { path: { id: FINDINGS_TASK, index } } }))) as EpisodeView;
}

/** The modal with its focus kept, as the report keeps it. */
function Harness({ view, start = 0 }: { view: EpisodeView; start?: number | null }) {
  const [focus, setFocus] = useState<number | null>(start);
  return <MiniPlayerModal taskId={FINDINGS_TASK} view={view} focus={focus} onFocus={setFocus} onClose={() => undefined} />;
}

/** Bands of findings on the progress bar (annotation events are points of their own). */
const bands = (root: ParentNode) => root.querySelectorAll('.vz-prog .ev span:not(.event)');

describe('MiniPlayerModal (design doc 18 §4.6)', () => {
  beforeEach(() => {
    HTMLMediaElement.prototype.canPlayType = (type: string) => (/avc1|av01/.test(type) ? 'probably' : '');
  });

  it('lists the findings as chips, the focused one on, and places them on the timeline', async () => {
    const view = await episode(6);
    expect(view.findings?.length).toBeGreaterThan(0);
    renderWithProviders(
      <StrictMode>
        <Harness view={view} />
      </StrictMode>,
    );
    const mini = await screen.findByTestId('vz-mini');
    const chips = await within(mini).findAllByTestId('vz-chip', {}, { timeout: 5000 });
    expect(chips).toHaveLength(view.findings!.length);
    expect(chips[0]).toHaveClass('on');
    expect(chips[0]).toHaveTextContent(view.findings![0].finding.item ?? view.findings![0].finding.code);
    // a finding with a moment is drawn on the progress bar
    await waitFor(() => expect(bands(mini).length).toBeGreaterThan(0));
    // another chip moves the focus
    chips[chips.length - 1].click();
    await waitFor(() => expect(within(screen.getByTestId('vz-mini')).getAllByTestId('vz-chip')[chips.length - 1]).toHaveClass('on'));
    expect(within(screen.getByTestId('vz-mini')).getAllByTestId('vz-chip')[0]).not.toHaveClass('on');
  });

  it('says plainly when the episode of the task input cannot be read (an old task whose data is gone)', async () => {
    const view = await episode(6);
    server.use(http.get('*/api/v1/tasks/:id/episodes/:index/viz', () => HttpResponse.json({ error: { code: 'not_found', message: 'gone' } }, { status: 404 })));
    renderWithProviders(<MiniPlayerModal taskId={FINDINGS_TASK} view={view} focus={0} onFocus={() => undefined} onClose={() => undefined} />);
    expect(await screen.findByText(/这条 episode 读不出来：gone/, {}, { timeout: 5000 })).toBeInTheDocument();
  });

  it('marks a finding about the whole episode 整条 and draws no band for it', async () => {
    const view = await episode(6);
    const base = view.findings![0];
    const whole = { ...view, findings: [{ ...base, finding: { ...base.finding, frames: undefined, time_s: undefined } }] };
    renderWithProviders(<MiniPlayerModal taskId={FINDINGS_TASK} view={whole} focus={0} onFocus={() => undefined} onClose={() => undefined} />);
    const chip = await screen.findByTestId('vz-chip', {}, { timeout: 5000 });
    expect(chip).toHaveTextContent(zh.viz.mini.whole);
    await screen.findByTestId('vz-progress');
    expect(bands(document).length).toBe(0);
  });

  it('says so when the task input cannot be read any more, instead of failing', async () => {
    server.use(http.get('*/api/v1/tasks/:id/viz', () => HttpResponse.json({ error: { code: 'not_found', message: '数据集已经不在了' } }, { status: 404 })));
    const view = await episode(6);
    renderWithProviders(<MiniPlayerModal taskId={FINDINGS_TASK} view={view} focus={null} onFocus={() => undefined} onClose={() => undefined} />);
    expect(await screen.findByText(/可视化读不出来：数据集已经不在了/, {}, { timeout: 5000 })).toBeInTheDocument();
    // the full page link is there while the registration is
    if (view.dataset_id) expect(screen.getByRole('link', { name: zh.viz.mini.openFull })).toHaveAttribute('href', `/visualize?dataset=${view.dataset_id}&ep=6`);
  });
});
