import { describe, expect, it } from 'vitest';
import { registry } from '../../mocks/world';
import { askable } from './EpisodesTab';

describe('which review items the Episode tab links to the adjudication page (D43)', () => {
  it('questions of the lines a module declares, and appeals of appealable modules', () => {
    expect(askable(registry, { module: 'skill_profile', kind: 'label_conflict' })).toBe(true);
    expect(askable(registry, { module: 'task_success', kind: 'task_verdict' })).toBe(true);
    expect(askable(registry, { module: 'task_success', kind: 'reject_appeal' })).toBe(true);
    expect(askable(registry, { module: 'dedup', kind: 'reject_appeal' })).toBe(true);
  });

  it("another module's abstention is shown, never linked", () => {
    expect(askable(registry, { module: 'motion_quality', kind: 'task_verdict' })).toBe(false);
    expect(askable(registry, { module: 'timestamp_check', kind: 'reject_appeal' })).toBe(false);
    expect(askable(registry, { module: 'task_success' })).toBe(false);
    expect(askable(undefined, { module: 'task_success', kind: 'task_verdict' })).toBe(false);
  });
});
