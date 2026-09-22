// v1 deep links on /tasks/new (doc 07 §2.1). A line-by-line port of the parsing rules in
// backend/curation/ui/runner.py (deeplink_values / deeplink_region / deeplink_endpoint /
// sanitize_endpoint / endpoint_region / parse_dataset_ref / split_dataset_url /
// borrowed_output_url) and of the deep-link branch of v1's app._prefill_from_query and
// runner.prefill_plan. The rerun viewer's 「质检」 button depends on this contract.
//
// What changed for v2 (and only this): v1 matched links against mounted buckets from the site
// config; v2 has no mounts, so a full tos:// address is used as given (access is decided by the
// access key, and the automatic preflight says whether it is readable), and a bare name is
// looked up among the registered datasets (D36) instead of «the default bucket». The iron rules
// stay: an address is never rewritten into another bucket or prefix, an ambiguous name is never
// guessed, and a key that is present always gets an answer.

import { zh } from '../locales/zh';

// ---------------------------------------------------------------- query access

/** Query parameters as URLSearchParams, or a plain object (tests), like v1's QueryParams/dict. */
export type QueryInput = URLSearchParams | Record<string, string | string[] | undefined>;

function getAll(qp: QueryInput, key: string): string[] {
  if (qp instanceof URLSearchParams) return qp.getAll(key);
  const v = qp[key];
  if (v === undefined) return [];
  return Array.isArray(v) ? v : [v];
}

// ---------------------------------------------------------------- dataset keys

/** The three keys, all on the same parsing path (bare names and tos:// URLs alike). */
export const DEEPLINK_KEYS = ['dataset', 'dataset_url', 'url'] as const;

/**
 * query → (values to pre-select, whether a dataset key was present).
 * Several keys are merged in DEEPLINK_KEYS order and de-duplicated; commas split again.
 * `present` is true even when the value is empty: the page must then say something.
 */
export function deeplinkValues(qp: QueryInput): { values: string[]; present: boolean } {
  const out: string[] = [];
  let present = false;
  for (const key of DEEPLINK_KEYS) {
    const vals = getAll(qp, key);
    if (vals.length) present = true;
    for (const raw of vals) {
      for (const part of String(raw ?? '').split(',')) {
        const s = part.trim();
        if (s && !out.includes(s)) out.push(s);
      }
    }
  }
  return { values: out, present };
}

// ---------------------------------------------------------------- region

export const REGION_RE = /^[a-z0-9][a-z0-9-]{1,31}$/;
export const REGION_KEY = 'region';

/**
 * query → (a valid region or null, whether the key was present). A scalar: the first value that
 * passes the character set wins. It only pre-selects the dropdown and never enters any request
 * path; a bad value is never echoed back.
 */
export function deeplinkRegion(qp: QueryInput): { region: string | null; present: boolean } {
  const vals = getAll(qp, REGION_KEY);
  for (const raw of vals) {
    const s = String(raw ?? '').trim().toLowerCase();
    if (s && REGION_RE.test(s)) return { region: s, present: true };
  }
  return { region: null, present: vals.length > 0 };
}

// ---------------------------------------------------------------- endpoint

export const ENDPOINT_KEYS = ['endpoint', 'tos_endpoint'] as const;

const ENDPOINT_HOST_RE = /^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$/;
const REGION_IN_HOST_RE = /(?<![a-z0-9])((?:cn|ap|us|eu)-[a-z]+(?:-\d+)?)(?![a-z0-9])/;
const SCHEME_RE = /^[A-Za-z][A-Za-z0-9+.-]*$/;

