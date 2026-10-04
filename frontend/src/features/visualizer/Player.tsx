import { Button, Select } from '@arco-design/web-react';
import { IconClose, IconExpand, IconInfoCircle, IconPlus, IconShrink, IconSwap } from '@arco-design/web-react/icon';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { VizDataset, VizEpisode } from '../../api/types';
import { PALETTE } from '../../lib/vizCurves';
import {
  cellHeight,
  cellValid,
  CELL_GAP_PX,
  fitCells,
  GRID_SIZES,
  gridKey,
  parseGridKey,
  resizeCells,
  smartColumns,
  templateLayout,
  type CellContent,
  type GridShape,
  type LayoutTemplate,
} from '../../lib/vizLayout';
import { fmtClock, frameAt, frameCount, segmentAt, stepFrom } from '../../lib/vizTime';
import { zh } from '../../locales/zh';
import { CellMenu } from './CellMenu';
import { CurveCell, type Band } from './cells/CurveCell';
import { FramesCell } from './cells/FramesCell';
import { canDecode, VideoCell } from './cells/VideoCell';
import { PlayerClock } from './clock';
import { useVizEpisode, useVizModel, useVizSeries, type VizRef } from './data';
import { SidePanel } from './SidePanel';
import { Progress, Transport, type Evidence, type TimelineInfo } from './Transport';
import { useClockValue } from './useClock';
import './visualizer.css';

export type { Evidence } from './Transport';

/** What a page around the player may do to it. */
export interface PlayerControl {
  /** Puts content into the first empty cell, or replaces the last cell; says which. */
  add(c: CellContent): 'empty' | 'replaced';
}

export interface PlayerProps {
  source: VizRef;
  index: number;
  /** full: the visualize page; mini: the report / adjudication / task-detail modal */
  mode?: 'full' | 'mini';
  onPrevEpisode?: () => void;
  onNextEpisode?: () => void;
  /** findings on this episode (mini) */
  evidence?: readonly Evidence[];
  focusEvidence?: string | null;
  onFocusEvidence?: (id: string) => void;
  /** episode seconds to start at (a finding's start) */
  startAt?: number | null;
  sidebar?: boolean;
  /** the clock, for tools that read it (the drift check) */
  onClock?: (clock: PlayerClock | null) => void;
  /** filled with the player's PlayerControl while it is mounted */
  control?: React.MutableRefObject<PlayerControl | null>;
}

/** The player: loads the model and the episode, keeps the last episode on screen while the next loads. */
export function Player(props: PlayerProps) {
  const model = useVizModel(props.source);
  const episode = useVizEpisode(props.source, props.index);
  if (model.isError || (!model.isLoading && !model.data)) {
    return (
      <div className={`vz${props.mode === 'mini' ? ' is-mini' : ''}`}>
        <div className="vz-skeleton" role="alert">
          {zh.viz.modelFailed}
          {model.error instanceof Error ? `：${model.error.message}` : ''}
        </div>
      </div>
    );
  }
  if (episode.isError && !episode.data) {
    return (
      <div className={`vz${props.mode === 'mini' ? ' is-mini' : ''}`}>
        <div className="vz-skeleton" role="alert">
          {zh.viz.episodeFailed}
          {episode.error instanceof Error ? `：${episode.error.message}` : ''}
        </div>
      </div>
    );
  }
  if (!model.data || !episode.data) {
    return (
      <div className={`vz${props.mode === 'mini' ? ' is-mini' : ''}`}>
        <div className="vz-skeleton" role="status">
          {model.data ? zh.viz.loadingEpisode : zh.viz.loadingModel}
        </div>
      </div>
    );
  }
  return <PlayerView {...props} model={model.data} ep={episode.data} loadingNext={episode.data.index !== props.index} />;
}

interface Layout {
  cells: CellContent[];
  shape: GridShape;
}

