import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';

/**
 * One figure of a `.stat-grid`. A value too long for one column takes two instead of being cut
 * (requester, third round: the cards keep some room); once widened it stays so.
 */
export function StatCell({
  label,
  value,
  foot,
  tone,
  testId,
}: {
  label: ReactNode;
  value: ReactNode;
  foot?: ReactNode;
  tone?: 'bad' | 'warn' | 'good';
  testId?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [wide, setWide] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || wide) return;
    const check = () => {
      if (el.scrollWidth > el.clientWidth + 1) setWide(true);
    };
    check();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(check);
    observer.observe(el);
    return () => observer.disconnect();
  }, [wide]);
  return (
    <div className={`stat-cell${tone ? ` tone-${tone}` : ''}${wide ? ' wide' : ''}`} data-testid={testId}>
      <div className="stat-label">{label}</div>
      <div className="stat-value" ref={ref}>
        {value}
      </div>
      {foot ? <div className="stat-foot">{foot}</div> : null}
    </div>
  );
}
