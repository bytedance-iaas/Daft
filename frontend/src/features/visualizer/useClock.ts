import { useSyncExternalStore } from 'react';
import type { ClockSnapshot, PlayerClock } from './clock';

/**
 * One value derived from the clock; the component re-renders only when it changes (a frame
 * number, a rounded time). `select` must return a primitive.
 */
export function useClockValue<T extends string | number | boolean | null>(clock: PlayerClock, select: (s: ClockSnapshot) => T): T {
  const get = () => select(clock.getSnapshot());
  return useSyncExternalStore(clock.subscribe, get, get);
}
