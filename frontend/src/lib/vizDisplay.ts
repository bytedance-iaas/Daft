// The display configuration (design doc 21 §6, C4 VizDisplayConfig): what the player saves as the
// dataset's default layout and restores, and the edits of the display drawer - camera order, names
// and hiding, curve groups made of the dataset's dimensions (LeRobot / Lance), the subtitle track and
// playback. Pure functions over the configuration; the player and the drawer keep the state.
import type { VizDataset, VizDisplay, VizDisplayCamera, VizDisplayCell, VizDisplayConfig, VizDisplayGroup, VizDisplayLayout, VizDisplayLine } from '../api/types';
import { cellValid, fitCells, type CellContent, type GridShape, type LayoutTemplate } from './vizLayout';

export const SPEEDS = [1, 1.5, 2] as const;
export type Speed = (typeof SPEEDS)[number];

/** What a saved layout carries of the player (保存为缺省布局). */
export interface PlayerLayout {
  template: LayoutTemplate;
  shape: GridShape;
  cells: readonly CellContent[];
  /** per curve group, the lines not drawn */
  hidden: Readonly<Record<string, readonly string[]>>;
  /** the subtitle track picked, null: the dataset's primary */
  track: string | null;
  speed: number;
  loop: boolean;
}

function toCell(c: VizDisplayCell): CellContent {
  if (c.kind === 'empty' || !c.key) return { kind: 'empty' };
  if (c.kind === 'depth') return c.view ? { kind: 'depth', key: c.key, view: c.view } : { kind: 'depth', key: c.key };
  return { kind: c.kind, key: c.key };
}

function fromCell(c: CellContent): VizDisplayCell {
  if (c.kind === 'empty') return { kind: 'empty' };
  if (c.kind === 'depth') return c.view ? { kind: 'depth', key: c.key, view: c.view } : { kind: 'depth', key: c.key };
  return { kind: c.kind, key: c.key };
}

/** The cells of a saved custom layout; what the model no longer has is an empty cell. */
export function savedCells(layout: VizDisplayLayout | null | undefined, model: Pick<VizDataset, 'cameras' | 'streams'>): { cells: CellContent[]; shape: GridShape } | null {
  if (!layout || layout.template !== 'custom' || !layout.cols || !layout.rows || !layout.cells) return null;
  const shape = { cols: layout.cols, rows: layout.rows };
  const cells = layout.cells.map(toCell).map((c) => (cellValid(c, model) ? c : ({ kind: 'empty' } as const)));
  return { cells: fitCells(cells, shape), shape };
}

/** The player's starting point from the configuration in effect: template, cells, hidden lines, track, playback. */
export function startOf(cfg: VizDisplayConfig | null | undefined, model: Pick<VizDataset, 'cameras' | 'streams'>) {
  const saved = savedCells(cfg?.layout, model);
  const speed = cfg?.playback?.speed ?? 1;
  return {
    template: (saved ? 'custom' : (cfg?.layout?.template ?? 'smart')) as LayoutTemplate,
    layout: saved,
    hidden: { ...(cfg?.curves?.hidden ?? {}) } as Record<string, string[]>,
    track: cfg?.track ?? null,
    speed: SPEEDS.includes(speed as Speed) ? speed : 1,
    loop: Boolean(cfg?.playback?.loop),
  };
}

/** The configuration with the player's layout as the dataset's default (cameras and curve groups kept). */
export function withLayout(cfg: VizDisplayConfig | null | undefined, p: PlayerLayout): VizDisplayConfig {
  const hidden = Object.fromEntries(Object.entries(p.hidden).filter(([, names]) => names.length).map(([k, names]) => [k, [...names]]));
  const groups = cfg?.curves?.groups ?? null;
  const layout: VizDisplayLayout =
    p.template === 'custom' ? { template: 'custom', cols: p.shape.cols, rows: p.shape.rows, cells: fitCells(p.cells, p.shape).map(fromCell) } : { template: p.template };
  return {
    ...(cfg ?? {}),
    layout,
    curves: { ...(groups?.length ? { groups } : {}), hidden },
    track: p.track,
    playback: { speed: SPEEDS.includes(p.speed as Speed) ? (p.speed as Speed) : 1, loop: p.loop },
  };
}

/** The configuration without the player's layout (恢复默认布局); null when nothing else is left. */
export function withoutLayout(cfg: VizDisplayConfig | null | undefined): VizDisplayConfig | null {
  if (!cfg) return null;
  const groups = cfg.curves?.groups ?? null;
  const out: VizDisplayConfig = {};
  if (cfg.cameras?.length) out.cameras = cfg.cameras;
  if (groups?.length) out.curves = { groups };
  return isEmpty(out) ? null : out;
}

