import { Button, Checkbox, InputNumber, Radio, Slider, Switch } from '@arco-design/web-react';
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { VizEpisodeStream, VizStream } from '../../../api/types';
import { colorize, decodePng16, gradient, pixelAt, type DepthPicture, type DepthView } from '../../../lib/vizDepth';
import { zh } from '../../../locales/zh';
import type { PlayerClock } from '../clock';
import { useStreamIndex } from '../data';
import { FramePack, type Drawable } from '../framePack';
import { drawFrame, usePackPainter } from './FramesCell';

/** Decoded depth frames kept per cell (a 640 × 480 frame is 600 KB of millimetres plus its colours)... */
const DEPTH_CACHE = 32;
/** ...and decoded ahead while playing: fewer than kept, or the frame shown would be pushed out (0.4 s at 30 fps). */
const DEPTH_AHEAD = 12;

/** A colour picture of a depth frame (a canvas where createImageBitmap is missing: jsdom). */
async function toDrawable(rgba: Uint8ClampedArray<ArrayBuffer>, w: number, h: number): Promise<Drawable> {
  const data = new ImageData(rgba, w, h);
  if (typeof createImageBitmap === 'function') return createImageBitmap(data);
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  c.getContext('2d')?.putImageData?.(data, 0, 0);
  return c as unknown as Drawable;
}

/** Draws over a camera (transparent around the picture) or on the dark background. */
function depthPainter(overlay: boolean) {
  return (canvas: HTMLCanvasElement, img: Drawable | undefined) => {
    if (!overlay) {
      drawFrame(canvas, img);
      return;
    }
    const ctx = canvas.getContext('2d');
    if (!ctx || typeof ctx.clearRect !== 'function') return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!img) return;
    const iw = img.width;
    const ih = img.height;
    if (!iw || !ih) return;
    const k = Math.min(canvas.width / iw, canvas.height / ih);
    ctx.drawImage(img, (canvas.width - iw * k) / 2, (canvas.height - ih * k) / 2, iw * k, ih * k);
  };
}

/**
 * A depth cell (design doc 21 §5.5, D68): the depth picture at or before t, decoded from its 16-bit PNG
 * pack and coloured between the cell's range (by default the episode's 2 % / 98 %); holes are
 * transparent. The value under the pointer is read from the decoded frame. With `overlay` the depth is
 * drawn over its camera (`children`, the camera's own cell) at `view.opacity`.
 */
export function DepthCell({
  stream,
  ep,
  clock,
  view,
  overlay,
  children,
}: {
  stream: VizStream;
  ep: VizEpisodeStream;
  clock: PlayerClock;
  view: DepthView;
  overlay: boolean;
  children?: React.ReactNode;
}) {
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const index = useStreamIndex(ep.url ? ep.index_url : null);
  const ready = index.data?.state === 'ready' ? index.data.index : null;
  const lo = view.lo ?? ready?.depth?.lo ?? 0;
  const hi = Math.max(lo + 1, view.hi ?? ready?.depth?.hi ?? 4000);
  // the millimetres of each colour picture, for the value under the pointer
  const raws = useRef(new WeakMap<object, DepthPicture>());
  const decode = useCallback(
    async (blob: Blob) => {
      const pic = await decodePng16(new Uint8Array(await blob.arrayBuffer()));
      const img = await toDrawable(colorize(pic, lo, hi, view.cmap), pic.width, pic.height);
      raws.current.set(img, pic);
      return img;
    },
    [lo, hi, view.cmap],
  );
  // a new range or colour map makes a new pack: its frames are fetched again (the browser's cache has them) and coloured anew
  const pack = useMemo(() => (ep.url && ready ? new FramePack(ep.url, ready, { decode, cache: DEPTH_CACHE, rawBytes: 48 << 20 }) : null), [ep.url, ready, decode]);
  useEffect(() => () => pack?.clear(), [pack]);
  const shown = useRef<Drawable | undefined>(undefined);
  const onShown = useCallback((img: Drawable | undefined) => {
    shown.current = img;
  }, []);
  const draw = useMemo(() => depthPainter(overlay), [overlay]);
  const sourceId = `${stream.key}:${useId()}`;
  const waiting = usePackPainter(canvas, pack, clock, sourceId, draw, onShown, DEPTH_AHEAD);

  const [hover, setHover] = useState<{ x: number; y: number; v: number } | null>(null);
  const onMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const img = shown.current;
    const pic = img ? raws.current.get(img) : undefined;
    const el = canvas.current;
    if (!pic || !el) return setHover(null);
    const r = el.getBoundingClientRect();
    const px = pixelAt(r.width, r.height, pic, e.clientX - r.left, e.clientY - r.top);
    return setHover(px ? { ...px, v: pic.data[px.y * pic.width + px.x] } : null);
  };

  let message: React.ReactNode = null;
  if (!ep.url) {
    message = (
      <span>
        {zh.viz.depth.unsupported}
        <span className="why">{ep.reason ?? ''}</span>
      </span>
    );
  } else if (index.data?.state === 'pending' || (index.isLoading && !index.data)) {
    message = zh.viz.depth.generating(index.data?.state === 'pending' ? index.data.progress : null);
  } else if (index.data?.state === 'failed' || index.isError) {
    message = (
      <span>
        {zh.viz.depth.failed}
        <span className="why">{index.data?.state === 'failed' ? index.data.message : ''}</span>
      </span>
    );
  } else if (waiting) {
    message = zh.viz.depth.loading;
  }

  return (
    <>
      {children}
      <canvas
        ref={canvas}
        className={`vz-depth${overlay ? ' over' : ''}`}
        style={overlay ? { opacity: view.opacity } : undefined}
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
        data-testid={`vz-depth-${stream.key}`}
      />
      {ready ? (
        // the colour scale; under the pointer, the value there instead of the range
        <div className={`vz-depth-scale${hover ? ' at' : ''}`} title={zh.viz.side.depthShown} data-testid="vz-depth-scale">
          <i style={{ background: gradient(view.cmap) }} />
          <span>{hover ? (hover.v ? zh.viz.depth.at(hover.x, hover.y, hover.v) : zh.viz.depth.hole(hover.x, hover.y)) : zh.viz.depth.range(lo, hi)}</span>
        </div>
      ) : null}
      {message ? (
        <div className="vz-overlay" role="status">
          {message}
        </div>
      ) : null}
    </>
  );
}

