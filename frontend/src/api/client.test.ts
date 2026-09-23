import { HttpResponse, http } from 'msw';
import { afterEach, describe, expect, it } from 'vitest';
import { server } from '../mocks/server';
import { apiBaseUrl, appPath, routerBasename, taskEventsUrl } from '../base';
import { api, idempotencyKey, unwrap } from './client';
import type { ApiError} from './errors';
import { errorMessage, isApiError } from './errors';

afterEach(() => {
  delete window.__CURATOR_BASE__;
});

describe('mount prefix (doc 07 §2.3)', () => {
  it('builds every URL from window.__CURATOR_BASE__', () => {
    expect(apiBaseUrl()).toBe(`${window.location.origin}/api/v1`);
    expect(routerBasename()).toBe('/');
    window.__CURATOR_BASE__ = '/curation/';
    expect(apiBaseUrl()).toBe(`${window.location.origin}/curation/api/v1`);
    expect(taskEventsUrl('task 1')).toBe('/curation/events/tasks/task%201');
    expect(routerBasename()).toBe('/curation');
    expect(appPath('tasks/1')).toBe('/curation/tasks/1');
  });

  it('sends requests under the prefix', async () => {
    window.__CURATOR_BASE__ = '/curation';
    let seen = '';
    server.use(
      http.get('*/api/v1/modules', ({ request }) => {
        seen = new URL(request.url).pathname;
        return HttpResponse.json({ registry_version: '1.1', stages: [], modules: [] });
      }),
    );
    await unwrap(api().GET('/modules'));
    expect(seen).toBe('/curation/api/v1/modules');
  });
});

describe('unwrap and the one Error body (doc 03 §1)', () => {
  it('returns data for 2xx', async () => {
    const reg = await unwrap(api().GET('/modules'));
    expect(reg.modules.length).toBe(10);
    expect(reg.modules.filter((m) => !m.affects_dataset_verdict).map((m) => m.id)).toEqual(['eef_video_consistency', 'eef_video_review']);
  });

  it('throws ApiError with the Chinese message and the stable code', async () => {
    const e = await unwrap(api().GET('/tasks/{id}', { params: { path: { id: 'task_nope' } } })).catch((x: unknown) => x);
    expect(isApiError(e, 'not_found')).toBe(true);
    expect((e as ApiError).status).toBe(404);
    expect(errorMessage(e)).toBe('任务不存在，可能已被删除');
  });

  it('keeps details (e.g. the SourceChange of source_changed)', async () => {
    server.use(
      http.post('*/api/v1/tasks/:id/actions/:action', () =>
        HttpResponse.json({ error: { code: 'source_changed', message: '数据集变了', details: { added: 3 } } }, { status: 409 }),
      ),
    );
    const e = (await unwrap(api().POST('/tasks/{id}/actions/{action}', { params: { path: { id: 't', action: 'start' } } })).catch((x: unknown) => x)) as ApiError;
    expect(e.code).toBe('source_changed');
    expect(e.details).toEqual({ added: 3 });
  });

  it('handles 204, non-JSON errors and network failures', async () => {
    server.use(
      http.delete('*/api/v1/datasets/:id', () => new HttpResponse(null, { status: 204 })),
      http.get('*/api/v1/overview', () => new HttpResponse('<html>bad gateway</html>', { status: 502, headers: { 'Content-Type': 'text/html' } })),
      http.get('*/api/v1/credentials', () => HttpResponse.error()),
    );
    await expect(unwrap(api().DELETE('/datasets/{id}', { params: { path: { id: 'x' } } }))).resolves.toBeUndefined();
    const bad = (await unwrap(api().GET('/overview')).catch((x: unknown) => x)) as ApiError;
    expect(bad.code).toBe('unexpected_response');
    expect(bad.message).toContain('502');
    const net = (await unwrap(api().GET('/credentials')).catch((x: unknown) => x)) as ApiError;
    expect(net.code).toBe('network');
    expect(net.message).toBe('连不上服务，请检查网络后重试');
  });

  it('every write carries Content-Type: application/json, even without a body (C4 1.2)', async () => {
    const seen: Record<string, string | null> = {};
    server.use(
      http.post('*/api/v1/datasets/:id/recheck', ({ request }) => {
        seen.post = request.headers.get('Content-Type');
        return HttpResponse.json({ at: 1, trigger: 'recheck', result: 'same' });
      }),
      http.delete('*/api/v1/datasets/:id', ({ request }) => {
        seen.delete = request.headers.get('Content-Type');
        return new HttpResponse(null, { status: 204 });
      }),
      http.get('*/api/v1/modules', ({ request }) => {
        seen.get = request.headers.get('Content-Type');
        return HttpResponse.json({ registry_version: '1.1', stages: [], modules: [] });
      }),
    );
    await unwrap(api().POST('/datasets/{id}/recheck', { params: { path: { id: 'x' } } }));
    await unwrap(api().DELETE('/datasets/{id}', { params: { path: { id: 'x' } } }));
    await unwrap(api().GET('/modules'));
    expect(seen).toEqual({ post: 'application/json', delete: 'application/json', get: null });
  });

  it('idempotency keys are unique per action', () => {
    const a = idempotencyKey();
    const b = idempotencyKey();
    expect(a).not.toBe(b);
    expect(a.length).toBeGreaterThanOrEqual(8);
  });
});
