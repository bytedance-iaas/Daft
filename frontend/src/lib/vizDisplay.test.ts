import { describe, expect, it } from 'vitest';
import type { VizCamera, VizDisplay, VizDisplayConfig, VizStream } from '../api/types';
import {
  addGroup,
  assignDim,
  cameraRows,
  camerasOf,
  dimId,
  drawerConfig,
  drawerState,
  groupOfDim,
  groupsOf,
  hasLayout,
  isEmpty,
  moveRow,
  removeGroup,
  sameGroups,
  savedCells,
  setGroup,
  setLine,
  startOf,
  withLayout,
  withoutLayout,
} from './vizDisplay';

function cam(key: string): VizCamera {
  return { key, name: key, source: `observation.images.${key}`, kind: 'video', access: 'direct', codec: 'h264', codec_string: 'avc1.640028', width: 640, height: 480, fps: 30, transcoded: false, reason: null, hidden: false };
}

const series = (key: string): VizStream => ({ key, kind: 'series', name: key, unit: null, lines: [], smart: true, available: true, reason: null, sources: [], rate_hz: null });
const MODEL = { cameras: [cam('front'), cam('wrist')], streams: [series('arm'), series('observation_state')] };

const DEFAULTS: VizDisplay['defaults'] = {
  cameras: [
    { key: 'front', name: 'front', source: 'observation.images.front' },
    { key: 'wrist', name: 'wrist', source: 'observation.images.wrist' },
  ],
  groups: [
    {
      key: 'observation_state',
      name: 'observation.state / action',
      unit: null,
      smart: true,
      lines: [
        { source: 'observation.state', dim: 0, name: 'j0', role: 'state' },
        { source: 'action', dim: 0, name: 'j0', role: 'action' },
      ],
    },
    { key: 'observation_force', name: 'observation.force', unit: null, smart: false, lines: [{ source: 'observation.force', dim: 0, name: 'dim_0', role: 'other' }] },
  ],
  dimensions: [
    { source: 'observation.state', dim: 0, name: 'j0', role: 'state' },
    { source: 'action', dim: 0, name: 'j0', role: 'action' },
    { source: 'observation.force', dim: 0, name: 'dim_0', role: 'other' },
  ],
  tracks: [{ key: 'subtask_index', name: '子任务' }],
  groups_editable: true,
};

