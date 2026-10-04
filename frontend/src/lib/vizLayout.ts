// The player's grid (design doc 18 §5.1–§5.3, design doc 19 §2): layout templates, the smart rule
// and cell sizes. Pure functions over the presentation model; the player keeps the cells in its state.
import type { VizDataset } from '../api/types';

export type CellContent = { kind: 'video'; key: string } | { kind: 'curve'; key: string } | { kind: 'empty' };

export type LayoutTemplate = 'smart' | 'video' | 'curve' | 'custom';

export interface GridShape {
  cols: number;
  rows: number;
}

/** The grid sizes the full player offers (columns × rows). */
export const GRID_SIZES: readonly GridShape[] = [
  { cols: 1, rows: 1 },
  { cols: 2, rows: 1 },
  { cols: 2, rows: 2 },
  { cols: 3, rows: 2 },
  { cols: 3, rows: 3 },
  { cols: 4, rows: 3 },
  { cols: 4, rows: 4 },
];

export const MAX_COLS = 4;
export const MAX_ROWS = 4;
/** Up to this many cells a template lays out as in phase one (at most 3 × 3, design doc 19 §2.2). */
export const SMALL_CELLS = 9;
const SMALL_MAX = 3;
/** The smart layout uses three columns from this grid width (px) on, two below. */
export const WIDE_GRID_PX = 1000;
/** More than nine cells: four columns from this grid width on, three below. */
export const WIDER_GRID_PX = 1240;
/** The mini player's cells in its one row (design doc 18 §4.6). */
export const MINI_MAX_CELLS = 3;
/** Curve groups the smart layout adds after the cameras. */
export const SMART_CURVES = 2;
export const CELL_GAP_PX = 10;

export function gridKey(g: GridShape): string {
  return `${g.cols}x${g.rows}`;
}

export function parseGridKey(key: string): GridShape | null {
  const m = /^([1-4])x([1-4])$/.exec(key);
  return m ? { cols: Number(m[1]), rows: Number(m[2]) } : null;
}

/** The smallest grid that holds n cells with at most `cols` columns and `maxRows` rows (at least one cell). */
export function shapeFor(n: number, cols: number, maxRows: number = MAX_ROWS): GridShape {
  const c = Math.max(1, Math.min(cols, MAX_COLS, Math.max(1, n)));
  return { cols: c, rows: Math.max(1, Math.min(maxRows, MAX_ROWS, Math.ceil(Math.max(1, n) / c))) };
}

/** Pads with empty cells / cuts to exactly cols × rows. */
export function fitCells(cells: readonly CellContent[], shape: GridShape): CellContent[] {
  const n = shape.cols * shape.rows;
  const out = cells.slice(0, n);
  while (out.length < n) out.push({ kind: 'empty' });
  return out;
}

function cameraCells(model: Pick<VizDataset, 'cameras'>): CellContent[] {
  return model.cameras.map((c) => ({ kind: 'video', key: c.key }));
}

function curveCells(model: Pick<VizDataset, 'streams'>, smartOnly: boolean): CellContent[] {
  return model.streams
    .filter((s) => s.kind === 'series' && s.available && (!smartOnly || s.smart))
    .map((s) => ({ kind: 'curve', key: s.key }));
}

/**
 * The cells of a template at a given grid width. Smart: every camera, then the first two smart curve
 * groups; up to nine cells three columns on a wide grid and two otherwise (it follows the width as side
 * panels open and close), more than nine four columns on a wider grid and three otherwise, up to four
 * rows. Video / curve: only those. What does not fit drops cameras from the end, never the curves;
 * `overflow` counts them (they stay a pick in 「+」/「更换」). Custom keeps the user's arrangement (null).
 */
export function templateLayout(
  template: LayoutTemplate,
  model: Pick<VizDataset, 'cameras' | 'streams'>,
  gridWidth: number,
): { cells: CellContent[]; shape: GridShape; overflow: number } | null {
  let cameras: CellContent[] = [];
  let curves: CellContent[] = [];
  let cols: number;
  if (template === 'video') {
    cameras = cameraCells(model);
    cols = cameras.length > SMALL_CELLS ? manyColumns(gridWidth) : Math.min(SMALL_MAX, Math.max(1, cameras.length));
  } else if (template === 'curve') {
    curves = curveCells(model, false);
    cols = curves.length > 1 ? 2 : 1;
  } else if (template === 'smart') {
    cameras = cameraCells(model);
    curves = curveCells(model, true).slice(0, SMART_CURVES);
    cols = smartColumns(gridWidth, cameras.length + curves.length);
  } else {
    return null;
  }
  const n = cameras.length + curves.length;
  const shape = shapeFor(n, cols, n > SMALL_CELLS ? MAX_ROWS : SMALL_MAX);
  const room = Math.max(0, shape.cols * shape.rows - curves.length);
  const shown = cameras.slice(0, room);
  return { cells: fitCells([...shown, ...curves], shape), shape, overflow: cameras.length - shown.length };
}

/** Columns of the smart layout at a grid width, for `cells` cells. */
export function smartColumns(gridWidth: number, cells = 0): number {
  if (cells > SMALL_CELLS) return manyColumns(gridWidth);
  return gridWidth >= WIDE_GRID_PX ? 3 : 2;
}

