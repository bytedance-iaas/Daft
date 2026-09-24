// Screen 2 of the new-task form is generated from each module's param_schema (C1, D38): a new
// module with parameters needs no front-end change. Supported JSON Schema shapes: oneOf/anyOf of
// {const, title} (choices), enum, boolean, integer / number (with bounds), string, and a file
// (`format: upload` with x-upload-kind / x-accept / x-max-mb, registry 1.5: the value is the
// handle `upload:<id>` that POST /uploads returns). Parameters that stand in for one another
// share an `x-choice-group` (registry 1.10): the form shows them as one field.
import { zh } from '../locales/zh';

export type ParamKind = 'choice' | 'boolean' | 'integer' | 'number' | 'string' | 'upload';

export const UPLOAD_PREFIX = 'upload:';

export interface ParamOption {
  value: string | number | boolean;
  label: string;
}

/** Parameters of one choice group (registry 1.10): `required` asks for one of them. */
export interface ChoiceGroup {
  id: string;
  title: string;
  required: boolean;
}

export interface ParamField {
  key: string;
  title: string;
  description?: string;
  required: boolean;
  kind: ParamKind;
  options?: ParamOption[];
  default?: unknown;
  min?: number;
  max?: number;
  exclusiveMin?: boolean;
  maxLength?: number;
  pattern?: string;
  uploadKind?: string;
  accept?: string[];
  maxMb?: number;
  choiceGroup?: ChoiceGroup;
}

type Schema = Record<string, unknown>;

function asObject(x: unknown): Schema | null {
  return x && typeof x === 'object' && !Array.isArray(x) ? (x as Schema) : null;
}

function optionsOf(prop: Schema): ParamOption[] | undefined {
  const list = (prop.oneOf ?? prop.anyOf) as unknown;
  if (Array.isArray(list) && list.length && list.every((o) => asObject(o) && 'const' in (o as Schema))) {
    return list.map((o) => {
      const s = o as Schema;
      return { value: s.const as string | number | boolean, label: typeof s.title === 'string' ? s.title : String(s.const) };
    });
  }
  if (Array.isArray(prop.enum)) {
    return (prop.enum as unknown[]).filter((v) => v !== null).map((v) => ({ value: v as string | number | boolean, label: String(v) }));
  }
  return undefined;
}

function groupOf(prop: Schema): ChoiceGroup | undefined {
  const g = asObject(prop['x-choice-group']);
  if (!g || typeof g.id !== 'string') return undefined;
  return { id: g.id, title: typeof g.title === 'string' ? g.title : g.id, required: g.required === true };
}

function kindOf(prop: Schema, options: ParamOption[] | undefined): ParamKind {
  if (prop.format === 'upload') return 'upload';
  if (options) return 'choice';
  const t = Array.isArray(prop.type) ? (prop.type as string[]).find((x) => x !== 'null') : prop.type;
  if (t === 'boolean') return 'boolean';
  if (t === 'integer') return 'integer';
  if (t === 'number') return 'number';
  return 'string';
}

/** The form fields for one module's param_schema, in schema order. */
export function paramFields(schema: unknown): ParamField[] {
  const s = asObject(schema);
  const props = asObject(s?.properties);
  if (!props) return [];
  const required = new Set(Array.isArray(s?.required) ? (s!.required as string[]) : []);
  return Object.entries(props).map(([key, raw]) => {
    const prop = asObject(raw) ?? {};
    const options = optionsOf(prop);
    const kind = kindOf(prop, options);
    const exclusive = typeof prop.exclusiveMinimum === 'number';
    return {
      key,
      title: typeof prop.title === 'string' ? prop.title : key,
      description: typeof prop.description === 'string' ? prop.description : undefined,
      required: required.has(key),
      kind,
      options,
      default: prop.default,
      min: typeof prop.minimum === 'number' ? prop.minimum : exclusive ? (prop.exclusiveMinimum as number) : undefined,
      max: typeof prop.maximum === 'number' ? prop.maximum : undefined,
      exclusiveMin: exclusive || undefined,
      maxLength: typeof prop.maxLength === 'number' ? prop.maxLength : undefined,
      pattern: typeof prop.pattern === 'string' ? prop.pattern : undefined,
      choiceGroup: groupOf(prop),
      ...(kind === 'upload'
        ? {
            uploadKind: typeof prop['x-upload-kind'] === 'string' ? (prop['x-upload-kind'] as string) : undefined,
            accept: Array.isArray(prop['x-accept']) ? (prop['x-accept'] as string[]) : undefined,
            maxMb: typeof prop['x-max-mb'] === 'number' ? (prop['x-max-mb'] as number) : undefined,
          }
        : {}),
    };
  });
}

/** One entry per field, or one per choice group (its fields together, at the first one's place). */
export type FieldOrGroup = { field: ParamField } | { group: ChoiceGroup; fields: ParamField[] };

export function groupFields(fields: readonly ParamField[]): FieldOrGroup[] {
  const out: FieldOrGroup[] = [];
  const groups = new Map<string, { group: ChoiceGroup; fields: ParamField[] }>();
  for (const f of fields) {
    if (!f.choiceGroup) {
      out.push({ field: f });
      continue;
    }
    const known = groups.get(f.choiceGroup.id);
    if (known) {
      known.fields.push(f);
      continue;
    }
    const entry = { group: f.choiceGroup, fields: [f] };
    groups.set(f.choiceGroup.id, entry);
    out.push(entry);
  }
  return out;
}

export function hasParams(schema: unknown): boolean {
  return paramFields(schema).length > 0;
}

/** Default values of a module's parameters (fields without a default are left out). */
export function defaultParams(schema: unknown): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of paramFields(schema)) if (f.default !== undefined) out[f.key] = f.default;
  return out;
}

/** Validates one value; returns the Chinese problem or null. */
export function validateParam(f: ParamField, value: unknown): string | null {
  const empty = value === undefined || value === null || value === '';
  if (empty) return f.required ? zh.errors.paramRequired(f.title) : null;
  if (f.kind === 'upload') return typeof value === 'string' && value.startsWith(UPLOAD_PREFIX) ? null : zh.errors.paramUpload(f.title);
  if (f.kind === 'choice' && f.options && !f.options.some((o) => o.value === value)) return zh.errors.paramChoice(f.title);
  if (f.kind === 'integer' && !Number.isInteger(value)) return zh.errors.paramInteger(f.title);
  if ((f.kind === 'integer' || f.kind === 'number') && typeof value === 'number') {
    if (f.min !== undefined && (f.exclusiveMin ? value <= f.min : value < f.min)) return zh.errors.paramMin(f.title, f.min, Boolean(f.exclusiveMin));
    if (f.max !== undefined && value > f.max) return zh.errors.paramMax(f.title, f.max);
  }
  if (f.kind === 'string' && typeof value === 'string') {
    if (f.maxLength !== undefined && value.length > f.maxLength) return zh.errors.maxLength(f.title, f.maxLength);
    if (f.pattern && !new RegExp(f.pattern).test(value)) return zh.errors.paramPattern(f.title);
  }
  return null;
}

/** Only the values that differ from the defaults are sent (the server fills in the rest). */
export function changedParams(schema: unknown, values: Record<string, unknown> | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (!values) return out;
  for (const f of paramFields(schema)) {
    const v = values[f.key];
    if (v === undefined || v === null || v === '') continue;
    if (v !== f.default) out[f.key] = v;
  }
  return out;
}
