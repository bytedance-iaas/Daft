// Vitest setup: jest-dom matchers and the browser APIs jsdom lacks (Arco and our lazy
// loading rely on them).
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

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
