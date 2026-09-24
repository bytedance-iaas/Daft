import { Tooltip } from '@arco-design/web-react';
import { useLayoutEffect, useRef, useState } from 'react';

/**
 * A table value on one line: cut with an ellipsis when the column is too narrow, the whole of it
 * in a tooltip then (fourth round). The tooltip is on only while the text is actually cut.
 */
export function OneLine({ text, mono }: { text: string; mono?: boolean }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [cut, setCut] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const check = () => setCut(el.scrollWidth > el.clientWidth + 1);
    check();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(check);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text]);
  return (
    <Tooltip content={text} disabled={!cut}>
      <span ref={ref} className={`one-line${mono ? ' mono' : ''}`} data-cut={cut || undefined}>
        {text}
      </span>
    </Tooltip>
  );
}
