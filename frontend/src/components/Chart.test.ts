import { describe, expect, it } from 'vitest';
import { LEGEND_GRID_TOP, barOption, groupedBarOption } from './Chart';

describe('bar chart options', () => {
  const items = [
    { name: '0.0–0.1', value: 1 },
    { name: '0.1–0.2', value: 3 },
  ];
  const top = (o: Record<string, unknown>) => (o.grid as { top: number }).top;

  it('a plain bar chart starts its plot near the top, lower when the value axis has a name', () => {
    expect(top(barOption(items))).toBe(12);
    expect(top(barOption(items, { valueName: '条' }))).toBe(28);
  });

  it('gridTop lines the plot up with a neighbour that has a legend on top', () => {
    expect(top(groupedBarOption(['a'], [{ name: 'cam', data: [1] }]))).toBe(LEGEND_GRID_TOP);
    expect(top(barOption(items, { gridTop: LEGEND_GRID_TOP }))).toBe(LEGEND_GRID_TOP);
  });
});
