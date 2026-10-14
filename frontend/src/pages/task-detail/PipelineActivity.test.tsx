import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { findTask } from '../../mocks/db';
import { renderWithProviders } from '../../test/render';
import { PipelineActivity } from './PipelineActivity';

const RUNNING = 'task_01HXR4M7';

describe('PipelineActivity', () => {
  it('draws a card per group the task has: two when 数据完整性 was not picked (2026-10-13)', () => {
    const task = findTask(RUNNING)!;
    const stages = task.progress.stages.filter((s) => s.id !== 'integrity' && s.id !== 'final' && s.id !== 'report' && s.id !== 'verify');
    renderWithProviders(<PipelineActivity task={task} stages={stages} />);
    const cards = screen.getByTestId('pipeline-activity').querySelector('.pipeline-layers') as HTMLElement;
    expect([...cards.children].map((c) => c.getAttribute('data-testid'))).toEqual(['stage-cpu', 'stage-model']);
    expect(cards.style.getPropertyValue('--cards')).toBe('2');
    expect(screen.queryByTestId('stage-integrity')).toBeNull();
    expect(screen.queryByTestId('timeline-integrity')).toBeNull();
  });
});
