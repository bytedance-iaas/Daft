import { screen, within } from '@testing-library/react';
import { beforeAll, describe, expect, it } from 'vitest';
import { fill, pick } from '../../test/arco';
import { renderApp } from '../../test/render';
import { PREFLIGHT_DEBOUNCE } from './usePreflight';

beforeAll(() => {
  PREFLIGHT_DEBOUNCE.ms = 0;
});

async function preflight(uri: string) {
  const r = renderApp('/tasks/new');
  await screen.findByText('基本信息');
  await fill(r.user, '数据集地址', uri);
  await pick(r.user, '访问密钥', 'readonly-tos');
  return { ...r, card: await screen.findByTestId('preflight-card') };
}

describe('mcap and lance datasets in the task form (D44)', () => {
  it('mcap: no fps in the summary, says only file summaries were read, EEF asks for its file (F5.13), the grid defers the task text', async () => {
    const { user, card } = await preflight('tos://pai-kit-datasets/raw/warehouse_mcap');
    const summary = await within(card).findByText(/^mcap · 12 条 episode · 2 路相机（front \/ wrist）/);
    expect(summary.textContent).not.toContain('fps');
    expect(within(card).getByText('只读每个 mcap 文件的摘要（通道、消息数与元数据记录），未读取消息')).toBeInTheDocument();
    const eef = screen.getByTestId('module-eef_video_consistency');
    expect(within(eef).getByRole('checkbox')).toBeEnabled();                 // selectable; the file on screen 2
    expect(eef).not.toHaveTextContent('不支持');
    expect(within(screen.getByTestId('module-motion_quality')).getByRole('checkbox')).toBeEnabled();
    await user.click(within(screen.getByRole('radiogroup', { name: 'Episode 选择' })).getByRole('radio', { name: '自选' }));
    const grid = await screen.findByTestId('episode-grid');
    expect((await within(grid).findAllByText('（任务文本在 mcap 文件里，质检时读取）')).length).toBeGreaterThan(0);
  });

  it('lance: named with its LeRobot metadata version, says the tables were not read', async () => {
    const { card } = await preflight('tos://pai-kit-datasets/raw/pusht_lance');
    expect(await within(card).findByText(/^Lance（LeRobot v3 元数据） · 206 条 episode · 1 路相机（image） · 10 fps/)).toBeInTheDocument();
    expect(within(card).getByText('读取 meta/ 下的 LeRobot v3 元数据，未读取 frames / videos 表')).toBeInTheDocument();
    expect(screen.getByTestId('module-eef_video_consistency')).toHaveTextContent('该模块只能读 LeRobot 与 mcap 数据集，不支持 lance');
  });

  it('an rrd recording stays unsupported with the new wording', async () => {
    const { card } = await preflight('tos://pai-kit-datasets/raw/warehouse_rrd');
    expect(await within(card).findByText('当前支持 LeRobot v2/v3、mcap 与 Lance（lerobot-lance-convert 0.3.0 起），检测到 rrd')).toBeInTheDocument();
  });
});