/** The depth cell's settings (design doc 21 §5.5): colour map, range, drawing over the camera and its opacity. */
export function DepthSettings({
  at,
  view,
  indexUrl,
  overlayWhyNot,
  onChange,
  onClose,
}: {
  /** where, in the grid's frame (beside the cell: a small cell would cut it off) */
  at: { left: number; top: number };
  view: DepthView;
  /** the stream's index: its 2 % / 98 % start a range of one's own */
  indexUrl: string | null;
  /** why it cannot be drawn over a camera, or null when it can */
  overlayWhyNot: string | null;
  onChange: (v: DepthView) => void;
  onClose: () => void;
}) {
  const index = useStreamIndex(indexUrl);
  const defaults = index.data?.state === 'ready' ? (index.data.index.depth ?? { lo: null, hi: null }) : { lo: null, hi: null };
  const auto = view.lo === null && view.hi === null;
  const box = useRef<HTMLDivElement>(null);
  // closes on Escape or a click outside (the select dropdowns of Arco render in the body: those stay)
  useEffect(() => {
    const down = (e: PointerEvent) => {
      const t = e.target as HTMLElement;
      if (box.current && !box.current.contains(t) && !t.closest('.arco-trigger-popup, .arco-slider')) onClose();
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
  return (
    <div ref={box} className="vz-depth-set" style={{ left: at.left, top: at.top }} role="dialog" aria-label={zh.viz.depth.settingsTitle} data-testid="vz-depth-set">
      <div className="row">
        <span className="k">{zh.viz.depth.cmap}</span>
        <Radio.Group type="button" size="mini" value={view.cmap} onChange={(v: DepthView['cmap']) => onChange({ ...view, cmap: v })}>
          <Radio value="turbo">{zh.viz.depth.turbo}</Radio>
          <Radio value="gray">{zh.viz.depth.gray}</Radio>
        </Radio.Group>
      </div>
      <div className="row">
        <span className="k">{zh.viz.depth.rangeLabel}</span>
        <Checkbox
          checked={auto}
          onChange={(on: boolean) => onChange(on ? { ...view, lo: null, hi: null } : { ...view, lo: defaults.lo ?? 0, hi: defaults.hi ?? 4000 })}
        >
          {zh.viz.depth.auto}
        </Checkbox>
      </div>
      {!auto ? (
        <div className="row">
          <InputNumber
            size="mini"
            min={0}
            max={65534}
            value={view.lo ?? undefined}
            prefix={zh.viz.depth.lo}
            aria-label={zh.viz.depth.lo}
            onChange={(v) => onChange({ ...view, lo: typeof v === 'number' ? v : 0 })}
          />
          <InputNumber
            size="mini"
            min={1}
            max={65535}
            value={view.hi ?? undefined}
            prefix={zh.viz.depth.hi}
            aria-label={zh.viz.depth.hi}
            onChange={(v) => onChange({ ...view, hi: typeof v === 'number' ? v : 1 })}
          />
        </div>
      ) : null}
      <div className="row" title={overlayWhyNot ?? ''}>
        <span className="k">{zh.viz.depth.overlay}</span>
        <Switch size="small" checked={view.overlay && !overlayWhyNot} disabled={!!overlayWhyNot} onChange={(on: boolean) => onChange({ ...view, overlay: on })} aria-label={zh.viz.depth.overlay} />
        {overlayWhyNot ? <span className="why">{overlayWhyNot}</span> : null}
      </div>
      {view.overlay && !overlayWhyNot ? (
        <div className="row">
          <span className="k">{zh.viz.depth.opacity}</span>
          <Slider style={{ flex: 1 }} min={0.1} max={1} step={0.05} value={view.opacity} onChange={(v) => onChange({ ...view, opacity: Number(v) })} />
        </div>
      ) : null}
      <div className="row end">
        <Button size="mini" onClick={onClose}>
          {zh.viz.depth.close}
        </Button>
      </div>
    </div>
  );
}
