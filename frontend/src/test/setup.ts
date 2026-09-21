// Vitest setup: jest-dom matchers, the browser APIs jsdom lacks (Arco and our lazy loading rely
// on them), the MSW server with the mock world, and contract validation of every request body
// the UI sends (a violation fails the test that caused it).
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, beforeEach, expect } from 'vitest';
import { resetDb } from '../mocks/db';
import { setRequestValidator } from '../mocks/handlers';
import { server } from '../mocks/server';
import { formatErrors, makeContract } from './contract';

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
  server.resetHandlers();
  window.localStorage.clear();
  expect(violations, violations.join('\n\n')).toEqual([]);
});
afterAll(() => server.close());

if (!window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }) as MediaQueryList;
}

class NoopResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (!('ResizeObserver' in window)) {
  (window as unknown as { ResizeObserver: unknown }).ResizeObserver = NoopResizeObserver;
}

/** IntersectionObserver that reports every observed element as visible right away. */
class ImmediateIntersectionObserver {
  private readonly cb: IntersectionObserverCallback;
  constructor(cb: IntersectionObserverCallback) {
    this.cb = cb;
  }
  observe(target: Element) {
    const entry = { isIntersecting: true, target, intersectionRatio: 1 } as IntersectionObserverEntry;
    queueMicrotask(() => this.cb([entry], this as unknown as IntersectionObserver));
  }
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}
(window as unknown as { IntersectionObserver: unknown }).IntersectionObserver = ImmediateIntersectionObserver;

window.scrollTo = () => undefined;
Element.prototype.scrollIntoView = function scrollIntoView() {};
HTMLMediaElement.prototype.play = function play() {
  return Promise.resolve();
};
HTMLMediaElement.prototype.pause = function pause() {};
HTMLMediaElement.prototype.load = function load() {};
