// A controllable EventSource for SSE tests (jsdom has none).
type Listener = (e: { data: string; lastEventId?: string }) => void;

export class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static last(): FakeEventSource {
    const es = FakeEventSource.instances[FakeEventSource.instances.length - 1];
    if (!es) throw new Error('no EventSource was created');
    return es;
  }
  readonly url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners: Record<string, Listener[]> = {};
  private seq = 0;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, cb: Listener) {
    (this.listeners[type] ??= []).push(cb);
  }
  close() {
    this.readyState = 2;
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  emit(type: string, data: unknown) {
    this.seq += 1;
    for (const cb of this.listeners[type] ?? []) cb({ data: JSON.stringify(data), lastEventId: `1-${this.seq}` });
  }
  /** Drop the connection; `closed` means the browser gave up (no automatic reconnect). */
  fail(closed = false) {
    this.readyState = closed ? 2 : 0;
    this.onerror?.();
  }
}