function PlayerView({
  source,
  mode = 'full',
  model,
  ep,
  loadingNext,
  onPrevEpisode,
  onNextEpisode,
  evidence = [],
  focusEvidence = null,
  onFocusEvidence,
  startAt = null,
  sidebar = false,
  onClock,
  control,
}: PlayerProps & { model: VizDataset; ep: VizEpisode; loadingNext: boolean }) {
  const full = mode === 'full';
  // one clock per episode; the cleanup only stops it (StrictMode runs it and then the effect again on
  // the same clock - a one-way dispose there would leave the cells on a dead clock)
  const clock = useMemo(() => new PlayerClock(ep.duration_s), [ep]);
  useEffect(() => {
    onClock?.(clock);
    return () => {
      clock.pause();
      onClock?.(null);
    };
  }, [clock, onClock]);
  useEffect(() => {
    if (startAt !== null && startAt !== undefined) clock.seek(startAt);
  }, [clock, startAt]);

  const tl: TimelineInfo = useMemo(() => ({ timeline: ep.timeline, frames: frameCount(ep), duration: ep.duration_s }), [ep]);

  // -- the grid
  const wrap = useRef<HTMLDivElement>(null);
  const [gridW, setGridW] = useState(0);
  useEffect(() => {
    const el = wrap.current;
    if (!el) return undefined;
    const measure = () => setGridW(Math.max(0, el.clientWidth - 24));
    measure();
    if (typeof ResizeObserver !== 'function') return undefined;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const [template, setTemplate] = useState<LayoutTemplate>('smart');
  const [layout, setLayout] = useState<Layout | null>(null);
  const [max, setMax] = useState<number | null>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const [sideOpen, setSideOpen] = useState(sidebar);
  const [menu, setMenu] = useState<{ i: number; left: number; top: number } | null>(null);
  const smartCols = smartColumns(gridW || 1200);
  useEffect(() => {
    if (template === 'custom') return;
    const next = templateLayout(template, model, template === 'smart' ? (smartCols === 3 ? 1200 : 900) : gridW || 1200);
    if (next) {
      setLayout(next);
      setMax(null);
    }
  }, [template, model, smartCols, gridW]);
  // cells the model no longer has (another dataset's layout) become empty
  const cells = useMemo(() => (layout ? layout.cells.map((c) => (cellValid(c, model) ? c : ({ kind: 'empty' } as CellContent))) : []), [layout, model]);
  const shape = layout?.shape ?? { cols: 1, rows: 1 };

  const setCustom = (cellsNext: CellContent[], shapeNext: GridShape = shape) => {
    setTemplate('custom');
    setLayout({ cells: fitCells(cellsNext, shapeNext), shape: shapeNext });
  };
  const setCell = (i: number, c: CellContent) => {
    const next = [...cells];
    next[i] = c;
    setCustom(next);
    setFocus(i);
  };

  useEffect(() => {
    if (!control) return undefined;
    control.current = {
      add: (c) => {
        const free = cells.findIndex((x) => x.kind === 'empty');
        const at = free >= 0 ? free : Math.max(0, cells.length - 1);
        setMax(null);
        setCell(at, c);
        return free >= 0 ? 'empty' : 'replaced';
      },
    };
    return () => {
      control.current = null;
    };
  });

  // -- curves shown, per group
  const [hidden, setHidden] = useState<Record<string, string[]>>({});
  const hiddenOf = useCallback((key: string) => new Set(hidden[key] ?? []), [hidden]);
  const toggle = (key: string, name: string) =>
    setHidden((h) => {
      const cur = new Set(h[key] ?? []);
      if (cur.has(name)) cur.delete(name);
      else cur.add(name);
      return { ...h, [key]: [...cur] };
    });

  // -- annotations: the subtitle's track
  const tracks = ep.annotations.tracks;
  const [trackKey, setTrackKey] = useState<string | null>(null);
  const track = tracks.find((t) => t.key === trackKey) ?? tracks.find((t) => t.primary) ?? tracks[0] ?? null;
  const otherTracks = tracks.filter((t) => t !== track);

  // -- findings
  const bands: Band[] = useMemo(() => evidence.map((e) => ({ id: e.id, level: e.level, start: e.start, end: e.end })), [evidence]);
  const focusEv = (id: string) => {
    const ev = evidence.find((e) => e.id === id);
    if (ev) {
      clock.pause();
      clock.seek(ev.start);
    }
    onFocusEvidence?.(id);
  };

  // -- keyboard (only with the focus inside the player, never in a field)
  const onKey = (e: React.KeyboardEvent) => {
    const tag = (e.target as HTMLElement).tagName;
    if (/INPUT|SELECT|TEXTAREA/.test(tag) || (e.target as HTMLElement).isContentEditable) return;
    if (e.code === 'Space') {
      e.preventDefault();
      clock.toggle();
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      const dir = e.key === 'ArrowLeft' ? -1 : 1;
      clock.pause();
      const t = clock.getSnapshot().t;
      clock.seek(e.shiftKey ? t + dir : stepFrom(tl.timeline, tl.frames, t, dir));
    }
  };

  const cameraColor = (key: string) => PALETTE[Math.max(0, model.cameras.findIndex((c) => c.key === key)) % PALETTE.length];
  const rowH = cellHeight(gridW || 1200, max !== null ? 1 : shape.cols);
  const focusedCell = focus !== null && focus < cells.length ? cells[focus] : null;
  const focusedStream = focusedCell?.kind === 'curve' ? focusedCell.key : null;

  const openMenu = (i: number, el: HTMLElement) => {
    const w = wrap.current?.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    if (!w) return;
    setMenu({ i, left: Math.max(0, Math.min(r.right - w.left - 236, w.width - 240)), top: r.top - w.top + 36 });
  };

  return (
    <div
      className={`vz${full ? '' : ' is-mini'}`}
      tabIndex={0}
      onKeyDown={onKey}
      onPointerDown={(e) => {
        if (!(e.target as HTMLElement).closest('input,select,textarea,.arco-select')) (e.currentTarget as HTMLElement).focus({ preventScroll: true });
      }}
      data-testid="vz-player"
    >
      <Head model={model} ep={ep} clock={clock} loadingNext={loadingNext}>
        {full ? (
          <>
            <Select size="small" style={{ width: 210 }} value={template} onChange={(v: LayoutTemplate) => setTemplate(v)} aria-label={zh.viz.layoutTitle}>
              {(['smart', 'video', 'curve', 'custom'] as const).map((k) => (
                <Select.Option key={k} value={k}>
                  {zh.viz.layouts[k]}
                </Select.Option>
              ))}
            </Select>
            <Select
              size="small"
              style={{ width: 84 }}
              value={gridKey(shape)}
              onChange={(v: string) => {
                const s = parseGridKey(v);
                if (s) setCustom(resizeCells(cells, s), s);
              }}
              aria-label={zh.viz.gridTitle}
            >
              {GRID_SIZES.map((g) => (
                <Select.Option key={gridKey(g)} value={gridKey(g)}>
                  {zh.viz.grid(g.cols, g.rows)}
                </Select.Option>
              ))}
            </Select>
          </>
        ) : null}
        <Button size="small" className={sideOpen ? 'on' : ''} title={zh.viz.infoTitle} icon={<IconInfoCircle />} onClick={() => setSideOpen((v) => !v)}>
          {zh.viz.info}
        </Button>
      </Head>
      {!full && evidence.length ? (
        <div className="vz-evlist">
          {evidence.map((ev) => (
            <button key={ev.id} type="button" className={`chip ${ev.level}${ev.id === focusEvidence ? ' on' : ''}`} onClick={() => focusEv(ev.id)}>
              <i className="dot" />
              <code>{ev.item}</code>
              {ev.label}
            </button>
          ))}
        </div>
      ) : null}
      {[...model.warnings, ...ep.warnings, ...ep.annotations.warnings].map((w, i) => (
        <div key={`${w.code}-${i}`} className="vz-warn">
          {w.message}
        </div>
      ))}
      <div className="vz-body">
        <div className="vz-grid-wrap" ref={wrap}>
          {cells.length === 0 || (model.cameras.length === 0 && model.streams.length === 0) ? <div className="vz-skeleton">{zh.viz.noCells}</div> : null}
          <div
            className="vz-grid"
            style={{
              gridTemplateColumns: max !== null ? '1fr' : `repeat(${shape.cols}, minmax(0, 1fr))`,
              gridAutoRows: `${max !== null ? rowH * shape.rows + CELL_GAP_PX * (shape.rows - 1) : rowH}px`,
            }}
          >
            {cells.map((c, i) => {
              const hiddenCell = max !== null && max !== i;
              const cls = `vz-cell kind-${c.kind}${focus === i ? ' is-focus' : ''}${hiddenCell ? ' is-hidden' : ''}`;
              const tools =
                c.kind === 'empty' ? null : (
                  <div className="vz-tools">
                    <button
                      type="button"
                      title={zh.viz.cell.swapTitle}
                      onClick={(e) => {
                        e.stopPropagation();
                        openMenu(i, e.currentTarget.closest('.vz-cell') as HTMLElement);
                      }}
                    >
                      <IconSwap />
                      {zh.viz.cell.swap}
                    </button>
                    <button
                      type="button"
                      title={max === i ? zh.viz.cell.restore : zh.viz.cell.expandTitle}
                      onClick={(e) => {
                        e.stopPropagation();
                        setMax(max === i ? null : i);
                        setFocus(i);
                      }}
                    >
                      {max === i ? <IconShrink /> : <IconExpand />}
                      {max === i ? zh.viz.cell.restore : zh.viz.cell.expand}
                    </button>
                    {full ? (
                      <button
                        type="button"
                        title={zh.viz.cell.clear}
                        aria-label={zh.viz.cell.clear}
                        onClick={(e) => {
                          e.stopPropagation();
                          if (max === i) setMax(null);
                          setCell(i, { kind: 'empty' });
                        }}
                      >
                        <IconClose />
                      </button>
                    ) : null}
                  </div>
                );
              if (c.kind === 'video') {
                const cam = model.cameras.find((x) => x.key === c.key);
                const e = ep.cameras.find((x) => x.key === c.key);
                if (!cam || !e) return <div key={i} className={cls} />;
                const transcoded = cam.transcoded || e.transcoded || (e.access !== 'frames' && !!e.transcode_url && !canDecode(cam.codec_string));
                return (
                  <div key={i} className={cls} onPointerDown={() => setFocus(i)} data-testid={`vz-cell-${i}`}>
                    {cam.kind === 'frames' || e.access === 'frames' ? <FramesCell cam={cam} ep={e} clock={clock} /> : <VideoCell cam={cam} ep={e} clock={clock} />}
                    <span className="vz-cap">
                      <i className="dot" style={{ color: cameraColor(cam.key) }} />
                      {cam.name}
                      {transcoded ? (
                        <span className="vz-tc" title={zh.viz.video.transcodeTip(cam.codec)}>
                          {zh.viz.video.transcodeTag}
                        </span>
                      ) : null}
                    </span>
                    <Stamp clock={clock} tl={tl} />
                    {tools}
                  </div>
                );
              }
              if (c.kind === 'curve') {
                const stream = model.streams.find((x) => x.key === c.key);
                if (!stream) return <div key={i} className={cls} />;
                return (
                  <div key={i} className={cls} onPointerDown={() => setFocus(i)} data-testid={`vz-cell-${i}`}>
                    <CurveCell
                      source={source}
                      index={ep.index}
                      stream={stream}
                      duration={ep.duration_s}
                      clock={clock}
                      hidden={hiddenOf(stream.key)}
                      onToggle={(name) => toggle(stream.key, name)}
                      bands={bands}
                      focusBand={focusEvidence}
                    />
                    {tools}
                  </div>
                );
              }
              return (
                <div key={i} className={cls} onPointerDown={() => setFocus(i)} data-testid={`vz-cell-${i}`}>
                  <button type="button" className="plus" onClick={(e) => openMenu(i, e.currentTarget.closest('.vz-cell') as HTMLElement)}>
                    <span className="ring">
                      <IconPlus />
                    </span>
                    {zh.viz.cell.choose}
                  </button>
                </div>
              );
            })}
          </div>
          {menu ? (
            <CellMenu
              at={{ left: menu.left, top: menu.top }}
              current={cells[menu.i] ?? { kind: 'empty' }}
              cameras={model.cameras}
              streams={model.streams}
              cameraColor={cameraColor}
              onPick={(c) => {
                setCell(menu.i, c);
                setMenu(null);
              }}
              onClose={() => setMenu(null)}
            />
          ) : null}
        </div>
        {sideOpen ? (
          <SideFor
            source={source}
            ep={ep}
            model={model}
            clock={clock}
            focused={focusedCell}
            hidden={focusedStream ? hiddenOf(focusedStream) : new Set()}
            onToggle={(name) => focusedStream && toggle(focusedStream, name)}
            trackKey={track?.key ?? null}
            onTrack={setTrackKey}
            onClose={() => setSideOpen(false)}
            points={2000}
          />
        ) : null}
      </div>
      <Transport
        clock={clock}
        tl={tl}
        onPrevEpisode={full ? onPrevEpisode : undefined}
        onNextEpisode={full ? onNextEpisode : undefined}
        progress={
          <Progress
            clock={clock}
            tl={tl}
            track={track}
            others={otherTracks}
            evidence={evidence}
            events={ep.annotations.events}
            focusEvidence={focusEvidence}
            onFocusEvidence={focusEv}
          />
        }
      />
      {track ? <Subtitle clock={clock} track={track} /> : null}
    </div>
  );
}

/** "LeRobot v3", "mcap" ... */
export function formatLabel(f: VizDataset['format']): string {
  if (f.kind === 'lerobot') return `LeRobot ${f.version ?? ''}`.trim();
  return f.kind;
}

function Head({ model, ep, clock, loadingNext, children }: { model: VizDataset; ep: VizEpisode; clock: PlayerClock; loadingNext: boolean; children: React.ReactNode }) {
  const waiting = useClockValue(clock, (s) => s.playing && s.waiting);
  const fps = ep.fps ?? ep.timeline.fps ?? model.fps;
  return (
    <div className="vz-head">
      <span className="ep" title={model.name}>
        {model.name}
        <small>{`ep ${ep.index}`}</small>
      </span>
      <span className="meta">
        {formatLabel(model.format)}
        {fps ? (
          <>
            <i />
            {zh.viz.fps(fps)}
          </>
        ) : null}
        <i />
        {zh.viz.framesDuration(frameCount(ep), fmtClock(ep.duration_s))}
      </span>
      <span className={`task${ep.task ? '' : ' none'}`} title={ep.task?.text ?? ''}>
        {ep.task ? (
          <>
            <span className="k">{zh.viz.task}</span>
            {ep.task.text}
          </>
        ) : (
          zh.viz.noTask
        )}
      </span>
      {waiting || loadingNext ? <span className="waiting">{loadingNext ? zh.viz.loadingEpisode : zh.viz.waiting}</span> : null}
      <span className="ctl">{children}</span>
    </div>
  );
}

function Stamp({ clock, tl }: { clock: PlayerClock; tl: TimelineInfo }) {
  const f = useClockValue(clock, (s) => frameAt(tl.timeline, tl.frames, s.t));
  const t = useClockValue(clock, (s) => Math.round(s.t * 10) / 10);
  return <span className="vz-stamp">{zh.viz.stamp(fmtClock(t), f)}</span>;
}

function Subtitle({ clock, track }: { clock: PlayerClock; track: NonNullable<VizEpisode['annotations']['tracks'][number]> }) {
  const k = useClockValue(clock, (s) => segmentAt(track.segments, s.t));
  const seg = k >= 0 ? track.segments[k] : null;
  return (
    <div className="vz-sub" data-testid="vz-sub">
      <span className="idx">{seg ? zh.viz.sub.step(k + 1, track.segments.length) : ''}</span>
      <span className="lab">{seg ? seg.label : zh.viz.sub.gap}</span>
      {seg ? <span className="rng">{`${seg.start_s.toFixed(1)}–${seg.end_s.toFixed(1)} s`}</span> : null}
      {seg?.quality === 'unqualified' ? <span className="arco-tag arco-tag-orange arco-tag-size-small">{zh.viz.sub.unqualified}</span> : null}
      <span className="src">{zh.viz.sub.source(`${track.name}${track.source ? ` · ${track.source}` : ''}`)}</span>
    </div>
  );
}

/** The info panel with the focused curve's series (the same request the cell made). */
function SideFor({
  source,
  ep,
  model,
  focused,
  points,
  ...rest
}: {
  source: VizRef;
  ep: VizEpisode;
  model: VizDataset;
  clock: PlayerClock;
  focused: CellContent | null;
  hidden: ReadonlySet<string>;
  onToggle: (name: string) => void;
  trackKey: string | null;
  onTrack: (key: string) => void;
  onClose: () => void;
  points: number;
}) {
  const stream = focused?.kind === 'curve' ? focused.key : '';
  const series = useVizSeries(source, ep.index, stream, points, !!stream);
  return <SidePanel {...rest} focused={focused} model={model} ep={ep} series={stream ? series.data : undefined} />;
}