/** Whether the configuration saves a layout (what 恢复默认布局 takes away). */
export function hasLayout(cfg: VizDisplayConfig | null | undefined): boolean {
  if (!cfg) return false;
  return Boolean(cfg.layout || Object.values(cfg.curves?.hidden ?? {}).some((v) => v.length) || cfg.track || cfg.playback);
}

export function isEmpty(cfg: VizDisplayConfig | null | undefined): boolean {
  return !cfg || (!cfg.cameras?.length && !cfg.curves?.groups?.length && !hasLayout(cfg));
}

// ---------------------------------------------------------------- the drawer: cameras

export interface CameraRow {
  key: string;
  source: string;
  /** the dataset's own name */
  own: string;
  /** the configured name ('' = its own) */
  name: string;
  hidden: boolean;
}

/** The drawer's camera rows: the configured ones in their order, then the rest in the dataset's order. */
export function cameraRows(cfg: VizDisplayConfig | null | undefined, defaults: VizDisplay['defaults']): CameraRow[] {
  const byKey = new Map(defaults.cameras.map((c) => [c.key, c]));
  const out: CameraRow[] = [];
  for (const e of cfg?.cameras ?? []) {
    const c = byKey.get(e.key);
    if (!c || out.some((r) => r.key === c.key)) continue;
    out.push({ key: c.key, source: c.source, own: c.name, name: e.name ?? '', hidden: Boolean(e.hidden) });
  }
  for (const c of defaults.cameras) if (!out.some((r) => r.key === c.key)) out.push({ key: c.key, source: c.source, own: c.name, name: '', hidden: false });
  return out;
}

export function moveRow<T>(rows: readonly T[], i: number, dir: -1 | 1): T[] {
  const j = i + dir;
  if (j < 0 || j >= rows.length) return [...rows];
  const out = [...rows];
  [out[i], out[j]] = [out[j], out[i]];
  return out;
}

/** The rows as the configuration's cameras; null when they are the dataset's own (order, names, none hidden). */
export function camerasOf(rows: readonly CameraRow[], defaults: VizDisplay['defaults']): VizDisplayCamera[] | null {
  const same = rows.every((r, i) => r.key === defaults.cameras[i]?.key && !r.name.trim() && !r.hidden);
  if (same) return null;
  return rows.map((r) => ({ key: r.key, ...(r.name.trim() ? { name: r.name.trim() } : {}), ...(r.hidden ? { hidden: true } : {}) }));
}

// ---------------------------------------------------------------- the drawer: curve groups

export const dimId = (l: Pick<VizDisplayLine, 'source' | 'dim'>): string => `${l.source}#${l.dim}`;

/** The groups the drawer starts from: the configured ones, else a copy of the automatic ones. */
export function groupsOf(cfg: VizDisplayConfig | null | undefined, defaults: VizDisplay['defaults']): VizDisplayGroup[] {
  const own = cfg?.curves?.groups;
  return (own?.length ? own : defaults.groups).map((g) => ({ ...g, lines: g.lines.map((l) => ({ ...l })) }));
}

/** The group drawing a dimension, or null (不画). */
export function groupOfDim(groups: readonly VizDisplayGroup[], id: string): VizDisplayGroup | null {
  return groups.find((g) => g.lines.some((l) => dimId(l) === id)) ?? null;
}

/** The dimension moved into a group (at its end) or out of every group (null: 不画); its name and role go with it. */
export function assignDim(groups: readonly VizDisplayGroup[], dim: VizDisplayLine, key: string | null): VizDisplayGroup[] {
  const id = dimId(dim);
  const was = groupOfDim(groups, id);
  const line = was?.lines.find((l) => dimId(l) === id) ?? dim;
  return groups.map((g) => {
    const lines = g.lines.filter((l) => dimId(l) !== id);
    return { ...g, lines: g.key === key ? [...lines, { ...line }] : lines };
  });
}

/** A line's name or role in its group. */
export function setLine(groups: readonly VizDisplayGroup[], id: string, patch: Partial<Pick<VizDisplayLine, 'name' | 'role'>>): VizDisplayGroup[] {
  return groups.map((g) => ({ ...g, lines: g.lines.map((l) => (dimId(l) === id ? { ...l, ...patch } : l)) }));
}

