// Dev and demo builds only: the service worker answers every C4 call from the mock world.
import { getResponse } from 'msw';
import { setupWorker } from 'msw/browser';
import { getBase } from '../base';
import { resetDb } from './db';
import { handlers } from './handlers';
import { sseHandlers } from './sse';

/**
 * Where service workers are unavailable (embedded browsers, some private modes) the mocks are
 * answered in-page by patching fetch; SSE then is not mocked, so the app shows its 5 s polling
 * fallback, which is worth seeing anyway.
 */
function installFetchFallback(): void {
  const original = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    if (new URL(request.url).pathname.startsWith(`${getBase()}/api/v1/`)) {
      const res = await getResponse(handlers, request);
      if (res) return res;
    }
    return original(input, init);
  };
}

export async function startMockWorker(): Promise<void> {
  resetDb();
  const base = getBase();
  const worker = setupWorker(...handlers, ...sseHandlers);
  try {
    await worker.start({
      serviceWorker: { url: `${base}/mockServiceWorker.js`, options: { scope: `${base}/` } },
      onUnhandledRequest: 'bypass',
      quiet: true,
    });
    console.info('[curator] mock API is on (msw); set VITE_API_TARGET to use a real Daemon');
  } catch (e) {
    console.warn('[curator] no service worker here; mocking fetch in the page instead (SSE falls back to polling)', e);
    installFetchFallback();
  }
}
