import { describe, expect, it } from 'vitest';
import { containerValue } from './integrity';
import { fieldLabel } from './reportView';

describe('report integrity of an mcap / lance run (D44)', () => {
  it('shows the container findings as one readable line', () => {
    expect(fieldLabel('container')).toBe('数据包（mcap / Lance）');
    const v = {
      format: 'mcap',
      findings: [{ 项: '机器人型号', 状态: '正常', 说明: 'metadata 记录带 robot_type=franka' }, { 项: '任务文本', 状态: '缺失' }],
    };
    expect(containerValue(v)).toBe(
      'mcap · 体检：机器人型号：正常（metadata 记录带 robot_type=franka）；任务文本：缺失',
    );
    expect(containerValue({ format: 'lance', findings: [] })).toBe('lance');
  });

  it('keeps the delivery line of a report written before the export was retired (D69)', () => {
    expect(containerValue({ format: 'mcap', delivery: 'mcap_curated/（12 个 .mcap）', findings: [] })).toBe(
      'mcap · 交付：mcap_curated/（12 个 .mcap）',
    );
  });
});
