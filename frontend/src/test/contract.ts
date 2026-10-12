// Test-only: the frozen C4 contract as a JSON Schema validator (Ajv, draft 2020-12).
// docs/contracts/openapi.yaml is loaded as is and validated verbatim; its external $refs to
// ./cli/*.schema.json resolve because every file gets an $id under one base URL.
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

/** The JSON Schemas next to openapi.yaml it refers to (C7 viz-mapping.schema.json). */
function loadTopSchemas(): Record<string, Json> {
  const out: Record<string, Json> = {};
  for (const f of readdirSync(CONTRACTS)) {
    if (f.endsWith('.schema.json')) out[f] = JSON.parse(readFileSync(`${CONTRACTS}${f}`, 'utf8')) as Json;
  }
  return out;
}

const addFormats = addFormatsModule as unknown as (ajv: Ajv2020) => Ajv2020;

export interface Contract {
  ajv: Ajv2020;
  validator(ref: string): ValidateFunction;
  schemaRef(name: string): string;
  responseRef(operationId: string, status: number): string | null;
  /** The media types a response of that status declares (binary routes: video/mp4 ...). */
  responseTypes(operationId: string, status: number): string[];
  requestRef(operationId: string): string | null;
  operations(): { operationId: string; path: string; method: string }[];
}

export function makeContract(doc: Json = loadOpenApi()): Contract {
  const ajv = new Ajv2020({ strict: false, allErrors: true });
  addFormats(ajv);
  for (const [file, schema] of Object.entries(loadCliSchemas())) ajv.addSchema({ ...schema, $id: `${BASE}cli/${file}` });
  for (const [file, schema] of Object.entries(loadTopSchemas())) ajv.addSchema({ ...schema, $id: `${BASE}${file}` });
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
    responseTypes(operationId, status) {
      const op = ops.find((o) => o.operationId === operationId);
      if (!op) throw new Error(`unknown operation ${operationId}`);
      const responses = (((doc.paths as Json)[op.path] as Json)[op.method] as Json).responses as Json;
      const key = String(status) in responses ? String(status) : 'default';
      let res = responses[key] as Json | undefined;
      if (res && typeof res.$ref === 'string') res = ((doc.components as Json).responses as Json)[(res.$ref as string).replace('#/components/responses/', '')] as Json;
      return Object.keys((res?.content as Json | undefined) ?? {});
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