describe('vizDisplay (design doc 21 §6)', () => {
  it('starts the player from a saved layout; cells the model lost are empty', () => {
    const cfg: VizDisplayConfig = {
      layout: { template: 'custom', cols: 2, rows: 1, cells: [{ kind: 'video', key: 'gone' }, { kind: 'curve', key: 'arm' }] },
      curves: { hidden: { arm: ['x'] } },
      track: 'subtask_index',
      playback: { speed: 1.5, loop: true },
    };
    const s = startOf(cfg, MODEL);
    expect(s.template).toBe('custom');
    expect(s.layout?.cells).toEqual([{ kind: 'empty' }, { kind: 'curve', key: 'arm' }]);
    expect([s.hidden, s.track, s.speed, s.loop]).toEqual([{ arm: ['x'] }, 'subtask_index', 1.5, true]);
    expect(startOf({ layout: { template: 'video' } }, MODEL)).toMatchObject({ template: 'video', layout: null, speed: 1, loop: false });
    expect(startOf(null, MODEL)).toMatchObject({ template: 'smart', layout: null, hidden: {}, track: null });
    expect(savedCells({ template: 'custom', cols: 1, rows: 1 }, MODEL)).toBeNull();
  });

  it('saves the player as the default layout and takes it away, keeping cameras and groups', () => {
    const base: VizDisplayConfig = { cameras: [{ key: 'wrist' }], curves: { groups: DEFAULTS.groups } };
    const depth = { kind: 'depth' as const, key: 'd', view: { cmap: 'gray' as const, lo: 1, hi: 2, overlay: true, opacity: 0.4 } };
    const saved = withLayout(base, { template: 'custom', shape: { cols: 2, rows: 1 }, cells: [depth], hidden: { arm: ['a'], other: [] }, track: null, speed: 2, loop: false });
    expect(saved).toEqual({
      cameras: [{ key: 'wrist' }],
      layout: { template: 'custom', cols: 2, rows: 1, cells: [depth, { kind: 'empty' }] },
      curves: { groups: DEFAULTS.groups, hidden: { arm: ['a'] } },
      track: null,
      playback: { speed: 2, loop: false },
    });
    expect(withLayout(null, { template: 'smart', shape: { cols: 3, rows: 2 }, cells: [], hidden: {}, track: 'subtask_index', speed: 1, loop: true }).layout).toEqual({ template: 'smart' });
    expect(hasLayout(saved)).toBe(true);
    expect(withoutLayout(saved)).toEqual({ cameras: [{ key: 'wrist' }], curves: { groups: DEFAULTS.groups } });
    expect(hasLayout(withoutLayout(saved))).toBe(false);
    expect(withoutLayout({ layout: { template: 'video' }, playback: { speed: 1, loop: true } })).toBeNull();
    expect(isEmpty({ curves: { hidden: {} } })).toBe(true);
  });

  it('edits the cameras: order, names, hidden; the dataset’s own order saves nothing', () => {
    let rows = cameraRows({ cameras: [{ key: 'wrist', name: '腕部', hidden: true }, { key: 'gone' }] }, DEFAULTS);
    expect(rows.map((r) => [r.key, r.name, r.hidden])).toEqual([
      ['wrist', '腕部', true],
      ['front', '', false],
    ]);
    expect(camerasOf(rows, DEFAULTS)).toEqual([{ key: 'wrist', name: '腕部', hidden: true }, { key: 'front' }]);
    rows = moveRow(rows, 1, -1).map((r) => ({ ...r, name: '', hidden: false }));
    expect(camerasOf(rows, DEFAULTS)).toBeNull();
    expect(moveRow([1, 2], 0, -1)).toEqual([1, 2]);
  });

  it('regroups the dimensions: into a new group, out of every group, renamed', () => {
    let groups = groupsOf(null, DEFAULTS);
    expect(sameGroups(groups, DEFAULTS)).toBe(true);
    const force = DEFAULTS.dimensions[2];
    groups = addGroup(groups, '力 sensor', new Set(['observation_force']));
    expect(groups.at(-1)).toMatchObject({ key: 'sensor', name: '力 sensor', lines: [] });
    groups = assignDim(groups, force, 'sensor');
    expect(groupOfDim(groups, dimId(force))?.key).toBe('sensor');
    expect(groups.find((g) => g.key === 'observation_force')?.lines).toEqual([]);
    groups = setLine(groups, dimId(force), { name: 'fz', role: 'state' });
    expect(groupOfDim(groups, dimId(force))?.lines).toEqual([{ source: 'observation.force', dim: 0, name: 'fz', role: 'state' }]);
    groups = setGroup(groups, 'sensor', { unit: 'N', smart: false });
    groups = assignDim(groups, DEFAULTS.dimensions[1], null);
    expect(groupOfDim(groups, dimId(DEFAULTS.dimensions[1]))).toBeNull();
    expect(sameGroups(groups, DEFAULTS)).toBe(false);
    expect(addGroup(addGroup([], 'x'), 'x').map((g) => g.key)).toEqual(['x', 'x_2']);
    expect(removeGroup(groups, 'sensor').map((g) => g.key)).toEqual(['observation_state', 'observation_force']);
  });

  it('saves what the drawer changed; a layout and hidden lines on groups that are gone are fitted', () => {
    const config: VizDisplayConfig = {
      layout: { template: 'custom', cols: 2, rows: 1, cells: [{ kind: 'curve', key: 'observation_force' }, { kind: 'video', key: 'front' }] },
      curves: { hidden: { observation_force: ['dim_0'], observation_state: ['j0', 'nope'] } },
    };
    const doc = { config, defaults: DEFAULTS };
    const st = drawerState(doc);
    expect(drawerConfig(doc, st)).toEqual({ ...config, curves: { hidden: { observation_force: ['dim_0'], observation_state: ['j0'] } } });
    // the force group taken away: its cell is empty, its hidden lines go; the empty group is not saved
    const groups = removeGroup(st.groups, 'observation_force');
    const out = drawerConfig(doc, { ...st, groups, track: 'subtask_index', speed: 1.5 });
    expect(out?.layout?.cells).toEqual([{ kind: 'empty' }, { kind: 'video', key: 'front' }]);
    expect(out?.curves).toEqual({ groups: [DEFAULTS.groups[0]], hidden: { observation_state: ['j0'] } });
    expect(out).toMatchObject({ track: 'subtask_index', playback: { speed: 1.5, loop: false } });
    // nothing but the defaults: nothing to save
    expect(drawerConfig({ config: null, defaults: DEFAULTS }, drawerState({ config: null, defaults: DEFAULTS }))).toBeNull();
    // an mcap dataset's groups are never written
    const mcap = { config: null, defaults: { ...DEFAULTS, groups_editable: false } };
    expect(drawerConfig(mcap, { ...drawerState(mcap), groups: [] })).toBeNull();
  });
});
