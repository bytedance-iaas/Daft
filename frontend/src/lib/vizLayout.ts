// The player's grid (design doc 18 §5.1–§5.3): layout templates, the smart rule and cell sizes.
// Pure functions over the presentation model; the player keeps the cells in its state.
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
];

export const MAX_COLS = 3;
export const MAX_ROWS = 3;
/** The smart layout uses three columns from this grid width (px) on, two below. */
export const WIDE_GRID_PX = 1000;
/** Curve groups the smart layout adds after the cameras. */
export const SMART_CURVES = 2;
export const CELL_GAP_PX = 10;

export function gridKey(g: GridShape): string {
  return `${g.cols}x${g.rows}`;
}

export function parseGridKey(key: string): GridShape | null {
  const m = /^([1-3])x([1-3])$/.exec(key);
  return m ? { cols: Number(m[1]), rows: Number(m[2]) } : null;
}

/** The smallest grid that holds n cells with at most `cols` columns (at least one cell). */
export function shapeFor(n: number, cols: number): GridShape {
  const c = Math.max(1, Math.min(cols, MAX_COLS, Math.max(1, n)));
  return { cols: c, rows: Math.max(1, Math.min(MAX_ROWS, Math.ceil(Math.max(1, n) / c))) };
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
 * groups, three columns on a wide grid and two otherwise (it follows the width as side panels open
 * and close); video / curve: only those. Custom keeps whatever the user arranged (null here).
 */
export function templateLayout(
  template: LayoutTemplate,
  model: Pick<VizDataset, 'cameras' | 'streams'>,
  gridWidth: number,
): { cells: CellContent[]; shape: GridShape } | null {
  let cells: CellContent[];
  let cols: number;
  if (template === 'video') {
    cells = cameraCells(model);
    cols = Math.min(MAX_COLS, Math.max(1, cells.length));
  } else if (template === 'curve') {
    cells = curveCells(model, false);
    cols = cells.length > 1 ? 2 : 1;
  } else if (template === 'smart') {
    cells = [...cameraCells(model), ...curveCells(model, true).slice(0, SMART_CURVES)];
    cols = smartColumns(gridWidth);
  } else {
    return null;
  }
  const shape = shapeFor(cells.length, cols);
  return { cells: fitCells(cells, shape), shape };
}

/** Columns of the smart layout at a grid width. */
export function smartColumns(gridWidth: number): number {
  return gridWidth >= WIDE_GRID_PX ? 3 : 2;
}

/** A cell's height: about two thirds of its width, 160–420 px (design doc 18 §5.2). */
export function cellHeight(gridWidth: number, cols: number): number {
  const w = (gridWidth - CELL_GAP_PX * (cols - 1)) / Math.max(1, cols);
  return Math.max(160, Math.min(420, Math.round(w * 0.66)));
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
    cells = keys.slice(0, MAX_COLS).map((key) => ({ kind: 'video', key }));
    if (!cells.length) {
      const curve = armStream(scope, model.streams);
      if (curve) cells = [{ kind: 'curve', key: curve }];
    }
  }
  const shape = { cols: Math.max(1, Math.min(MAX_COLS, cells.length)), rows: 1 };
  return { cells: fitCells(cells, shape), shape };
}
