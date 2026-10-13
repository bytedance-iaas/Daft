import { describe, expect, it } from 'vitest';
import { publicOptionLabel } from './publicDatasets';

describe('a cache-bucket dataset in the picker (2026-10-12: every format the platform reads)', () => {
  it('says the format and the episodes after the name', () => {
    expect(publicOptionLabel({ name: 'lerobot/droid_100', format_hint: 'lerobot_v2', episodes: 100 })).toBe('lerobot/droid_100 · LeRobot v2 · 100 条');
    expect(publicOptionLabel({ name: 'cups/mcap', format_hint: 'mcap', episodes: 1 })).toBe('cups/mcap · mcap · 1 条');
    expect(publicOptionLabel({ name: 'pusht', format_hint: 'lance', episodes: null })).toBe('pusht · Lance');
  });

  it('a raw UMI session runs the EEF module only, and says so', () => {
    expect(publicOptionLabel({ name: 'cups', format_hint: 'umi_session', episodes: null })).toBe('cups · UMI 原始会话（只能跑 EEF–视频一致性）');
  });

  it('without a known format, the name alone', () => {
    expect(publicOptionLabel({ name: 'x', format_hint: 'unknown', episodes: 3 })).toBe('x');
    expect(publicOptionLabel({ name: 'y' })).toBe('y');
  });
});