function manyColumns(gridWidth: number): number {
  return gridWidth >= WIDER_GRID_PX ? 4 : 3;
}

/** The grid widths the templates tell apart: a layout is redone only when the width crosses one. */
export function widthClass(gridWidth: number): number {
  if (gridWidth >= WIDER_GRID_PX) return WIDER_GRID_PX;
  return gridWidth >= WIDE_GRID_PX ? WIDE_GRID_PX : WIDE_GRID_PX - 100;
}

/** Below this width (px) a cell's tools show icons only (design doc 19 §2.2). */
export const NARROW_CELL_PX = 280;

/** A cell's width in a grid of `cols` columns. */
export function cellWidth(gridWidth: number, cols: number): number {
  return (gridWidth - CELL_GAP_PX * (cols - 1)) / Math.max(1, cols);
}

/** A cell's height: about two thirds of its width, 160–420 px (design doc 18 §5.2). */
export function cellHeight(gridWidth: number, cols: number): number {
  return Math.max(160, Math.min(420, Math.round(cellWidth(gridWidth, cols) * 0.66)));
}

/** Resizes the grid keeping the arranged cells in order (empty ones dropped first). */
export function resizeCells(cells: readonly CellContent[], shape: GridShape): CellContent[] {
  return fitCells(
    cells.filter((c) => c.kind !== 'empty'),
    shape,
  );
}

/** Whether a cell still names something the model has (after the model changed). */
export function cellValid(cell: CellContent, model: Pick<VizDataset, 'cameras' | 'streams'>): boolean {
  if (cell.kind === 'video') return model.cameras.some((c) => c.key === cell.key);
  if (cell.kind === 'curve') return model.streams.some((s) => s.key === cell.key);
  return true;
}

// ---------------------------------------------------------------- the mini player (design doc 18 §4.6)

/** Modules whose findings are about a picture: the camera in scope and another one. */
const PICTURE_MODULES = new Set(['visual_quality', 'camera_defects', 'data_integrity']);
/** Modules whose findings are about motion: the camera in scope and the arm's curves. */
const MOTION_MODULES = new Set(['video_action_sync', 'eef_video_consistency', 'motion_quality', 'kinematic_limits', 'timestamp_check']);

/** A finding's scope (C2 2.0): cameras by their short name, an arm. */
export interface FindingScope {
  camera?: string;
  cameras?: string[];
  arm?: string;
}

/** The model's camera a scope names (`wrist` is `observation.images.wrist`, an mcap topic's last words). */
export function cameraOfScope(scope: FindingScope | null | undefined, cameras: Pick<VizDataset, 'cameras'>['cameras']): string | null {
  const names = [scope?.camera, ...(scope?.cameras ?? [])].filter((n): n is string => !!n);
  for (const n of names) {
    const exact = cameras.find((c) => c.key === n || c.name === n || c.source === n);
    if (exact) return exact.key;
    const tail = cameras.find((c) => c.key.endsWith(n) || c.source.endsWith(`.${n}`) || c.source.endsWith(`/${n}`));
    if (tail) return tail.key;
  }
  return null;
}

/** The curve group of an arm (`left`, `right`) or, without one, the first smart group. */
export function armStream(scope: FindingScope | null | undefined, streams: Pick<VizDataset, 'streams'>['streams']): string | null {
  const drawable = streams.filter((s) => s.kind === 'series' && s.available);
  const arm = scope?.arm?.toLowerCase();
  if (arm) {
    const hit = drawable.find((s) => s.key.toLowerCase().includes(arm) || s.name.toLowerCase().includes(arm));
    if (hit) return hit.key;
  }
  return (drawable.find((s) => s.smart) ?? drawable[0])?.key ?? null;
}

/**
 * The mini player's cells for a finding of `module` (design doc 18 §4.6): a picture finding shows the
 * camera in scope and another camera; a motion finding the camera and the arm's curves; anything else
 * (and no finding) every camera, at most three in a row.
 */
export function miniLayout(module: string | null, scope: FindingScope | null | undefined, model: Pick<VizDataset, 'cameras' | 'streams'>): { cells: CellContent[]; shape: GridShape } {
  const keys = model.cameras.map((c) => c.key);
  const scoped = cameraOfScope(scope, model.cameras) ?? keys[0] ?? null;
  let cells: CellContent[];
  if (module && PICTURE_MODULES.has(module) && scoped) {
    const other = keys.find((k) => k !== scoped);
    cells = [scoped, other].filter((k): k is string => !!k).map((key) => ({ kind: 'video', key }));
  } else if (module && MOTION_MODULES.has(module) && (scoped || armStream(scope, model.streams))) {
    const curve = armStream(scope, model.streams);
    cells = [...(scoped ? [{ kind: 'video' as const, key: scoped }] : []), ...(curve ? [{ kind: 'curve' as const, key: curve }] : [])];
  } else {
    cells = keys.slice(0, MINI_MAX_CELLS).map((key) => ({ kind: 'video', key }));
    if (!cells.length) {
      const curve = armStream(scope, model.streams);
      if (curve) cells = [{ kind: 'curve', key: curve }];
    }
  }
  const shape = { cols: Math.max(1, Math.min(MINI_MAX_CELLS, cells.length)), rows: 1 };
  return { cells: fitCells(cells, shape), shape };
}
