import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { zh } from '../../locales/zh';
import { eefConflictRecord } from '../../mocks/eef';
import { renderWithProviders } from '../../test/render';
import { EPISODE_BLOCKS } from './episodeBlocks';

describe('the EEF block of Episode 明细', () => {
  it("an episode of a dataset-level calibration suspect says so, and its camera's position cell is marked (design doc 25 §7.4)", async () => {
    const Block = EPISODE_BLOCKS.eef_video_consistency;
    const message = '相机 ext：7 条里 7 条的位置有同向的恒定偏差（约 38 px，向左）：多半是外参、TCP 偏移或假设值，不是逐条的数据问题';
    const dataset = [{ code: 'calibration_suspect', item: 'MV-4', severity: 'medium', unit: 'dataset', scope: { camera: 'ext' }, message_zh: message, readings: { episodes: [12] } }];
    renderWithProviders(<Block taskId="task-x" rev={1} ep={12} record={eefConflictRecord(12)} onEpisode={() => undefined} dataset={dataset} />);
    expect(await screen.findByTestId('eef-calibration-suspect')).toHaveTextContent(`${zh.eefDetail.output.flag.calibration_suspect} ${message}`);
    const position = within(screen.getByTestId('eef-cells')).getAllByRole('row').find((r) => r.textContent?.startsWith('位置'));
    expect(position?.textContent).toContain(zh.eefDetail.output.flag.calibration_suspect);
  });

  it('another episode, or no report, shows nothing of it', async () => {
    const Block = EPISODE_BLOCKS.eef_video_consistency;
    const dataset = [{ code: 'calibration_suspect', item: 'MV-4', severity: 'medium', unit: 'dataset', scope: { camera: 'ext' }, message_zh: 'x', readings: { episodes: [3] } }];
    renderWithProviders(<Block taskId="task-x" rev={1} ep={12} record={eefConflictRecord(12)} onEpisode={() => undefined} dataset={dataset} />);
    await screen.findByTestId('eef-cells');
    expect(screen.queryByTestId('eef-calibration-suspect')).toBeNull();
    expect(screen.getByTestId('eef-cells').textContent).not.toContain(zh.eefDetail.output.flag.calibration_suspect);
  });
});
