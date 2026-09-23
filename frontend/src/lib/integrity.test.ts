import { describe, expect, it } from 'vitest';
import type { Report, Task } from '../api/types';
import { mainReport } from '../mocks/world';
import { integrityItems, shortDigest, warningText } from './integrity';

const value = (items: ReturnType<typeof integrityItems>, key: string) => items.find((i) => i.key === key)?.value;

describe('数据包完整性 in Chinese (F6.2)', () => {
  it("renders the CLI's integrity and dataset facts as Chinese label / value pairs", () => {
    const report = mainReport(2);
    const task = { episodes: { mode: 'head', n: 50 }, source: { objects: 204, bytes: 1_520_331_122, digest: 'sha256:9f3c1a2b3c4d5e6f7a8b9c0de21a' } } as unknown as Task;
    const items = integrityItems(report, task);
    expect(items.map((i) => i.label)).toEqual(['格式', 'Episode', '相机', '帧率', '机器人型号', '语义档案', '任务标注', '源文件清单', '结构校验', '预检提示']);
    expect(value(items, 'format')).toBe('LeRobot v3（结构校验通过）');
    expect(value(items, 'episodes')).toBe('数据集 100 条，本次前 50 条（ep 0–49）');
    expect(value(items, 'cameras')).toBe('3 路：exterior_image_1_left、exterior_image_2_left、wrist_image_left');
    expect(value(items, 'fps')).toBe('15 fps');
    expect(value(items, 'robot_type')).toBe('未读到，运动学极限未运行');
    expect(value(items, 'profile')).toBe('命中 droid_100（按 repo_id 匹配）');
    expect(value(items, 'labels')).toBe('28 条有；22 条没有，没有的由模型补描述（来源记为「自产描述」）');
    expect(value(items, 'source')).toBe('204 个对象 · 1.42 GiB · sha256:9f3c…e21a（启动时固化）');
    expect(value(items, 'validation')).toBe('通过');
    expect(value(items, 'warnings')).toBe('无');
    for (const i of items) expect(i.value).not.toMatch(/[{}]|"|\b(kind|supported|with_task|matched)\b/);
  });

  it('says what is wrong: unsupported formats, validation problems, preflight warnings, skipped episodes', () => {
    const base = mainReport(2);
    const report: Report = {
      ...base,
      overview: { ...base.overview, counts: { ...base.overview.counts, skipped: 3 } },
      integrity: {
        format: { kind: 'lerobot', version: null, supported: false, detail: 'only LeRobot v2/v3 is supported' },
        validation: ['meta/info.json is malformed'],
        warnings: ["3 episodes miss their parquet or a camera's video (212, 587, 901); they are left out like v1 does", 'camera wrist has no video files', 'something new happened'],
        labels: null,
        robot_type: 'so101',
        extra_check: { passed: 3, failed: 1 },
      },
    };
    const items = integrityItems(report);
    expect(value(items, 'format')).toBe('LeRobot（不支持：only LeRobot v2/v3 is supported）');
    expect(value(items, 'episodes')).toBe('数据集 100 条，本次质检 50 条；另有 3 条缺源文件');
    expect(value(items, 'robot_type')).toBe('so101');
    expect(value(items, 'labels')).toBe('未读到');
    expect(value(items, 'validation')).toBe('meta/info.json is malformed');
    expect(items.find((i) => i.key === 'warnings')).toMatchObject({ full: true, warn: true });
    expect(value(items, 'warnings')).toBe(
      '3 条缺少 parquet 或某一路相机的视频（212, 587, 901）：照 v1 的做法剔除，不参与质检、不进任何清单，名单见下表；相机 wrist 没有视频文件；命令行原文：something new happened',
    );
    expect(value(items, 'extra_check')).toBe('passed 3 · 不合格 1');
    expect(items.find((i) => i.key === 'profile')).toBeUndefined(); // older reports: no row
  });

  it('keeps older reports readable', () => {
    const base = mainReport(2);
    const items = integrityItems({ ...base, integrity: { format: 'LeRobot v2' } });
    expect(value(items, 'format')).toBe('LeRobot v2');
    expect(warningText('info.json total_episodes is 100 but the episode table lists 98')).toBe('info.json 写的是 100 条，episode 表里有 98 条');
    expect(warningText('episode indices are not 0..9; selections use the indices as listed')).toBe('episode 编号不是 0–9 连续的；自选范围按表里列出的编号算');
    expect(shortDigest('sha256:0123456789abcdef')).toBe('sha256:0123…cdef');
    expect(shortDigest(null)).toBeNull();
  });
});