/** The netloc of a URL the way Python's urlsplit finds it ('' when there is none). */
function splitNetloc(url: string): string {
  let rest = url.replace(/[\t\r\n]/g, '');
  const i = rest.indexOf(':');
  if (i > 0 && SCHEME_RE.test(rest.slice(0, i))) rest = rest.slice(i + 1);
  if (!rest.startsWith('//')) return '';
  rest = rest.slice(2);
  const end = rest.search(/[/?#]/);
  return end === -1 ? rest : rest.slice(0, end);
}

/**
 * An untrusted endpoint string → a clean lowercase host name, or null (treat as absent).
 * Scheme, credentials, port, path and query are dropped; only host name characters survive.
 * The host is for hints only and is never used to read anything (SSRF surface).
 */
export function sanitizeEndpoint(raw: unknown): string | null {
  const s = String(raw ?? '').trim();
  if (!s || s.length > 1024) return null;
  const netloc = splitNetloc(s.includes('://') ? s : `//${s}`);
  // Brackets mean an IPv6 literal; anything bracketed is rejected outright.
  if (netloc.includes('[') || netloc.includes(']')) return null;
  const hostinfo = netloc.slice(netloc.lastIndexOf('@') + 1);
  const host = hostinfo.split(':')[0].trim().toLowerCase();
  if (!host || host.length > 253 || !ENDPOINT_HOST_RE.test(host)) return null;
  return host;
}

/** Host → region segment (tos-cn-beijing.ivolces.com → cn-beijing); null when not recognisable. */
export function endpointRegion(host: unknown): string | null {
  const m = REGION_IN_HOST_RE.exec(String(host ?? '').toLowerCase());
  return m ? m[1] : null;
}

/** query → (sanitized host or null, whether an endpoint key was present); first clean value wins. */
export function deeplinkEndpoint(qp: QueryInput): { host: string | null; present: boolean } {
  let present = false;
  for (const key of ENDPOINT_KEYS) {
    const vals = getAll(qp, key);
    if (vals.length) present = true;
    for (const raw of vals) {
      const host = sanitizeEndpoint(raw);
      if (host) return { host, present: true };
    }
  }
  return { host: null, present };
}

// ---------------------------------------------------------------- names and tos:// URLs

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;

/** v1 safe_name: returns the problem in Chinese, or null when the name is fine. */
export function safeNameError(name: string, what: string = zh.deeplink.nameWhat): string | null {
  const s = String(name ?? '').trim();
  if (!s) return zh.deeplink.nameEmpty(what);
  if (s.includes('/') || s.includes('\\')) return zh.deeplink.nameSeparator(what, JSON.stringify(s));
  if (s === '.' || s === '..' || s.startsWith('.')) return zh.deeplink.nameDot(what, JSON.stringify(s));
  if (!NAME_RE.test(s)) return zh.deeplink.nameChars(what, JSON.stringify(s));
  return null;
}

/** Percent-decoding like Python's unquote: malformed sequences stay, bad UTF-8 becomes U+FFFD. */
export function unquote(s: string): string {
  return s.replace(/(?:%[0-9A-Fa-f]{2})+/g, (run) => {
    const bytes = new Uint8Array(run.length / 3);
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = parseInt(run.slice(i * 3 + 1, i * 3 + 3), 16);
    return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
  });
}

export type DatasetRef =
  | { bucket: string | null; prefix: string | null; dataset: string; error?: undefined }
  | { error: string; bucket?: undefined; prefix?: undefined; dataset?: undefined };

/**
 * One reference from a deep link → {bucket, prefix, dataset} or {error}. Bare names keep
 * bucket/prefix null. Bucket names compare case-insensitively (lowercased); prefix and name are
 * case-sensitive. Trailing slashes are tolerated. %-escapes are decoded first and a decoded name
 * that fails safe_name is refused, never «fixed».
 */
export function parseDatasetRef(raw: string): DatasetRef {
  const s = String(raw ?? '').trim();
  if (!s.includes('://')) return { bucket: null, prefix: null, dataset: s };
  if (!s.toLowerCase().startsWith('tos://')) return { error: zh.deeplink.onlyTos(s) };
  const clean = s.replace(/[\t\r\n]/g, '');
  const netloc = splitNetloc(clean);
  if ((netloc.includes('[') && !netloc.includes(']')) || (netloc.includes(']') && !netloc.includes('['))) {
    return { error: zh.deeplink.unparsable(s) };
  }
  const bucket = unquote(netloc).trim().toLowerCase();
  if (!bucket) return { error: zh.deeplink.noBucket(s) };
  const afterNetloc = clean.slice(clean.indexOf('://') + 3 + netloc.length);
  const path = afterNetloc.split(/[?#]/)[0];
  const segs = path
    .split('/')
    .filter((x) => x.trim())
    .map((x) => unquote(x).trim());
  if (!segs.length) return { error: zh.deeplink.bucketOnly(s) };
  const name = segs[segs.length - 1];
  const bad = safeNameError(name);
  if (bad) return { error: zh.deeplink.badName(bad, s) };
  return { bucket, prefix: segs.slice(0, -1).join('/'), dataset: name };
}

const BUCKET_RE = /^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/;

/** v1 tos_store.parse_tos_url: `tos://<bucket>/<prefix>` → [bucket, prefix]; throws on bad syntax. */
export function parseTosUrl(url: string): [string, string] {
  const s = String(url ?? '').trim();
  if (!s.startsWith('tos://')) throw new Error(zh.deeplink.notTosUrl(s));
  const rest = s.slice('tos://'.length);
  const slash = rest.indexOf('/');
  const bucket = slash === -1 ? rest : rest.slice(0, slash);
  let prefix = slash === -1 ? '' : rest.slice(slash + 1);
  if (!BUCKET_RE.test(bucket)) throw new Error(zh.deeplink.badBucket(bucket));
  prefix = prefix.replace(/^\/+|\/+$/g, '');
  if (prefix.split('/').some((seg) => seg === '..')) throw new Error(zh.deeplink.dotDot(prefix));
  if (prefix.includes('\\')) throw new Error(zh.deeplink.backslash(prefix));
  return [bucket, prefix];
}

/**
 * `tos://bucket/prefix…/name` → [root prefix URL, dataset name] by the last-segment rule.
 * A bucket without a dataset segment gives name '' (fill the root, pre-select nothing).
 */
export function splitDatasetUrl(url: string): [string, string] {
  const [bucket, prefix] = parseTosUrl(url);
  const parts = prefix.split('/').filter(Boolean);
  if (!parts.length) return [`tos://${bucket}`, ''];
  const name = parts[parts.length - 1];
  const root = parts.slice(0, -1).join('/');
  return [root ? `tos://${bucket}/${root}` : `tos://${bucket}`, name];
}

/** Canonical form for comparing dataset addresses: bucket lowercased, no trailing slash. */
export function canonicalTosUri(uri: string): string | null {
  try {
    const s = String(uri ?? '').trim();
    const lower = s.toLowerCase();
    if (!lower.startsWith('tos://')) return null;
    const rest = s.slice(6);
    const slash = rest.indexOf('/');
    const bucket = (slash === -1 ? rest : rest.slice(0, slash)).toLowerCase();
    const prefix = (slash === -1 ? '' : rest.slice(slash + 1)).replace(/^\/+|\/+$/g, '');
    return prefix ? `tos://${bucket}/${prefix}` : `tos://${bucket}`;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- delivery directory default

/**
 * Where the delivery directory defaults to (doc 07 §2.1 rule 3, v1 borrowed_output_url):
 * the last delivery root used → otherwise borrow the dataset's bucket as tos://<bucket>/deliveries.
 * Never the read-only public cache bucket; '' when nothing sensible can be offered.
 * The caller still has to pass a write probe before filling a borrowed bucket in.
 */
export function borrowedOutputUrl(datasetUrl: string, lastRoot: string | null | undefined, publicBuckets: readonly string[] = []): string {
  const home = String(lastRoot ?? '').trim().replace(/\/+$/, '');
  if (home.startsWith('tos://')) return home;
  const s = String(datasetUrl ?? '').trim();
  if (!s.startsWith('tos://')) return '';
  let bucket: string;
  try {
    [bucket] = parseTosUrl(s);
  } catch {
    return '';
  }
  if (publicBuckets.includes(bucket)) return '';
  return `tos://${bucket}/deliveries`;
}

/** Delivery directory for the public cache source: [value, note]; never the cache bucket itself. */
export function publicOutputDefault(lastRoot: string | null | undefined): [string, string] {
  const home = String(lastRoot ?? '').trim().replace(/\/+$/, '');
  if (home.startsWith('tos://')) return [home, ''];
  return ['', zh.deeplink.publicReadonly];
}

/** v1 suggest_delivery_name: `<dataset>-<MMDD>`, cleaned to a safe directory name. */
export function suggestDeliveryName(dataset: string, mmdd: string): string {
  const base = String(dataset || 'dataset')
    .replace(/[^A-Za-z0-9._-]/g, '-')
    .replace(/^[-.]+|[-.]+$/g, '') || 'dataset';
  return `${base.slice(0, 60)}-${mmdd}`;
}

// ---------------------------------------------------------------- the whole plan

/** A registered dataset as far as deep-link matching is concerned (a DatasetItem subset). */
export interface RegisteredDataset {
  id: string;
  name: string;
  source: 'tos' | 'public' | 'local';
  uri: string;
  region: string | null;
}

/** A public cache-bucket dataset (from GET /datasets/browse?source=public). */
export interface PublicDataset {
  name: string;
  uri: string;
}

export interface PlanContext {
  registered: readonly RegisteredDataset[];
  /** null when the site has no HuggingFace cache bucket configured. */
  publicDatasets: readonly PublicDataset[] | null;
  /** The region this instance reads from when nothing else is known (for the endpoint hint). */
  homeRegion?: string | null;
}

export interface PlannedDataset {
  uri: string;
  name: string;
  /** Set when the address is a registered dataset: its id, region and access key come along. */
  datasetId?: string;
  region?: string | null;
}

export interface DeepLinkPlan {
  /** Any deep-link key at all (dataset keys, region, endpoint, source). */
  present: boolean;
  source: 'tos' | 'public' | null;
  /** Addresses to pre-fill; more than one means batch mode (P3). */
  datasets: PlannedDataset[];
  /** Only the root was given (bucket or prefix without a dataset): fill it, select nothing. */
  rootOnly: string | null;
  region: string | null;
  endpointHost: string | null;
  /** Persistent red notes under the field they concern (rule 1: never silent). */
  notes: { dataset: string[]; region: string[] };
}

function lastSegment(uri: string): string {
  const parts = uri.replace(/\/+$/, '').split('/');
  return parts[parts.length - 1] ?? '';
}

function rootOf(uri: string): string {
  const c = canonicalTosUri(uri) ?? uri;
  return c.slice(0, c.lastIndexOf('/'));
}

/** Resolves the query of /tasks/new into what to pre-fill (pure; the page only renders it). */
export function planDeepLink(qp: QueryInput, ctx: PlanContext): DeepLinkPlan {
  const plan: DeepLinkPlan = {
    present: false,
    source: null,
    datasets: [],
    rootOnly: null,
    region: null,
    endpointHost: null,
    notes: { dataset: [], region: [] },
  };
  const linkValues = deeplinkValues(qp);
  const wanted = linkValues.values;
  let present = linkValues.present;

  // ?source=public[&dataset=name]: the HuggingFace cache bucket.
  const srcVals = getAll(qp, 'source');
  const src = String(srcVals[0] ?? '').trim().toLowerCase();
  let isPublic = false;
  if (srcVals.length) {
    plan.present = true;
    if (src === 'public') {
      if (ctx.publicDatasets) {
        isPublic = true;
        present = true;
        plan.source = 'public';
      } else {
        plan.notes.dataset.push(zh.deeplink.publicMissing);
      }
    } else if (src === 'tos') {
      plan.source = 'tos';
    } else {
      plan.notes.dataset.push(zh.deeplink.sourceUnknown);
    }
  }

  // Region: pre-select only.
  const rg = deeplinkRegion(qp);
  if (rg.present) plan.present = true;
  if (rg.region) plan.region = rg.region;
  else if (rg.present) plan.notes.region.push(zh.deeplink.regionBad);

  // Endpoint: hint only, never read from.
  const ep = deeplinkEndpoint(qp);
  if (ep.present) plan.present = true;
  plan.endpointHost = ep.host;
  if (ep.present && !ep.host) plan.notes.region.push(zh.deeplink.endpointBad);

  if (present) plan.present = true;
  if (!present) return withEndpointHint(plan, ctx);

  if (isPublic) return withEndpointHint(planPublic(plan, wanted, ctx.publicDatasets ?? []), ctx);

  // Bucket names compare case-insensitively (URL host rules, as parse_dataset_ref documents);
  // TOS bucket names are lowercase, so the link's bucket is lowercased before anything else.
  const urls: string[] = [];
  const bare: string[] = [];
  for (const w of wanted) {
    if (w.startsWith('tos://')) {
      const rest = w.slice(6);
      const slash = rest.indexOf('/');
      urls.push(slash === -1 ? `tos://${rest.toLowerCase()}` : `tos://${rest.slice(0, slash).toLowerCase()}${rest.slice(slash)}`);
    } else {
      bare.push(w);
    }
  }

  // A bare value written as TOS://Bucket/... (any case) is a URL after all.
  const bareNames: string[] = [];
  for (const b of bare) {
    const ref = parseDatasetRef(b);
    if (ref.error !== undefined) {
      plan.notes.dataset.push(ref.error);
      continue;
    }
    if (ref.bucket === null) bareNames.push(ref.dataset);
    else urls.push(`tos://${ref.bucket}/${ref.prefix ? `${ref.prefix}/` : ''}${ref.dataset}`);
  }

  if (urls.length) {
    planUrls(plan, urls, ctx);
    if (bareNames.length) plan.notes.dataset.push(zh.deeplink.mixedIgnored(bareNames));
  } else if (bareNames.length) {
    planBareNames(plan, bareNames, ctx);
  }
  if (!plan.datasets.length && !plan.rootOnly && !plan.notes.dataset.length) {
    plan.notes.dataset.push(zh.deeplink.emptyValue);
  }
  plan.source = plan.source ?? (plan.datasets.length || plan.rootOnly ? 'tos' : null);
  return withEndpointHint(plan, ctx);
}

function planPublic(plan: DeepLinkPlan, wanted: string[], catalog: readonly PublicDataset[]): DeepLinkPlan {
  for (const w of wanted) {
    const hit = catalog.find((d) => d.name === w || canonicalTosUri(d.uri) === canonicalTosUri(w) || d.uri === w);
    if (hit) {
      if (!plan.datasets.some((d) => d.uri === hit.uri)) plan.datasets.push({ uri: hit.uri, name: hit.name });
    } else {
      plan.notes.dataset.push(zh.deeplink.publicNotFound(w));
    }
  }
  return plan;
}

function matchRegistered(uri: string, region: string | null, ctx: PlanContext): RegisteredDataset[] {
  const c = canonicalTosUri(uri);
  return ctx.registered.filter(
    (d) => d.source === 'tos' && canonicalTosUri(d.uri) === c && (region === null || d.region === region),
  );
}

function planUrls(plan: DeepLinkPlan, urls: string[], ctx: PlanContext): void {
  let root: string;
  let first: string;
  try {
    [root, first] = splitDatasetUrl(urls[0]);
  } catch (e) {
    plan.notes.dataset.push(zh.deeplink.addressUnparsable((e as Error).message));
    return;
  }
  // A link into the public cache bucket switches the source, as v1 did (is_public_root).
  const publicRoots = new Set((ctx.publicDatasets ?? []).map((d) => rootOf(d.uri)));
  if (publicRoots.has(canonicalTosUri(root) ?? root)) {
    plan.source = 'public';
    planPublic(plan, urls, ctx.publicDatasets ?? []);
    return;
  }
  const names: string[] = [];
  for (const u of urls) {
    let r2: string;
    let n2: string;
    try {
      [r2, n2] = splitDatasetUrl(u);
    } catch {
      plan.notes.dataset.push(zh.deeplink.addressIgnored(u));
      continue;
    }
    if (r2 !== root) {
      plan.notes.dataset.push(zh.deeplink.otherPrefixIgnored(root, u));
      continue;
    }
    if (!n2) continue;
    const bad = safeNameError(unquote(n2));
    if (bad) {
      plan.notes.dataset.push(zh.deeplink.badName(bad, u));
      continue;
    }
    if (!names.includes(n2)) names.push(n2);
  }
  if (!names.length) {
    if (!first) {
      plan.rootOnly = root;
      plan.notes.dataset.push(zh.deeplink.rootOnly(root));
    }
    return;
  }
  for (const n of names) {
    const uri = `${root}/${n}`;
    const hits = matchRegistered(uri, plan.region, ctx);
    if (hits.length === 1) {
      plan.datasets.push({ uri: hits[0].uri, name: n, datasetId: hits[0].id, region: hits[0].region });
    } else {
      if (hits.length > 1) plan.notes.dataset.push(zh.deeplink.registeredTwice(n));
      plan.datasets.push({ uri, name: n });
    }
  }
}

function planBareNames(plan: DeepLinkPlan, bareNames: string[], ctx: PlanContext): void {
  const picked: RegisteredDataset[] = [];
  const missing: string[] = [];
  for (const name of bareNames) {
    const hits = ctx.registered.filter(
      (d) => d.source === 'tos' && (lastSegment(d.uri) === name || d.name === name) && (plan.region === null || d.region === plan.region),
    );
    const distinct = hits.filter((d, i) => hits.findIndex((x) => canonicalTosUri(x.uri) === canonicalTosUri(d.uri)) === i);
    if (distinct.length === 1) {
      if (!picked.some((p) => p.id === distinct[0].id)) picked.push(distinct[0]);
    } else if (distinct.length > 1) {
      plan.notes.dataset.push(zh.deeplink.ambiguousName(name, distinct.length));
    } else {
      missing.push(name);
    }
  }
  if (missing.length) {
    // As in v1, an unmatched reference names the link's endpoint so people know where to ask.
    const suffix = plan.endpointHost ? zh.deeplink.endpointSuffix(plan.endpointHost) : '';
    plan.notes.dataset.push(zh.deeplink.notFound(missing) + suffix);
  }
  const roots = [...new Set(picked.map((p) => rootOf(p.uri)))];
  if (roots.length > 1) {
    plan.notes.dataset.push(zh.deeplink.spansRoots(roots));
    return;
  }
  for (const p of picked) plan.datasets.push({ uri: p.uri, name: lastSegment(p.uri), datasetId: p.id, region: p.region });
}

/** Adds the endpoint/region contradiction hint (once): hints only, the pre-selection stays. */
function withEndpointHint(plan: DeepLinkPlan, ctx: PlanContext): DeepLinkPlan {
  if (!plan.endpointHost) return plan;
  const linkRegion = endpointRegion(plan.endpointHost);
  const ours = plan.region ?? plan.datasets.find((d) => d.region)?.region ?? ctx.homeRegion ?? null;
  if (linkRegion && ours && linkRegion !== ours) {
    plan.notes.region.push(zh.deeplink.endpointConflict(plan.endpointHost, linkRegion, ours));
  }
  return plan;
}