export function setGroup(groups: readonly VizDisplayGroup[], key: string, patch: Partial<Pick<VizDisplayGroup, 'name' | 'unit' | 'smart'>>): VizDisplayGroup[] {
  return groups.map((g) => (g.key === key ? { ...g, ...patch } : g));
}

/** A new empty group named `name`, keyed by its name (made unique among `taken`: the groups and the other streams). */
export function addGroup(groups: readonly VizDisplayGroup[], name: string, taken: ReadonlySet<string> = new Set()): VizDisplayGroup[] {
  const base = name.trim().replace(/[^0-9A-Za-z_.-]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 80) || 'group';
  const used = new Set([...taken, ...groups.map((g) => g.key)]);
  let key = base;
  for (let i = 2; used.has(key); i += 1) key = `${base}_${i}`;
  return [...groups, { key, name: name.trim() || key, unit: null, smart: true, lines: [] }];
}

/** The group taken away; its lines are no longer drawn. */
export function removeGroup(groups: readonly VizDisplayGroup[], key: string): VizDisplayGroup[] {
  return groups.filter((g) => g.key !== key);
}

/** Whether the groups are the automatic ones (then nothing is saved for them). */
export function sameGroups(groups: readonly VizDisplayGroup[], defaults: VizDisplay['defaults']): boolean {
  const norm = (gs: readonly VizDisplayGroup[]) =>
    JSON.stringify(gs.filter((g) => g.lines.length).map((g) => [g.key, g.name, g.unit ?? null, g.smart, g.lines.map((l) => [l.source, l.dim, l.name, l.role])]));
  return norm(groups) === norm(defaults.groups);
}

// ---------------------------------------------------------------- the drawer: the whole configuration

export interface DrawerState {
  cameras: CameraRow[];
  groups: VizDisplayGroup[];
  /** the subtitle track, null: the dataset's primary */
  track: string | null;
  speed: number;
  loop: boolean;
}

export function drawerState(doc: Pick<VizDisplay, 'config' | 'defaults'>): DrawerState {
  const cfg = doc.config;
  return {
    cameras: cameraRows(cfg, doc.defaults),
    groups: groupsOf(cfg, doc.defaults),
    track: cfg?.track ?? null,
    speed: cfg?.playback?.speed ?? 1,
    loop: Boolean(cfg?.playback?.loop),
  };
}

/**
 * The configuration the drawer saves: the saved layout and hidden lines are kept, made to fit the
 * groups being saved (a cell or hidden line naming a group that is gone is left out); empty groups
 * are dropped; the parts equal to the defaults are not written.
 */
export function drawerConfig(doc: Pick<VizDisplay, 'config' | 'defaults'>, st: DrawerState): VizDisplayConfig | null {
  const cfg = doc.config;
  const groups = st.groups.filter((g) => g.lines.length);
  const own = doc.defaults.groups_editable && !sameGroups(groups, doc.defaults);
  const drawn = new Map((own ? groups : doc.defaults.groups).map((g) => [g.key, new Set(g.lines.map((l) => l.name))]));
  const hidden = Object.fromEntries(
    Object.entries(cfg?.curves?.hidden ?? {})
      .filter(([k]) => drawn.has(k))
      .map(([k, names]) => [k, names.filter((n) => drawn.get(k)?.has(n))])
      .filter(([, names]) => names.length),
  );
  // a saved cell naming a group that is gone, or a camera the dataset no longer has, is empty
  const cams = new Set(doc.defaults.cameras.map((c) => c.key));
  let layout = cfg?.layout ?? null;
  if (layout?.template === 'custom' && layout.cells) {
    const gone = (c: VizDisplayCell) => (c.kind === 'curve' && !drawn.has(c.key ?? '')) || (c.kind === 'video' && !cams.has(c.key ?? ''));
    layout = { ...layout, cells: layout.cells.map((c) => (gone(c) ? { kind: 'empty' } : c)) };
  }
  const cameras = camerasOf(st.cameras, doc.defaults);
  const speed = SPEEDS.includes(st.speed as Speed) ? (st.speed as Speed) : 1;
  const out: VizDisplayConfig = {
    ...(layout ? { layout } : {}),
    ...(cameras ? { cameras } : {}),
    ...(own || Object.keys(hidden).length ? { curves: { ...(own ? { groups } : {}), ...(Object.keys(hidden).length ? { hidden } : {}) } } : {}),
    ...(st.track ? { track: st.track } : {}),
    ...(speed !== 1 || st.loop ? { playback: { speed, loop: st.loop } } : {}),
  };
  return isEmpty(out) ? null : out;
}
