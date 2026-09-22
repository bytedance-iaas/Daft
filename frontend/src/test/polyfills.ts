// The browser APIs jsdom lacks. Imported first by setup.ts: Arco decides at import time whether a
// native ResizeObserver exists (resize-observer-polyfill), and its fallback calls getBBox, which
// jsdom does not have either.

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
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = NoopResizeObserver;
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

// ECharts' SVG renderer still measures text on a 2D canvas, which jsdom does not implement.
HTMLCanvasElement.prototype.getContext = function getContext() {
  return { font: '', measureText: (text: string) => ({ width: text.length * 7 }) };
} as unknown as typeof HTMLCanvasElement.prototype.getContext;

window.scrollTo = () => undefined;
Element.prototype.scrollIntoView = function scrollIntoView() {};
HTMLMediaElement.prototype.play = function play() {
  return Promise.resolve();
};
HTMLMediaElement.prototype.pause = function pause() {};
HTMLMediaElement.prototype.load = function load() {};
