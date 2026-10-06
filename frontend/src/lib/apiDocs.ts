// The API reference page (api-docs.html; doc 07 §2, frontend/README.md): Scalar over the C4
// contract that the build puts next to it as openapi.json. The Daemon serves the page as a plain
// file, without the prefix injection index.html gets, so the prefix is read off the page's address.
import type { createApiReference } from '@scalar/api-reference';
import { normalizeBase } from '../base';
import { apiDocsText } from '../locales/apiDocs';

type ScalarConfiguration = Exclude<Parameters<typeof createApiReference>[1], readonly unknown[]>;
type PageLocation = { origin: string; pathname: string };

/** The page comes in both languages; the API's own texts are English in both. */
export type Lang = 'zh' | 'en';
export const LANGS: readonly Lang[] = ['zh', 'en'];

interface Server {
  url: string;
  description?: string;
  variables?: unknown;
}

/** The parts of an OpenAPI document that name servers. */
interface OpenApiDocument {
  servers?: Server[];
  paths?: Record<string, Record<string, unknown> & { servers?: Server[] }>;
}

/** The mount prefix of a page served at `{base}/<file>`: '/curation/api-docs.html' -> '/curation'. */
export function pageBase(pathname: string): string {
  return normalizeBase(pathname.replace(/\/[^/]*$/, ''));
}

/** A server of the contract (`{base}/api/v1`, `{base}`, `/`) as an absolute URL on this site. */
export function serverUrl(url: string, loc: PageLocation): string {
  if (/^[a-z][a-z0-9+.-]*:/i.test(url)) return url;
  const path = url.replace(/\{base\}/g, pageBase(loc.pathname)).replace(/\/+$/, '');
  return `${loc.origin}${path}`;
}

/**
 * Point every server of the document at this site: the contract's `base` variable (empty by
 * default) becomes the page's prefix, at the top (`{base}/api/v1`) and on the paths that override
 * it (SSE `{base}/events/...`, the probes at `/` and `{base}`). Absolute URLs also give the code
 * samples a full address. Changes `doc` in place and returns it.
 */
export function localizeServers<T extends OpenApiDocument>(doc: T, loc: PageLocation): T {
  const localize = (servers: Server[] | undefined) => servers?.map(({ url, description }) => ({ url: serverUrl(url, loc), description }));
  if (doc.servers) {
    doc.servers = doc.servers.map(({ url, description }) => ({
      url: serverUrl(url, loc),
      description: description ?? apiDocsText.en.server,
      'x-description-zh': description ?? apiDocsText.zh.server,
    }));
  }
  for (const item of Object.values(doc.paths ?? {})) {
    if (item.servers) item.servers = localize(item.servers);
    for (const op of Object.values(item)) {
      if (op && typeof op === 'object' && 'servers' in op) {
        (op as { servers?: Server[] }).servers = localize((op as { servers?: Server[] }).servers);
      }
    }
  }
  return doc;
}

const WRITES = ['post', 'put', 'patch', 'delete'];

/**
 * Writes without a body still need `Content-Type: application/json` (doc 03 §1). The contract
 * says so in prose only, so Scalar's code samples would leave the header out and a copied curl
 * would get 400. On the page (not in openapi.json) such operations get the header as a required
 * parameter. Changes `doc` in place and returns it.
 */
export function requireJsonOnBodylessWrites<T extends OpenApiDocument>(doc: T): T {
  for (const item of Object.values(doc.paths ?? {})) {
    for (const method of WRITES) {
      const op = item[method] as { requestBody?: unknown; parameters?: unknown[] } | undefined;
      if (!op || op.requestBody) continue;
      op.parameters = [
        ...(op.parameters ?? []),
        {
          name: 'Content-Type',
          in: 'header',
          required: true,
          description: apiDocsText.en.contentType,
          'x-description-zh': apiDocsText.zh.contentType,
          schema: { type: 'string', enum: ['application/json'] },
          example: 'application/json',
        },
      ];
    }
  }
  return doc;
}

/** What Scalar sends for basicAuth when nobody typed credentials (literally, not base64). */
const PLACEHOLDER_AUTH = 'Basic username:password';

/**
 * Make a try-it request one the Daemon takes (doc 03 §1, backend/daemon/routes/common.py):
 * - an empty basicAuth form must not override the login the browser already holds for this
 *   origin, so Scalar's placeholder header goes and the browser adds the real one;
 * - every write says `Content-Type: application/json`, with a body or not (Scalar sets it only
 *   when the operation declares a JSON body, and several writes have none).
 */
export function prepareRequest(method: string, headers: Headers): void {
  if (headers.get('authorization') === PLACEHOLDER_AUTH) headers.delete('authorization');
  if (WRITES.includes(method.toLowerCase()) && !headers.has('content-type')) headers.set('content-type', 'application/json');
}

/**
 * The page's language: the one the address names (`?api=en`, or a link into `#zh/...`), else
 * Chinese for a browser that prefers it, else English.
 */
export function pageLanguage(languages: readonly string[], loc: { search: string; hash: string } = { search: '', hash: '' }): Lang {
  const named = new URLSearchParams(loc.search).get('api') ?? /^#(zh|en)\b/.exec(loc.hash)?.[1];
  if (named === 'zh' || named === 'en') return named;
  return /^zh\b/i.test(languages[0] ?? '') ? 'zh' : 'en';
}

const COPY = { description: 'x-description-zh', summary: 'x-summary-zh' } as const;

/**
 * The document in one language. The contract keeps its copy - the home page, the tags, the example
 * titles - in English under description / summary and in Chinese under x-description-zh /
 * x-summary-zh; the Chinese version takes the latter where there is one. Either way the x- fields
 * go, and the API's own texts stay English. Returns a new document.
 */
export function inLanguage<T>(doc: T, lang: Lang): T {
  const walk = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(walk);
    if (!node || typeof node !== 'object') return node;
    const out: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(node)) {
      if (key !== COPY.description && key !== COPY.summary) out[key] = walk(value);
    }
    if (lang === 'zh') {
      for (const [field, zh] of Object.entries(COPY)) {
        const text = (node as Record<string, unknown>)[zh];
        if (typeof text === 'string') out[field] = text;
      }
    }
    return out;
  };
  return walk(doc) as T;
}

/**
 * Scalar's configuration: the contract in Chinese and in English as two documents, switched in the
 * sidebar (or with `?api=en`), `lang` first. Everything that would leave the site is off: the site
 * may have no way out to the internet, and the page must not need one.
 */
export function apiDocsConfiguration(doc: object, lang: Lang): ScalarConfiguration {
  return {
    sources: LANGS.map((l) => ({ title: apiDocsText[l].language, slug: l, content: inLanguage(doc, l), default: l === lang })),
    defaultHttpClient: { targetKey: 'shell', clientKey: 'curl' },
    documentDownloadType: 'json',
    onBeforeRequest: ({ request }) => prepareRequest(request.method, request.headers),
    // no fonts.scalar.com, no analytics, no AI agent (it switches itself on at localhost), no
    // developer toolbar; proxyUrl stays unset, so try-it requests go straight to this origin
    withDefaultFonts: false,
    telemetry: false,
    agent: { disabled: true },
    showDeveloperTools: 'never',
  };
}
