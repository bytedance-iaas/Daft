// The dataset declaration of the mock world (design doc 25 §3, C4 5.0.0): a draft from each dataset's profile as
// the Daemon makes it (cameras by name, the pose of a robot it knows, its official tool assumed, no intrinsics), the
// confirmed versions, the check on confirming (unknown cameras and columns are errors; an odd opening a suspect).
// Simplification: the version counter is the declaration's own (the Daemon shares it with the mcap mapping).
import type { DatasetDeclaration, DatasetDeclarationInfo, DatasetDetail, Declaration, DeclarationNote } from '../api/types';
import { canGenerate, missingOf, shortName } from '../lib/declaration';
import { db } from './db';
import { vizFormatOf } from './vizWorld';

const WRIST = ['wrist', 'hand'];
const EXTERNAL = ['exterior', 'external', 'front', 'side', 'top', 'third', 'overhead', 'table'];
const MOVING = ['head', 'chest', 'body'];

function mountOf(source: string): { mount: 'fixed_external' | 'wrist' | 'moving'; word: string } | null {
  const s = source.toLowerCase();
  for (const [mount, words] of [['wrist', WRIST], ['fixed_external', EXTERNAL], ['moving', MOVING]] as const) {
    const w = words.find((x) => s.includes(x));
    if (w) return { mount, word: w };
  }
  return null;
}

export function camerasOf(d: DatasetDetail): DatasetDeclaration['cameras'] {
  const pf = d.preflight?.dataset;
  if (vizFormatOf(d) === 'mcap') {
    const m = db.vizMappings.get(d.id)?.mapping;
    return (m?.cameras ?? []).map((c) => ({ source: c.topic, name: c.name, width: 640, height: 480 }));
  }
  const infos = new Map((pf?.camera_info ?? []).map((c) => [c.name, c]));
  return (pf?.cameras ?? []).map((cam) => ({
    source: `observation.images.${cam}`,
    name: cam,
    width: infos.get(cam)?.width ?? 640,
    height: infos.get(cam)?.height ?? 480,
  }));
}

/** The draft of a dataset: what its names and robot type say (design doc 25 §3.3). */
export function draftOf(d: DatasetDetail): { declaration: Declaration; unresolved: DeclarationNote[] } {
  const robot = String(d.robot_type ?? '').toLowerCase();
  const franka = robot.includes('franka') || robot.includes('panda');
  const droid = d.name.startsWith('eef_ds2') || d.name.startsWith('droid');
  const unresolved: DeclarationNote[] = [];
  const cams: NonNullable<NonNullable<Declaration['calibration']>['cameras']> = {};
  for (const c of camerasOf(d)) {
    const m = mountOf(c.source);
    if (!m) {
      unresolved.push({ field: `calibration.cameras.${c.source}.mount`, code: 'mount_unknown' });
      continue;
    }
    const ext = droid && m.mount === 'fixed_external' ? { mode: 'column' as const, key: `camera_extrinsics.${shortName(c.source)}`, layout: 'xyz_rpy' as const, assurance: 'declared' as const } : null;
    cams[c.source] = {
      camera_id: shortName(c.source),
      mount: m.mount,
      ...(m.mount === 'wrist' ? { owner: 'arm' } : {}),
      intrinsics: null,
      extrinsics: ext,
      media_transform: 'identity',
      assurance: 'model_assumed',
      assumptions: [{ code: 'mount_from_keyword', args: { word: m.word } }, ...(ext ? [{ code: 'extrinsics_column_by_name', args: { key: ext.key } }] : [])],
    };
    unresolved.push({ field: `calibration.cameras.${c.source}.intrinsics`, code: 'intrinsics_missing' });
  }
  const pose = droid
    ? {
        key: 'observation.state.cartesian_position',
        layout: 'xyz_rpy_xyz_extrinsic' as const,
        units: { position: 'm' as const, angle: 'rad' as const },
        frame_id: franka ? 'panda_link8' : null,
        reference_frame: 'robot_base',
        pose_type: 'absolute' as const,
        assurance: 'model_assumed' as const,
        assumptions: [{ code: 'euler_extrinsic_xyz' }, { code: 'units_by_convention' }, ...(franka ? [{ code: 'pose_frame_by_robot', args: { frame: 'panda_link8' } }] : [])],
      }
    : null;
  const declaration: Declaration = {
    schema_version: 'dataset-declaration/1.0',
    base: vizFormatOf(d) === 'mcap' ? (db.vizMappings.get(d.id)?.mapping.base ?? null) : 'builtin:lerobot',
    ...(vizFormatOf(d) === 'mcap' && db.vizMappings.get(d.id) ? { ...db.vizMappings.get(d.id)!.mapping, schema_version: 'dataset-declaration/1.0' as const } : {}),
    semantics: {
      pose,
      joints: droid && franka ? { key: 'observation.state.joint_position', units: 'rad', robot: 'franka_panda', reference_frame: 'robot_base', assurance: 'model_assumed', assumptions: [{ code: 'robot_from_robot_type', args: { robot_type: d.robot_type } }] } : null,
      gripper: droid ? { key: 'observation.state.gripper_position', closed_fraction: 'identity', assurance: 'model_assumed', assumptions: [{ code: 'closed_fraction_identity' }] } : null,
    },
    calibration: {
      cameras: cams,
      tool: franka
        ? { model: 'franka_hand', tcp_offset_m: [0, 0, 0.1034], finger_axis: 'local_y', max_opening_m: 0.08, axes: { from: 'tcp', length_m: 0.06 }, assurance: 'model_assumed', assumptions: [{ code: 'tool_from_robot_type', args: { robot_type: d.robot_type } }] }
        : null,
      handheld: null,
    },
    timing: null,
  };
  return { declaration, unresolved };
}

