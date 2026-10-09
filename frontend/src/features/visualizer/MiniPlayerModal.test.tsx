import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { StrictMode, useState } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { api, unwrap } from '../../api/client';
import type { EpisodeView } from '../../api/types';
import { readPrefs } from '../../lib/prefs';
import { zh } from '../../locales/zh';
import { db } from '../../mocks/db';
import { eefOpinionRecord, eefOverlay } from '../../mocks/eef';
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
    // the title bar says which episode, plainly; the player's head does not repeat it (2026-10-04)
    expect(mini.querySelector('.arco-modal-title')).toHaveTextContent(/^ep 6$/);
    expect(mini.querySelector('.vz-head .ep small')).toBeNull();
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

describe('MiniPlayerModal: the EEF marks over the cameras (design doc 22 §3.3)', () => {
  beforeEach(() => {
    HTMLMediaElement.prototype.canPlayType = (type: string) => (/avc1|av01/.test(type) ? 'probably' : '');
    window.localStorage.clear();
  });
  const ran = () => {
    db.extraRecords = new Map([[FINDINGS_TASK, new Map([[6, { eef_video_consistency: eefOpinionRecord(6) }]])]]);
  };

  it('a task without the EEF module gets no menu and no complaint', async () => {
    const view = await episode(6);
    renderWithProviders(<Harness view={view} start={null} />);
    await screen.findByTestId('vz-progress');
    await new Promise((r) => setTimeout(r, 300));                      // the 404 has come back
    expect(screen.queryByTestId('vz-overlay')).toBeNull();
    expect(screen.queryByTestId('vz-overlay-failed')).toBeNull();
    expect(document.querySelector('canvas.vz-eef')).toBeNull();
  });

  it('draws the marks over the camera; the menu changes them and remembers the choice', async () => {
    ran();
    const view = await episode(6);
    renderWithProviders(<Harness view={view} start={null} />);
    const button = await screen.findByTestId('vz-overlay');
    expect(await screen.findByTestId('vz-eef-ext')).toBeInTheDocument();
    fireEvent.click(button);
    const menu = await screen.findByTestId('vz-overlay-menu');
    const box = (id: string) => within(screen.getByTestId(`vz-layer-${id}`)).getByRole('checkbox');
    expect(box('axis_x')).toBeChecked();
    expect(box('axis')).not.toBeChecked();                             // A is off by default
    expect(box('trail_future')).not.toBeChecked();
    fireEvent.click(within(menu).getByRole('button', { name: zh.viz.overlay.presets.model }));
    await waitFor(() => expect(box('axis')).toBeChecked());
    expect(box('axis_x')).not.toBeChecked();
    expect(readPrefs().eefOverlay).toMatchObject({ mode: 'on', preset: 'model' });
    fireEvent.click(box('trail_future'));
    await waitFor(() => expect(readPrefs().eefOverlay).toMatchObject({ preset: 'custom', layers: { trail_future: true, axis: true } }));
  });

  it('says one line when the marks cannot be read, and the cameras still play', async () => {
    ran();
    server.use(http.get('*/api/v1/tasks/:id/episodes/:index/eef-overlay', () => HttpResponse.json({ error: { code: 'internal', message: 'boom' } }, { status: 500 })));
    const view = await episode(6);
    renderWithProviders(<Harness view={view} start={null} />);
    expect(await screen.findByTestId('vz-overlay-failed')).toHaveTextContent(zh.viz.overlay.failed);
    expect(screen.getByTestId('vz-player')).toBeInTheDocument();
    expect(screen.queryByTestId('vz-overlay')).toBeNull();
  });

  it('lays out an EEF finding over the cameras with marks, the finding\'s one bold', async () => {
    ran();
    const view = await episode(6);
    const base = view.findings![0];
    const eef = { ...view, findings: [{ ...base, module: 'eef_video_consistency', level: 'info' as const, finding: { ...base.finding, code: 'opinion_mismatch', item: 'MV-4', message_zh: '模型认为末端投影与画面不符', frames: undefined, time_s: [2.67, 6.33] as [number, number], scope: { camera: 'ext' } } }] };
    renderWithProviders(<Harness view={eef} start={0} />);
    const canvas = await screen.findByTestId('vz-eef-ext');
    await waitFor(() => expect(document.querySelectorAll('.vz-cell.kind-curve')).toHaveLength(0));
    expect(document.querySelectorAll('.vz-cell.kind-video')).toHaveLength(1);
    expect(canvas.closest('.vz-cell')).toBeTruthy();
  });

  it('opens on a frame the opinion cites, paused there', async () => {
    ran();
    const view = await episode(6);
    renderWithProviders(<MiniPlayerModal taskId={FINDINGS_TASK} view={view} focus={null} seek={{ camera: 'ext', frame: 30 }} onFocus={() => undefined} onClose={() => undefined} />);
    await screen.findByTestId('vz-eef-ext');
    // the mock shows sample frame 30 at 30 / 15 s
    await waitFor(() => expect(document.querySelector('.vz-stamp')).toHaveTextContent(/^00:02\.0/));
  });

  it('a handheld gripper: the side panel sets the gap of its poses to bridge, and the marks are asked again (design doc 22 §5.2)', async () => {
    ran();
    const asked: (string | null)[] = [];
    // the marks over the input's first camera, as the mock world puts them
    const first = (await unwrap(api().GET('/tasks/{id}/viz', { params: { path: { id: FINDINGS_TASK } } }))).cameras[0].key;
    server.use(
      http.get('*/api/v1/tasks/:id/episodes/:index/eef-overlay', ({ request, params }) => {
        const gap = new URL(request.url).searchParams.get('max_gap_ms');
        asked.push(gap);
        const body = eefOverlay(String(params.id), Number(params.index), first);
        return HttpResponse.json({ ...body, interpolation: { max_gap_s: gap ? Number(gap) / 1000 : 0.1, default_s: 0.1, step_s: 1 / 30, range_steps: [2, 5], frames: { eef: gap ? 7 : 3 } } });
      }),
    );
    const view = await episode(6);
    renderWithProviders(<Harness view={view} start={null} />);
    await screen.findByTestId('vz-overlay');
    fireEvent.click(screen.getByRole('button', { name: zh.viz.info }));
    const cell = (await screen.findByTestId('vz-eef-ext')).closest('.vz-cell') as HTMLElement;
    fireEvent.pointerDown(cell);
    const gap = await screen.findByTestId('vz-gap');
    const input = (gap.tagName === 'INPUT' ? gap : gap.querySelector('input')) as HTMLInputElement;
    expect(input.value).toMatch(/^100/);
    expect(screen.getByTestId('vz-side')).toHaveTextContent('参考 67–167 ms');
    expect(screen.getByTestId('vz-side')).toHaveTextContent('补了 3 帧');
    fireEvent.change(input, { target: { value: '150' } });
    fireEvent.blur(input);
    await waitFor(() => expect(asked).toContain('150'));
    await waitFor(() => expect(screen.getByTestId('vz-side')).toHaveTextContent('补了 7 帧'));
    expect(readPrefs().eefOverlay).toMatchObject({ maxGapMs: 150 });
    fireEvent.click(within(screen.getByTestId('vz-side')).getByRole('button', { name: zh.viz.overlay.side.gapReset }));
    await waitFor(() => expect(readPrefs().eefOverlay).toMatchObject({ maxGapMs: null }));
  });
});
