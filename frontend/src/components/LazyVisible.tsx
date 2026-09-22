import { useEffect, useRef, useState, type ReactNode } from 'react';

/** Renders children only once the placeholder scrolls into view (07 §9 lazy loading). */
export function LazyVisible({ children, placeholder, rootMargin = '200px' }: { children: ReactNode; placeholder?: ReactNode; rootMargin?: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || visible) return undefined;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisible(true);
          io.disconnect();
        }
      },
      { rootMargin },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [visible, rootMargin]);
  return <div ref={ref}>{visible ? children : placeholder ?? null}</div>;
}

/**
 * Calls onVisible whenever the sentinel becomes visible (infinite scrolling). Pass the number of
 * loaded pages as `version`: the observer is set up again after each page, so a sentinel that is
 * still on screen asks for the next page instead of waiting for a scroll that never comes.
 */
export function Sentinel({ onVisible, disabled, version = 0 }: { onVisible: () => void; disabled?: boolean; version?: number }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const cb = useRef(onVisible);
  cb.current = onVisible;
  useEffect(() => {
    const el = ref.current;
    if (!el || disabled) return undefined;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) cb.current();
    });
    io.observe(el);
    return () => io.disconnect();
  }, [disabled, version]);
  return <div ref={ref} style={{ height: 1 }} />;
}
