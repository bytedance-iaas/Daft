// Shared by gen-api.mjs and check-api-types.mjs: turn the frozen C4 contract into TypeScript.
//
// openapi-typescript resolves the external $refs to ../docs/contracts/cli/*.schema.json, but it
// also emits the JSON Schema `$defs` blocks of those files as if they were object properties
// (e.g. a required `$defs` key on the preflight result). The payloads never carry `$defs`, and
// the definitions themselves are already hoisted into components.schemas, so we strip them.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import openapiTS, { astToString } from 'openapi-typescript';

export const CONTRACT = fileURLToPath(new URL('../../docs/contracts/openapi.yaml', import.meta.url));
export const OUTPUT = fileURLToPath(new URL('../src/api/schema.d.ts', import.meta.url));

const HEADER = `/**
 * Generated from docs/contracts/openapi.yaml (C4) by \`npm run gen:api\`. Do not edit by hand.
 * The contract is the only source of request and response shapes; CI fails when this file is stale.
 */

`;

/** Remove every `$defs: { ... };` member and the stray top-level `$defs` type alias. */
export function stripDefs(source) {
  const lines = source.split('\n');
  const out = [];
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^export type \$defs = /.test(line)) continue;
    const m = /^(\s*)\$defs: \{\s*$/.exec(line);
    if (!m) {
      out.push(line);
      continue;
    }
    // Skip until the closing brace at the same indentation.
    const indent = m[1];
    let j = i + 1;
    while (j < lines.length && !lines[j].startsWith(`${indent}};`)) j += 1;
    i = j;
  }
  return out.join('\n');
}

export async function generate() {
  const ast = await openapiTS(new URL(`file://${CONTRACT}`), { emptyObjectsUnknown: true });
  return HEADER + stripDefs(astToString(ast));
}

export function readCommitted() {
  try {
    return readFileSync(OUTPUT, 'utf8');
  } catch {
    return '';
  }
}
