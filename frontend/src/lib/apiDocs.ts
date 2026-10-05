// The API reference page (api-docs.html; doc 07 §2, frontend/README.md): Scalar over the C4
// contract that the build puts next to it as openapi.json. The Daemon serves the page as a plain
// file, without the prefix injection index.html gets, so the prefix is read off the page's address.
import type { createApiReference } from '@scalar/api-reference';
import { normalizeBase } from '../base';

type ScalarConfiguration = Exclude<Parameters<typeof createApiReference>[1], readonly unknown[]>;
type PageLocation = { origin: string; pathname: string };

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
  const localize = (servers: Server[] | undefined, fallback?: string) =>
    servers?.map(({ url, description }) => ({ url: serverUrl(url, loc), description: description ?? fallback }));
  if (doc.servers) doc.servers = localize(doc.servers, 'this Daemon');
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
          description: 'Every write says it, even without a body (Conventions, Writes).',
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
 * Scalar's configuration for the contract `doc` (already localized). Everything that would leave
 * the site is off: the site may have no way out to the internet, and the page must not need one.
 */
export function apiDocsConfiguration(doc: object): ScalarConfiguration {
  return {
    content: doc,
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
