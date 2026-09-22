// Test-only: the frozen C4 contract as a JSON Schema validator (Ajv, draft 2020-12).
// docs/contracts/openapi.yaml is loaded as is; its external $refs to ./cli/*.schema.json resolve
// because every file gets an $id under one base URL.
//
// KNOWN_CONTRACT_DEFECTS: three C4 1.1.0 schemas compose a *closed* base (additionalProperties:
// false) with extra members through allOf, which JSON Schema makes unsatisfiable (the base
// rejects the extra keys). We validate against a copy with exactly those compositions merged
// into one closed object, only where the contract still has them, and a test asserts each defect
// is still there until the revision that fixes it (C4 1.2.0 fixed Task.vlm, 1.3.0 the other two), so the patch goes
// the day the contract is fixed. Everything else is validated verbatim.
import { readFileSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { Ajv2020, type ErrorObject, type ValidateFunction } from 'ajv/dist/2020.js';
import addFormatsModule from 'ajv-formats';
import { parse } from 'yaml';

// Tests run from frontend/ (npm test); the contracts live next to it in docs/contracts.
// CURATOR_CONTRACTS points at another copy, e.g. to try a contract revision before it lands.
const CONTRACTS = `${process.env.CURATOR_CONTRACTS ?? resolve(process.cwd(), '..', 'docs', 'contracts')}/`;
const BASE = 'https://curator.contracts/';

type Json = Record<string, unknown>;

export function loadOpenApi(): Json {
  return parse(readFileSync(`${CONTRACTS}openapi.yaml`, 'utf8')) as Json;
}

function loadCliSchemas(): Record<string, Json> {
  const out: Record<string, Json> = {};
  for (const f of readdirSync(`${CONTRACTS}cli`)) {
    if (f.endsWith('.schema.json')) out[f] = JSON.parse(readFileSync(`${CONTRACTS}cli/${f}`, 'utf8')) as Json;
  }
  return out;
}

export interface ContractDefect {
  id: 'DatasetDetail' | 'Decision' | 'Task.vlm';
  what: string;
  /** The C4 revision that fixed it; the test then expects the unpatched schema to work. */
  fixedIn?: string;
}

export const KNOWN_CONTRACT_DEFECTS: readonly ContractDefect[] = [
  { id: 'DatasetDetail', what: 'components.schemas.DatasetDetail (allOf DatasetItem[closed] + extra required fields)', fixedIn: '1.3.0' },
  { id: 'Decision', what: 'components.schemas.Decision (allOf DecisionInput[closed] + id/decided_by/decided_at/applied)', fixedIn: '1.3.0' },
  { id: 'Task.vlm', what: 'components.schemas.Task.properties.vlm (allOf VlmChoice[closed] + snapshot)', fixedIn: '1.2.0' },
];

/** The contract revision (info.version). */
export function contractVersion(doc: Json = loadOpenApi()): string {
  return String((doc.info as Json | undefined)?.version ?? '0');
}

/** a < b for dotted versions. */
export function versionBefore(a: string, b: string): boolean {
  const x = a.split('.').map(Number);
  const y = b.split('.').map(Number);
  for (let i = 0; i < Math.max(x.length, y.length); i += 1) {
    if ((x[i] ?? 0) !== (y[i] ?? 0)) return (x[i] ?? 0) < (y[i] ?? 0);
  }
  return false;
}

function merge(doc: Json, parts: Json[]): Json {
  const props: Json = {};
  const required = new Set<string>();
  for (const p of parts) {
    const ref = typeof p.$ref === 'string' ? (p.$ref as string) : null;
    const s = ref ? (((doc.components as Json).schemas as Json)[ref.replace('#/components/schemas/', '')] as Json) : p;
    Object.assign(props, (s.properties as Json | undefined) ?? {});
    for (const r of (s.required as string[] | undefined) ?? []) required.add(r);
  }
  return { type: 'object', additionalProperties: false, required: [...required], properties: props };
}

/** The contract with the known unsatisfiable compositions merged, where it still has them (see header). */
export function patchedOpenApi(): Json {
  const doc = structuredClone(loadOpenApi());
  const schemas = (doc.components as Json).schemas as Json;
  const version = contractVersion(doc);
  const open = (id: ContractDefect['id']) => {
    const d = KNOWN_CONTRACT_DEFECTS.find((x) => x.id === id);
    return !d?.fixedIn || versionBefore(version, d.fixedIn);
  };
  for (const name of ['DatasetDetail', 'Decision'] as const) {
    const allOf = (schemas[name] as Json).allOf;
    if (open(name) && Array.isArray(allOf)) schemas[name] = merge(doc, allOf as Json[]);
  }
  const task = schemas.Task as Json;
  const vlm = (task.properties as Json).vlm as Json;
  const branches = vlm.oneOf as Json[] | undefined;
  const composed = branches?.[1]?.allOf;
  if (branches && Array.isArray(composed)) (task.properties as Json).vlm = { oneOf: [branches[0], merge(doc, composed as Json[])] };
  return doc;
}

const addFormats = addFormatsModule as unknown as (ajv: Ajv2020) => Ajv2020;

export interface Contract {
  ajv: Ajv2020;
  validator(ref: string): ValidateFunction;
  schemaRef(name: string): string;
  responseRef(operationId: string, status: number): string | null;
  requestRef(operationId: string): string | null;
  operations(): { operationId: string; path: string; method: string }[];
}

export function makeContract(doc: Json = patchedOpenApi()): Contract {
  const ajv = new Ajv2020({ strict: false, allErrors: true });
  addFormats(ajv);
  for (const [file, schema] of Object.entries(loadCliSchemas())) ajv.addSchema({ ...schema, $id: `${BASE}cli/${file}` });
  ajv.addSchema({ ...doc, $id: `${BASE}openapi.yaml` });
  const ops: { operationId: string; path: string; method: string }[] = [];
  for (const [path, item] of Object.entries(doc.paths as Json)) {
    for (const [method, op] of Object.entries(item as Json)) {
      if (op && typeof op === 'object' && typeof (op as Json).operationId === 'string') ops.push({ operationId: (op as Json).operationId as string, path, method });
    }
  }
  const ptr = (s: string) => s.replace(/~/g, '~0').replace(/\//g, '~1');
  const cache = new Map<string, ValidateFunction>();
  return {
    ajv,
    validator(ref) {
      if (!cache.has(ref)) {
        const v = ajv.getSchema(ref);
        if (!v) throw new Error(`no schema at ${ref}`);
        cache.set(ref, v);
      }
      return cache.get(ref)!;
    },
    schemaRef: (name) => `${BASE}openapi.yaml#/components/schemas/${name}`,
    responseRef(operationId, status) {
      const op = ops.find((o) => o.operationId === operationId);
      if (!op) throw new Error(`unknown operation ${operationId}`);
      const responses = (((doc.paths as Json)[op.path] as Json)[op.method] as Json).responses as Json;
      const key = String(status) in responses ? String(status) : 'default';
      const res = responses[key] as Json | undefined;
      if (!res) return null;
      if (typeof res.$ref === 'string') return `${BASE}openapi.yaml${res.$ref}/content/application~1json/schema`;
      const content = res.content as Json | undefined;
      if (!content?.['application/json']) return null;
      return `${BASE}openapi.yaml#/paths/${ptr(op.path)}/${op.method}/responses/${key}/content/application~1json/schema`;
    },
    requestRef(operationId) {
      const op = ops.find((o) => o.operationId === operationId);
      if (!op) throw new Error(`unknown operation ${operationId}`);
      const rb = (((doc.paths as Json)[op.path] as Json)[op.method] as Json).requestBody as Json | undefined;
      if (!rb) return null;
      return `${BASE}openapi.yaml#/paths/${ptr(op.path)}/${op.method}/requestBody/content/application~1json/schema`;
    },
    operations: () => ops,
  };
}

export function formatErrors(errors: ErrorObject[] | null | undefined): string {
  return (errors ?? []).map((e) => `${e.instancePath || '/'} ${e.message ?? ''} ${JSON.stringify(e.params)}`).join('\n');
}
