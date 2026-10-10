/**
 * The dataset declaration's form logic (design doc 25 §3, C7 dataset-declaration/1.0): immutable edits of the
 * document, the camera rows of the table, and - as the Daemon judges it - whether the platform can generate a robot
 * arm's trajectory from it (design doc 25 §4.1). Nothing here talks to the API.
 */
import type { Assurance, CameraCalibration, Declaration, DeclarationNote, Extrinsics, Intrinsics, ToolModel } from '../api/types';

export const SCHEMA_VERSION = 'dataset-declaration/1.0';
export const MOUNTS = ['fixed_external', 'wrist', 'moving'] as const;
export type Mount = (typeof MOUNTS)[number];
export const LAYOUTS = ['xyz_rpy_xyz_extrinsic', 'xyz_quat_xyzw', 'xyz_quat_wxyz', 'xyz_rotmat', 'xyz_rot6d'] as const;
export const MODELS = ['pinhole', 'opencv_brown', 'opencv_fisheye'] as const;
export const FINGER_AXES = ['local_x', 'local_y', 'local_z'] as const;

/** A video key / topic -> its short name (observation.images.exterior_1_left -> exterior_1_left). */
export function shortName(source: string): string {
  if (source.startsWith('observation.images.')) return source.slice('observation.images.'.length);
  if (source.startsWith('/'))
    return (
      source
        .replace(/^\/+/, '')
        .split('/')
        .filter((p) => !['compressed', 'image_raw', 'image', 'sensor', 'rgb', 'color'].includes(p))
        .join('_') || source
    );
  return source.split('.').pop() ?? source;
}

/** A deep copy that keeps the type (the form edits copies, never the query's data). */
export function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}

export function emptyDeclaration(): Declaration {
  return { schema_version: SCHEMA_VERSION };
}

/** The declaration with ``patch`` laid on its calibration of ``source`` (a missing entry is made). */
export function setCamera(doc: Declaration, source: string, patch: Partial<CameraCalibration>): Declaration {
  const out = clone(doc);
  const cal = (out.calibration ??= {});
  const cams = (cal.cameras ??= {});
  cams[source] = { ...(cams[source] ?? { mount: 'fixed_external' }), ...patch } as CameraCalibration;
  return out;
}

export const DECLARED_FIXED = 'mount_declared_fixed';

/** Whether a person declared this camera fixed though it was drafted as moving (design doc 25 §3.3). */
export function isDeclaredFixed(c: CameraCalibration | undefined): boolean {
  return Boolean(c && c.mount === 'fixed_external' && (c.assumptions ?? []).some((a) => a.code === DECLARED_FIXED));
}

/**
 * A moving camera taken as fixed (design doc 25 §3.3, 「视为固定」): a third-person camera on that assumption
 * (`model_assumed`, `mount_declared_fixed`) - it then needs fixed extrinsics like any; or back to moving, the
 * assumption gone.
 */
export function declareFixed(doc: Declaration, source: string, fixed: boolean): Declaration {
  const cam = doc.calibration?.cameras?.[source];
  const rest = (cam?.assumptions ?? []).filter((a) => a.code !== DECLARED_FIXED);
  return setCamera(
    doc,
    source,
    fixed ? { mount: 'fixed_external', assurance: 'model_assumed', assumptions: [...rest, { code: DECLARED_FIXED }] } : { mount: 'moving', assumptions: rest },
  );
}

export function setTool(doc: Declaration, patch: Partial<ToolModel> | null): Declaration {
  const out = clone(doc);
  const cal = (out.calibration ??= {});
  cal.tool = patch === null ? null : ({ ...(cal.tool ?? {}), ...patch } as ToolModel);
  return out;
}

type Semantic = 'pose' | 'joints' | 'gripper';

export function setSemantic<K extends Semantic>(doc: Declaration, key: K, value: NonNullable<Declaration['semantics']>[K] | null): Declaration {
  const out = clone(doc);
  const sem = (out.semantics ??= {});
  (sem as Record<string, unknown>)[key] = value;
  return out;
}

/** An item a person confirms: its assurance becomes ``declared`` (its assumptions stay, as what was drafted). */
export function declared<T extends { assurance?: Assurance }>(item: T, yes: boolean): T {
  return { ...item, assurance: yes ? 'declared' : 'model_assumed' };
}

/** fx, cx, fy, cy of intrinsics (K or the four numbers); null when not given. */
export function fxcxfycy(intr: Intrinsics | null | undefined): [number, number, number, number] | null {
  if (!intr) return null;
  if ('fx_cx_fy_cy' in intr && intr.fx_cx_fy_cy) return intr.fx_cx_fy_cy as [number, number, number, number];
  if ('K' in intr && intr.K) return [intr.K[0][0], intr.K[0][2], intr.K[1][1], intr.K[1][2]];
  return null;
}

