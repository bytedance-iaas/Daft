// Dev and demo builds only: the service worker answers every C4 call from the mock world.
import { setupWorker } from 'msw/browser';
import { getBase } from '../base';
import { resetDb } from './db';
import { handlers } from './handlers';
import { sseHandlers } from './sse';

export async function startMockWorker(): Promise<void> {
  resetDb();
  const base = getBase();
  const worker = setupWorker(...handlers, ...sseHandlers);
  await worker.start({
    serviceWorker: { url: `${base}/mockServiceWorker.js`, options: { scope: `${base}/` } },
    onUnhandledRequest: 'bypass',
    quiet: true,
  });
  // A hint for people poking at the dev console.
  console.info('[curator] mock API is on (msw); set VITE_API_TARGET to use a real Daemon');
}
