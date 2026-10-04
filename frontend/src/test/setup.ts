// Vitest setup: the browser APIs jsdom lacks (first, before anything imports Arco), jest-dom
// matchers, the MSW server with the mock world, and contract validation of every request body
// the UI sends (a violation fails the test that caused it).
import './polyfills';
import '@testing-library/jest-dom/vitest';
import { Message, Modal } from '@arco-design/web-react';
import { cleanup, configure } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, beforeEach, expect } from 'vitest';
import { resetDb } from '../mocks/db';
import { setRequestValidator } from '../mocks/handlers';
import { server } from '../mocks/server';
import { formatErrors, makeContract } from './contract';

// Every page is a lazy route and a CI runner is two to four times slower than a laptop: the first find
// in a file waits for the page's chunk to load and render (the report page, with ECharts and the
// visualizer, took 1.2 s under Node 22 in CI), so finds and waitFor give up after 5 s, not 1 s.
configure({ asyncUtilTimeout: 5000 });

export const contract = makeContract();

const violations: string[] = [];

setRequestValidator((operationId, body) => {
  const ref = contract.requestRef(operationId);
  if (!ref) return;
  const v = contract.validator(ref);
  if (!v(body)) {
    const msg = `request body of ${operationId} violates the contract:\n${formatErrors(v.errors)}\n${JSON.stringify(body)}`;
    violations.push(msg);
    throw new Error(msg);
  }
});

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
beforeEach(() => {
  resetDb();
  violations.length = 0;
});
afterEach(() => {
  cleanup();
  // Modal.confirm and Message render into their own roots, outside RTL's containers.
  Modal.destroyAll();
  Message.clear();
  server.resetHandlers();
  server.events.removeAllListeners();
  window.localStorage.clear();
  expect(violations, violations.join('\n\n')).toEqual([]);
});
afterAll(() => server.close());
