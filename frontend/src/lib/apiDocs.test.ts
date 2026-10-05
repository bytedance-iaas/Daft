import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { parse } from 'yaml';
import { describe, expect, it } from 'vitest';
import { apiDocsConfiguration, localizeServers, pageBase, prepareRequest, requireJsonOnBodylessWrites, serverUrl } from './apiDocs';

const at = (pathname: string) => ({ origin: 'https://host.example', pathname });
const contract = () => parse(readFileSync(resolve(process.cwd(), '..', 'docs', 'contracts', 'openapi.yaml'), 'utf8'));

describe('API reference page', () => {
  it('reads the mount prefix off its own address', () => {
    expect(pageBase('/curation/api-docs.html')).toBe('/curation');
    expect(pageBase('/api-docs.html')).toBe('');
    expect(pageBase('/a/b/api-docs.html')).toBe('/a/b');
  });

  it('turns the servers of the contract into absolute URLs under the prefix', () => {
    expect(serverUrl('{base}/api/v1', at('/curation/api-docs.html'))).toBe('https://host.example/curation/api/v1');
    expect(serverUrl('{base}/api/v1', at('/api-docs.html'))).toBe('https://host.example/api/v1');
    expect(serverUrl('{base}', at('/curation/api-docs.html'))).toBe('https://host.example/curation');
    expect(serverUrl('{base}', at('/api-docs.html'))).toBe('https://host.example');
    expect(serverUrl('/', at('/curation/api-docs.html'))).toBe('https://host.example');
    expect(serverUrl('https://elsewhere.example/v1', at('/curation/api-docs.html'))).toBe('https://elsewhere.example/v1');
  });

  it('localizes every server of the contract: the API, the SSE stream and the probes', () => {
    const doc = localizeServers(contract(), at('/curation/api-docs.html'));
    expect(doc.servers).toEqual([{ url: 'https://host.example/curation/api/v1', description: 'this Daemon' }]);
    expect(doc.paths['/events/tasks/{id}'].servers.map((s: { url: string }) => s.url)).toEqual(['https://host.example/curation']);
    expect(doc.paths['/healthz'].servers.map((s: { url: string }) => s.url)).toEqual(['https://host.example', 'https://host.example/curation']);
    // nothing relative or templated is left
    const servers = [doc.servers, ...Object.values(doc.paths as Record<string, { servers?: unknown[] }>).map((p) => p.servers ?? [])].flat();
    for (const s of servers as { url: string; variables?: unknown }[]) {
      expect(s.url).toMatch(/^https:\/\/host\.example(\/curation)?(\/api\/v1)?$/);
      expect(s).not.toHaveProperty('variables');
    }
  });

  it('gives every write without a body a required Content-Type header, so the code samples carry it', () => {
    type Op = { requestBody?: unknown; parameters?: { name?: string; in?: string; required?: boolean }[] };
    const before = contract();
    const doc = requireJsonOnBodylessWrites(contract());
    const contentType = (op: Op) => (op.parameters ?? []).filter((p) => p.in === 'header' && p.name === 'Content-Type');
    let bodyless = 0;
    for (const [path, item] of Object.entries(doc.paths as Record<string, Record<string, Op>>)) {
      for (const [method, op] of Object.entries(item)) {
        if (!['get', 'post', 'put', 'patch', 'delete'].includes(method)) continue;
        if (['post', 'put', 'patch', 'delete'].includes(method) && !op.requestBody) {
          bodyless += 1;
          expect(contentType(op), `${method} ${path}`).toEqual([expect.objectContaining({ required: true })]);
        } else {
          expect(op).toEqual(before.paths[path][method]);
        }
      }
    }
    expect(bodyless).toBeGreaterThan(0);
  });

  it('hands Scalar the document and turns off everything that would leave the site', () => {
    const doc = { openapi: '3.1.0' };
    const config = apiDocsConfiguration(doc);
    expect(config.content).toBe(doc);
    expect(config).toMatchObject({
      withDefaultFonts: false,
      telemetry: false,
      agent: { disabled: true },
      showDeveloperTools: 'never',
    });
    expect(config).not.toHaveProperty('proxyUrl');
  });

  it('every write carries Content-Type: application/json, with a body or not', () => {
    const post = new Headers();
    prepareRequest('post', post);
    expect(post.get('content-type')).toBe('application/json');
    const del = new Headers();
    prepareRequest('DELETE', del);
    expect(del.get('content-type')).toBe('application/json');
    const upload = new Headers({ 'content-type': 'application/zip' });
    prepareRequest('POST', upload);
    expect(upload.get('content-type')).toBe('application/zip');
    const get = new Headers();
    prepareRequest('GET', get);
    expect(get.has('content-type')).toBe(false);
  });

  it("drops Scalar's placeholder basic auth so the browser's own login applies, and keeps typed credentials", () => {
    const empty = new Headers({ authorization: 'Basic username:password' });
    prepareRequest('GET', empty);
    expect(empty.has('authorization')).toBe(false);
    const typed = new Headers({ authorization: `Basic ${btoa('alice:secret')}` });
    prepareRequest('GET', typed);
    expect(typed.get('authorization')).toBe(`Basic ${btoa('alice:secret')}`);
  });

  it('works on the Request object Scalar hands to onBeforeRequest', () => {
    const request = new Request('http://localhost/api/v1/tasks/t/actions/pause', {
      method: 'POST',
      headers: { authorization: 'Basic username:password' },
    });
    apiDocsConfiguration({}).onBeforeRequest?.({ request });
    expect(request.headers.get('content-type')).toBe('application/json');
    expect(request.headers.has('authorization')).toBe(false);
  });
});