/** Intrinsics from the four numbers (and what else they keep): null when any is missing or not positive. */
export function withFour(intr: Intrinsics | null | undefined, four: (number | null | undefined)[]): Intrinsics | null {
  if (four.length !== 4 || four.some((x) => typeof x !== 'number' || !Number.isFinite(x)) || (four[0] as number) <= 0 || (four[2] as number) <= 0) return null;
  const base = intr ? { ...intr } : { model: 'pinhole' as const };
  delete (base as { K?: unknown }).K;
  return { ...base, fx_cx_fy_cy: four as number[], model: base.model ?? 'pinhole' } as Intrinsics;
}

/** Why a camera cannot be drawn (as the Daemon says it, ``declared.camera_states``), or null when it can. */
export function cameraReason(c: CameraCalibration | undefined): string | null {
  if (!c) return 'mount_unknown';
  if (c.mount === 'moving') return 'moving_camera_unsupported';
  if (!c.intrinsics) return 'intrinsics_missing';
  const mode = (c.extrinsics as Extrinsics | null | undefined)?.mode;
  if (c.mount === 'fixed_external' && mode !== 'static' && mode !== 'column') return 'extrinsics_missing';
  if (c.mount === 'wrist' && mode !== 'camera_tcp') return 'camera_tcp_missing';
  return null;
}

export interface CameraRow {
  source: string;
  name: string;
  width: number | null;
  height: number | null;
  calibration: CameraCalibration | undefined;
  reason: string | null;
}

export function cameraRows(doc: Declaration | null, cameras: { source: string; name: string; width: number | null; height: number | null }[]): CameraRow[] {
  const cal = doc?.calibration?.cameras ?? {};
  return cameras.map((c) => ({ ...c, calibration: cal[c.source], reason: cameraReason(cal[c.source]) }));
}

/** What generating the trajectory still lacks (design doc 25 §4.1), the Daemon's ``declared.readiness``. */
export function missingOf(doc: Declaration | null, cameras: string[]): DeclarationNote[] {
  const pose = doc?.semantics?.pose;
  if (!pose) return [{ field: 'semantics.pose', code: 'pose_missing' }];
  const out: DeclarationNote[] = [];
  if (!pose.frame_id) out.push({ field: 'semantics.pose.frame_id', code: 'pose_frame_unknown' });
  if (!doc?.calibration?.tool) out.push({ field: 'calibration.tool', code: 'tool_missing' });
  const cal = doc?.calibration?.cameras ?? {};
  let drawable = 0;
  for (const src of cameras) {
    const why = cameraReason(cal[src]);
    if (why === null) drawable += 1;
    else if (why !== 'moving_camera_unsupported') {
      const part = why === 'mount_unknown' ? 'mount' : why === 'intrinsics_missing' ? 'intrinsics' : 'extrinsics';
      out.push({ field: `calibration.cameras.${src}.${part}`, code: why });
    }
  }
  if (!drawable) out.push({ field: 'calibration.cameras', code: 'no_drawable_camera' });
  return out;
}

/** Whether the declaration lets the platform generate the trajectory (no blocking item missing, a camera drawable). */
export function canGenerate(doc: Declaration | null, cameras: string[]): boolean {
  const m = missingOf(doc, cameras);
  return !m.some((x) => ['pose_missing', 'pose_frame_unknown', 'tool_missing', 'no_drawable_camera'].includes(x.code));
}

/** The declaration as a file (导出 JSON). */
export function declarationJson(doc: Declaration): string {
  return `${JSON.stringify(doc, null, 1)}\n`;
}

/** A file read back (从 JSON 导入): a declaration, or an mcap mapping as the declaration's first layer. */
export function parseDeclaration(text: string): Declaration {
  const doc = JSON.parse(text) as Record<string, unknown>;
  if (!doc || typeof doc !== 'object') throw new Error('not a JSON object');
  const v = doc.schema_version;
  if (v === 'viz-mapping/1.0' || v === 'viz-mapping/1.1') return { ...(doc as object), schema_version: SCHEMA_VERSION } as Declaration;
  if (v !== SCHEMA_VERSION) throw new Error(`schema_version ${String(v)}`);
  return doc as unknown as Declaration;
}

/** A template laid on the document (套用模版): its semantics and calibration replace what it has. */
export function applyTemplate(doc: Declaration, tpl: Declaration): Declaration {
  const out = clone(doc);
  if (tpl.semantics) out.semantics = { ...(out.semantics ?? {}), ...clone(tpl.semantics) };
  if (tpl.calibration) {
    const cams = { ...(out.calibration?.cameras ?? {}), ...clone(tpl.calibration.cameras ?? {}) };
    out.calibration = { ...(out.calibration ?? {}), ...clone(tpl.calibration), cameras: cams };
  }
  if (tpl.timing !== undefined) out.timing = clone(tpl.timing);
  delete out.suspects;
  return out;
}
