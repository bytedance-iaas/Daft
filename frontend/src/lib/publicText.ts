// What the build publishes as {base}/openapi.json (vite.config.ts) is read by customers and their
// agents. The contract's own texts point developers at where a rule was decided - contracts (C4),
// frozen decisions (D36), features (F12.3), design docs (design doc 18 §4.0), registry and contract
// versions - which means nothing outside the team. publicContract() takes those references out of
// every description, summary and title; docs/contracts/openapi.yaml keeps them.

const NAMED = String.raw`(?:C[1-7](?: \d+\.\d+(?:\.\d+)?)?|D(?:-E)?\d{1,2}|P\d{1,2}|F\d{1,2}(?:\.\d+)*|W\d{1,2}[ab]?|(?:[Dd]esign )?docs? \d{2}(?:(?:,| and) \d{2})*(?:,? (?:§ ?|section )[\d.]*\d)?|§ ?[\d.]*\d|section [\d.]*\d|registry \d+\.\d+|docs/contracts/[\w./-]+|(?:before|since|after) F\d{1,2}(?:\.\d+)*)`;
const VERSION = String.raw`\d+\.\d+(?:\.\d+)?`;

/** A whole list item that is only a reference: `D36`, `design doc 17 §1.2`, `2.5.0`, `before F5.6`. */
const ONLY_REF = new RegExp(`^(?:${NAMED}|${VERSION})$`);
/** A reference labelling what follows it: `design doc 18 §6.2: role=action ...`, `D-E17: ...`. */
const REF_LABEL = new RegExp(`^${NAMED}: `);
/** The same at the start of a text, where a contract version may label it too: `2.5.0: an mcap ...`. */
const LEADING_LABEL = new RegExp(String.raw`^(?:${NAMED}|\d+\.\d+\.\d+): `);
/** A first sentence that only cites: `Design doc 15 §2. For ...`. */
const CITING_SENTENCE = new RegExp(String.raw`^${NAMED}.*?\.\s+(?=[A-Z\x60])`);

/** References inside running prose, rewritten one by one. */
const PROSE: readonly [RegExp, string][] = [
  [/\bC2 (final-list|source-manifest)\b/g, '$1'],
  [/\bvalidated against C7\b/g, 'validated against the mapping schema'],
  [/\ba document C2 2\.0 changed\b/g, 'a document changed in format 2.0'],
  [/\btasks made before C2 2\.0\b/g, 'tasks made before format 2.0'],
  [/\bPlans made before D54\b/g, 'Older plans'],
  [/ since F\d{1,2}(?:\.\d+)*/g, ''],
  [/ - the requirement's run_modules\(\)/g, ''],
];

/** One parenthetical's content without its reference items; null when nothing is left. */
function stripItems(content: string): string | null {
  const parts = content.split(/(, |; )/);
  const kept: string[] = [];
  let changed = false;
  for (let i = 0; i < parts.length; i += 2) {
    let item = parts[i];
    if (ONLY_REF.test(item)) {
      changed = true;
      continue;
    }
    if (REF_LABEL.test(item)) {
      item = item.replace(REF_LABEL, '');
      changed = true;
    }
    if (kept.length) kept.push(parts[i - 1]);
    kept.push(item);
  }
  if (!changed) return content;
  return kept.length ? kept.join('') : null;
}

/** A text of the contract without internal references ('' when it was nothing but one). */
export function publicText(text: string): string {
  let out = text;
  for (const [pattern, replacement] of PROSE) out = out.replace(pattern, replacement);
  out = out.replace(/ ?\(([^()]*)\)/g, (whole: string, content: string) => {
    const rest = stripItems(content);
    if (rest === content) return whole;
    return rest === null ? '' : `${whole.startsWith(' ') ? ' ' : ''}(${rest})`;
  });
  if (LEADING_LABEL.test(out)) out = out.replace(LEADING_LABEL, '').replace(/^\w/, (c) => c.toUpperCase());
  out = out.replace(CITING_SENTENCE, '');
  if (ONLY_REF.test(out.trim())) return '';
  return out;
}

const TEXT_KEYS = new Set(['description', 'summary', 'title', 'x-description-zh', 'x-summary-zh']);
/** Data, not prose: example values and the values a schema allows are published as they are. */
const DATA_KEYS = new Set(['examples', 'example', 'default', 'enum', 'const', 'required']);

/** The contract as it is published: every description, summary and title through publicText(). */
export function publicContract<T>(doc: T): T {
  const walk = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(walk);
    if (!node || typeof node !== 'object') return node;
    const out: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(node)) {
      if (DATA_KEYS.has(key)) out[key] = value;
      else if (TEXT_KEYS.has(key) && typeof value === 'string') {
        const text = publicText(value);
        if (text.trim()) out[key] = text;
      } else out[key] = walk(value);
    }
    return out;
  };
  return walk(doc) as T;
}
