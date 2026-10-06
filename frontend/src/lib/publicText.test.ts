import { writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { bundle, createConfig } from '@redocly/openapi-core';
import { describe, expect, it } from 'vitest';
import { publicContract, publicText } from './publicText';

// Same pattern as backend/tests/contracts/test_openapi.py INTERNAL_REF.
const INTERNAL_REF =
  /\b(?:C[1-7]|D\d{1,2}|P\d{1,2}|F\d{1,2}(?:\.\d+)*|W\d{1,2}[ab]?)\b|\b(?:design )?docs? \d{2}\b|registry \d+\.\d+|§\s?\d|frozen contract/i;

type Json = Record<string, unknown>;

async function bundled(): Promise<Json> {
  const config = await createConfig({});
  const ref = resolve(process.cwd(), '..', 'docs', 'contracts', 'openapi.yaml');
  return (await bundle({ ref, config, dereference: false })).bundle.parsed as Json;
}

/** Every [pointer, text] under a description / summary / title key, outside example data. */
function texts(node: unknown, ptr = ''): [string, string][] {
  if (Array.isArray(node)) return node.flatMap((v, i) => texts(v, `${ptr}/${i}`));
  if (!node || typeof node !== 'object') return [];
  return Object.entries(node).flatMap(([k, v]): [string, string][] => {
    if (['examples', 'example', 'default', 'enum', 'const'].includes(k)) return [];
    if (['description', 'summary', 'title'].includes(k) && typeof v === 'string') return [[`${ptr}/${k}`, v]];
    return texts(v, `${ptr}/${k}`);
  });
}

describe('publicText', () => {
  it('drops a parenthetical that only cites', () => {
    expect(publicText('Task list, page-number pagination (D21), newest first')).toBe('Task list, page-number pagination, newest first');
    expect(publicText('Save an access key; verifies identity only (D30, doc 08 §4)')).toBe('Save an access key; verifies identity only');
    expect(publicText('The module registry (C1); the list comes only from here')).toBe('The module registry; the list comes only from here');
    expect(publicText('the presentation model (design doc 18 §4.0)')).toBe('the presentation model');
    expect(publicText('the plan (design doc 04, section 3).')).toBe('the plan.');
    expect(publicText('does not filter (1.15)')).toBe('does not filter');
    expect(publicText('a field (2.5.0, design doc 19 §4.3)')).toBe('a field');
    expect(publicText('shown (before F5.6)')).toBe('shown');
  });

  it('keeps the rest of a parenthetical that also says something', () => {
    expect(publicText('skipped (D40, as v1 does): not checked')).toBe('skipped (as v1 does): not checked');
    expect(publicText('the coverage (design doc 17 §5.2, the coverage matrix)')).toBe('the coverage (the coverage matrix)');
    expect(publicText('fields (a path, H.265 cameras; design doc 18 §6.2) and')).toBe('fields (a path, H.265 cameras) and');
    expect(publicText('the mapping (design doc 18 §6.2: role=state -> state, cameras -> video_topics)')).toBe(
      'the mapping (role=state -> state, cameras -> video_topics)',
    );
    expect(publicText('drafts (design doc 12 §8.7, D-E17: EEF drafts record_mapping)')).toBe('drafts (EEF drafts record_mapping)');
  });

  it('leaves parentheticals without references exactly as they were', () => {
    const text = 'kinds (2.0: finding, human; 1.0: hard_gate, duplicate) and `x` (409 when in use)';
    expect(publicText(text)).toBe(text);
  });

  it('drops a first sentence that only cites, and a reference that labels the text', () => {
    expect(publicText('Design doc 15 §2. For `source: tos` registrations only')).toBe('For `source: tos` registrations only');
    expect(publicText('Design doc 02, section 3.1; availability rules in doc 05, section 4. Reads metadata only.')).toBe(
      'Reads metadata only.',
    );
    expect(publicText('C1 2.0 (design doc 17 §2): where every module runs. The console takes it.')).toBe(
      'Where every module runs. The console takes it.',
    );
    expect(publicText('2.5.0 (design doc 19 §3): an mcap sample pack')).toBe('An mcap sample pack');
    expect(publicText('D26')).toBe('');
  });

  it('rewrites the references that sit inside running prose', () => {
    expect(publicText('shaped like C2 final-list reasons')).toBe('shaped like final-list reasons');
    expect(publicText('Confirm a new version (validated against C7 and the topics)')).toBe(
      'Confirm a new version (validated against the mapping schema and the topics)',
    );
    expect(publicText('lower (P4, D54). Plans made before D54 may say site.')).toBe('lower. Older plans may say site.');
    expect(publicText("Create a task - the requirement's run_modules() (D5)")).toBe('Create a task');
  });
});

describe('the published contract', () => {
  it('names nothing internal, and changes only texts', async () => {
    const source = await bundled();
    const published = publicContract(source);
    const left = texts(published).filter(([, t]) => INTERNAL_REF.test(t));
    expect(left.map(([p, t]) => `${p}: ${INTERNAL_REF.exec(t)?.[0]}`)).toEqual([]);

    // everything but the texts is the same, examples included
    const strip = (doc: unknown): unknown =>
      JSON.parse(JSON.stringify(doc, (k, v) => (['description', 'summary', 'title'].includes(k) && typeof v === 'string' ? undefined : v)));
    expect(strip(published)).toEqual(strip(source));

    if (process.env.DUMP_PUBLIC_TEXT) {
      const before = new Map(texts(source));
      const after = new Map(texts(published));
      const changed = [...before].filter(([p, t]) => after.get(p) !== t).map(([p, t]) => ({ p, before: t, after: after.get(p) ?? null }));
      writeFileSync(process.env.DUMP_PUBLIC_TEXT, JSON.stringify(changed, null, 1));
    }
  });
});
