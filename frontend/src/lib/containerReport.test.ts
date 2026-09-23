import { describe, expect, it } from 'vitest';
import { fieldLabel, integrityValue } from './reportView';

describe('report integrity of an mcap / lance run (D44)', () => {
  it('shows the delivery and the container findings as one readable line', () => {
    expect(fieldLabel('container')).toBe('数据包（mcap / Lance）');
    const v = {
      format: 'mcap',
      delivery: 'mcap_curated/（12 个 .mcap，原格式逐字节）',
      findings: [{ 项: '机器人型号', 状态: '正常', 说明: 'metadata 记录带 robot_type=franka' }, { 项: '任务文本', 状态: '缺失' }],
    };
    expect(integrityValue('container', v)).toBe(
      'mcap · 交付：mcap_curated/（12 个 .mcap，原格式逐字节） · 体检：机器人型号：正常（metadata 记录带 robot_type=franka）；任务文本：缺失',
    );
    expect(integrityValue('container', { format: 'lance', delivery: 'lance_episodes/', findings: [] })).toBe('lance · 交付：lance_episodes/');
  });
});