export function declarationInfoOf(id: string): DatasetDeclarationInfo {
  const stored = db.declarations.get(id);
  if (!stored) return { state: 'none', version: 0, updated_at: null, name: null, layers: [], assumed: 0, suspects: 0 };
  const doc = stored.doc;
  const assumed = [doc.semantics?.pose, doc.semantics?.joints, doc.semantics?.gripper, doc.calibration?.tool, ...Object.values(doc.calibration?.cameras ?? {})].filter(
    (x) => x && 'assurance' in x && x.assurance === 'model_assumed',
  ).length;
  return {
    state: 'confirmed',
    version: stored.version,
    updated_at: stored.updatedAt,
    name: doc.name ?? null,
    layers: [...(doc.cameras ? (['sources'] as const) : []), ...(doc.semantics || doc.calibration ? (['semantics', 'calibration'] as const) : [])],
    assumed,
    suspects: doc.suspects?.length ?? 0,
  };
}

function trajectoryOf(d: DatasetDetail, doc: Declaration, drafted: boolean): DatasetDeclaration['trajectory'] {
  const sources = camerasOf(d).map((c) => c.source);
  if (!doc.semantics?.pose) return { kind: 'missing_pose' };
  if (canGenerate(doc, sources) && !drafted) return { kind: 'generate' };
  const missing = canGenerate(doc, sources) ? [{ field: '<declaration>', code: 'declaration_unconfirmed' }] : missingOf(doc, sources);
  return { kind: 'missing_declaration', missing };
}

export function declarationDoc(d: DatasetDetail): DatasetDeclaration {
  const stored = db.declarations.get(d.id);
  const draft = draftOf(d);
  const fmt = vizFormatOf(d);
  return {
    dataset_id: d.id,
    format: fmt === 'mcap' ? 'mcap' : fmt === 'lance' ? 'lance' : fmt === 'umi_session' ? 'umi_session' : 'lerobot',
    state: stored ? 'confirmed' : 'none',
    declaration: stored?.doc ?? null,
    version: stored?.version ?? 0,
    updated_at: stored?.updatedAt ?? null,
    draft: stored?.doc ?? draft.declaration,
    unresolved: stored ? [] : draft.unresolved,
    cameras: camerasOf(d),
    trajectory: trajectoryOf(d, stored?.doc ?? draft.declaration, !stored),
    assumed: [],
    suspects: stored?.doc.suspects ?? [],
    warnings: [],
  };
}

/** The check on confirming: errors per field, suspects kept on the declaration. */
export function checkDeclaration(d: DatasetDetail, doc: Declaration): { errors: { field: string; problem: string }[]; doc: Declaration } {
  const errors: { field: string; problem: string }[] = [];
  const sources = new Set(camerasOf(d).map((c) => c.source));
  for (const src of Object.keys(doc.calibration?.cameras ?? {})) {
    if (!sources.has(src)) errors.push({ field: `declaration.calibration.cameras.${src}`, problem: `数据集里没有相机 ${src}` });
  }
  const pose = doc.semantics?.pose;
  if (pose && 'key' in pose && pose.key && !pose.key.startsWith('observation.')) errors.push({ field: 'declaration.semantics.pose.key', problem: `数据集里没有列 ${pose.key}` });
  const suspects: NonNullable<Declaration['suspects']> = [];
  const op = doc.calibration?.tool?.max_opening_m;
  if (typeof op === 'number' && (op <= 0 || op > 0.2)) suspects.push({ code: 'opening_range', field: 'calibration.tool.max_opening_m', message: `最大开口 ${op} m 不在 0–0.2 m 之间` });
  const out: Declaration = { ...doc };
  if (suspects.length) out.suspects = suspects;
  else delete out.suspects;
  return { errors, doc: out };
}
