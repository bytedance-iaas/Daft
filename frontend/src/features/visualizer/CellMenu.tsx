import { IconLineHeight } from '@arco-design/web-react/icon';
import { useEffect, useRef } from 'react';
import type { VizCamera, VizStream } from '../../api/types';
import type { CellContent } from '../../lib/vizLayout';
import { zh } from '../../locales/zh';

/**
 * 「选择 / 更换」 (design doc 18 §5.2): cameras, curve groups, and the streams this phase cannot draw
 * (depth, 3-D ...) greyed out with why. Closes on a pick, Escape or a click outside.
 */
export function CellMenu({
  at,
  current,
  cameras,
  streams,
  cameraColor,
  onPick,
  onClose,
}: {
  at: { left: number; top: number };
  current: CellContent;
  cameras: readonly VizCamera[];
  streams: readonly VizStream[];
  cameraColor: (key: string) => string;
  onPick: (c: CellContent) => void;
  onClose: () => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const down = (e: PointerEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) onClose();
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('pointerdown', down, true);
    document.addEventListener('keydown', key);
    return () => {
      document.removeEventListener('pointerdown', down, true);
      document.removeEventListener('keydown', key);
    };
  }, [onClose]);
  const curves = streams.filter((s) => s.kind === 'series' && s.available);
  const others = streams.filter((s) => !(s.kind === 'series' && s.available));
  return (
    <div ref={box} className="vz-pop" style={{ left: at.left, top: at.top }} role="menu" data-testid="vz-menu">
      {cameras.length ? <div className="grp">{zh.viz.menu.cameras}</div> : null}
      {cameras.map((c) => (
        <button
          key={c.key}
          type="button"
          role="menuitem"
          className={`it${current.kind === 'video' && current.key === c.key ? ' cur' : ''}`}
          onClick={() => onPick({ kind: 'video', key: c.key })}
        >
          <i className="dot" style={{ color: cameraColor(c.key) }} />
          {c.name}
          <span className="sub">{zh.viz.menu.size(c.width, c.height, c.codec)}</span>
        </button>
      ))}
      {curves.length ? <div className="grp">{zh.viz.menu.curves}</div> : null}
      {curves.map((s) => (
        <button
          key={s.key}
          type="button"
          role="menuitem"
          className={`it${current.kind === 'curve' && current.key === s.key ? ' cur' : ''}`}
          onClick={() => onPick({ kind: 'curve', key: s.key })}
        >
          <IconLineHeight />
          {s.name}
          <span className="sub">{zh.viz.menu.lines(new Set(s.lines.map((l) => l.name)).size)}</span>
        </button>
      ))}
      {others.length ? <div className="grp">{zh.viz.menu.other}</div> : null}
      {others.map((s) => (
        <div key={s.key} className="it dis" title={s.reason ?? ''}>
          {s.name}
          <span className="sub">{s.reason ?? zh.viz.menu.unavailable}</span>
        </div>
      ))}
    </div>
  );
}
