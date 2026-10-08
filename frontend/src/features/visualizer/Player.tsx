import { Button, Dropdown, Menu, Message, Modal, Select } from '@arco-design/web-react';
import { IconClose, IconExpand, IconInfoCircle, IconLayout, IconPlus, IconSettings, IconShrink, IconSwap } from '@arco-design/web-react/icon';
import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { VizCamera, VizDataset, VizEpisode, VizEpisodeCamera, VizStream } from '../../api/types';
import { CAMERA_PALETTE } from '../../lib/vizCurves';
import { errorMessage } from '../../api/errors';
import { DEFAULT_DEPTH_VIEW, overlayFits } from '../../lib/vizDepth';
import { hasLayout, startOf, withLayout, withoutLayout } from '../../lib/vizDisplay';
import {
  cellHeight,
  cellValid,
  cellWidth,
  CELL_GAP_PX,
  fitCells,
  GRID_SIZES,
  gridKey,
  NARROW_CELL_PX,
  parseGridKey,
  resizeCells,
  templateLayout,
  widthClass,
  type CellContent,
  type GridShape,
  type LayoutTemplate,
} from '../../lib/vizLayout';
import { fmtClock, frameAt, frameCount, segmentAt, stepFrom } from '../../lib/vizTime';
import { zh } from '../../locales/zh';
import { CellMenu } from './CellMenu';
import { CurveCell, type Band } from './cells/CurveCell';
import { DepthCell, DepthSettings } from './cells/DepthCell';
import { FramesCell } from './cells/FramesCell';
import { SamplesCell } from './cells/SamplesCell';
import { canDecode, VideoCell } from './cells/VideoCell';
import { PlayerClock } from './clock';
import { fetchVizDisplay, prefetchVizEpisode, saveVizDisplay, useVizEpisode, useVizModel, useVizSeries, type VizRef } from './data';
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
  /** cells to show (the mini player's layout for the focused finding); a new one replaces the grid */
  arrangement?: Layout | null;
  /** a control before the title (the visualize page's 展开侧栏 while its rail is folded); kept while loading */
  lead?: React.ReactNode;
  /** the episode to prepare once this one is on screen (the visualize page's next one) */
  prefetch?: number | null;
}

/** The player: loads the model and the episode, keeps the last episode on screen while the next loads. */
export function Player(props: PlayerProps) {
  const model = useVizModel(props.source);
  const episode = useVizEpisode(props.source, props.index);
  const lead = props.lead ? (
    <div className="vz-head">
      <span className="lead">{props.lead}</span>
    </div>
  ) : null;
  if (model.isError || (!model.isLoading && !model.data)) {
    return (
      <div className={`vz${props.mode === 'mini' ? ' is-mini' : ''}`}>
        {lead}
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
        {lead}
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
        {lead}
        <div className="vz-skeleton" role="status">
          {!model.data ? zh.viz.loadingModel : model.data.format.reader === 'mcap' ? zh.viz.loadingMcap : zh.viz.loadingEpisode}
        </div>
      </div>
    );
  }
  // one view per dataset: another episode of it keeps the layout, the hidden lines, the track and playback
  return <PlayerView key={`${props.source.scope}:${props.source.id}`} {...props} model={model.data} ep={episode.data} loadingNext={episode.data.index !== props.index} />;
}

/** The sizes offered, with the grid's own when a template made one that is not among them (2 × 3, 3 × 4). */
function gridSizes(shape: GridShape): readonly GridShape[] {
  if (GRID_SIZES.some((g) => g.cols === shape.cols && g.rows === shape.rows)) return GRID_SIZES;
  return [...GRID_SIZES, shape].sort((a, b) => a.cols * a.rows - b.cols * b.rows || a.cols - b.cols);
}

export interface Layout {
  cells: CellContent[];
  shape: GridShape;
  /** cameras a template left out (more than the grid holds); 0 once the user arranges the cells */
  overflow?: number;
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
  arrangement = null,
  lead,
  prefetch = null,
}: PlayerProps & { model: VizDataset; ep: VizEpisode; loadingNext: boolean }) {
  const full = mode === 'full';
  // once this episode is on screen, the next one is prepared in the background: clicking 下一条 on an mcap
  // dataset then finds its scan done (design doc 18 §10, 预生成 for one episode ahead)
  const qc = useQueryClient();
  useEffect(() => {
    if (!loadingNext && prefetch !== null && prefetch !== ep.index) void prefetchVizEpisode(qc, source, prefetch);
  }, [qc, source, prefetch, loadingNext, ep.index]);
  // where the dataset's display configuration starts the full player (design doc 21 §6.4), taken once
  const [start] = useState(() => startOf(full && !arrangement ? model.display : null, model));
  // speed and looping carry over to the next episode's clock
  const playback = useRef<{ speed: number; loop: boolean }>({ speed: start.speed, loop: start.loop });
  // one clock per episode; the cleanup only stops it (StrictMode runs it and then the effect again on
  // the same clock - a one-way dispose there would leave the cells on a dead clock)
  const clock = useMemo(() => {
    const c = new PlayerClock(ep.duration_s);
    c.setSpeed(playback.current.speed);
    c.setLoop(playback.current.loop);
    return c;
  }, [ep]);
  useEffect(
    () =>
      clock.subscribe(() => {
        const s = clock.getSnapshot();
        playback.current = { speed: s.speed, loop: s.loop };
      }),
    [clock],
  );
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
  const [template, setTemplate] = useState<LayoutTemplate>(arrangement ? 'custom' : start.template);
  const [layout, setLayout] = useState<Layout | null>(arrangement ?? start.layout);
  useEffect(() => {
    if (!arrangement) return;
    setTemplate('custom');
    setLayout(arrangement);
    setMax(null);
  }, [arrangement]);
  const [max, setMax] = useState<number | null>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const [sideOpen, setSideOpen] = useState(sidebar);
  const [menu, setMenu] = useState<{ i: number; left: number; top: number } | null>(null);
  // the depth cell whose settings are open, and where (design doc 21 §5.5): beside the grid, not in the cell
  const [settings, setSettings] = useState<{ i: number; left: number; top: number } | null>(null);
  // the templates only tell a few widths apart: the grid is laid out again when the width crosses one
  const wclass = widthClass(gridW || 1200);
  useEffect(() => {
    if (template === 'custom') return;
    const next = templateLayout(template, model, wclass);
    if (next) {
      setLayout(next);
      setMax(null);
    }
  }, [template, model, wclass]);
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

  // -- cameras the browser decodes itself (design doc 19 §3): the info panel says so
  const [clientKeys, setClientKeys] = useState<ReadonlySet<string>>(new Set());
  const onDecodeMode = useCallback((key: string, client: boolean) => {
    setClientKeys((cur) => {
      if (cur.has(key) === client) return cur;
      const next = new Set(cur);
      if (client) next.add(key);
      else next.delete(key);
      return next;
    });
  }, []);

  // -- cells the platform is transcoding: 播放 waits for them (requester 2026-10-05)
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const onBusy = useCallback((id: string, on: boolean) => {
    setBusy((cur) => {
      if (cur.has(id) === on) return cur;
      const next = new Set(cur);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);
  const playBlocked = busy.size ? zh.viz.playBlocked : null;

  // -- curves shown, per group
  const [hidden, setHidden] = useState<Record<string, string[]>>(start.hidden);
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
  const [trackKey, setTrackKey] = useState<string | null>(start.track);
  const track = tracks.find((t) => t.key === trackKey) ?? tracks.find((t) => t.primary) ?? tracks[0] ?? null;
  const otherTracks = tracks.filter((t) => t !== track);

  // -- findings
  const bands: Band[] = useMemo(
    () => evidence.flatMap((e) => (e.start !== null && e.end !== null ? [{ id: e.id, level: e.level, start: e.start, end: e.end }] : [])),
    [evidence],
  );
  const focusEv = (id: string) => {
    const ev = evidence.find((e) => e.id === id);
    if (ev && ev.start !== null) {
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
      if (playBlocked && !clock.getSnapshot().playing) return;
      clock.toggle();
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      const dir = e.key === 'ArrowLeft' ? -1 : 1;
      clock.pause();
      const t = clock.getSnapshot().t;
      clock.seek(e.shiftKey ? t + dir : stepFrom(tl.timeline, tl.frames, t, dir));
    }
  };

  const cameraColor = (key: string) => CAMERA_PALETTE[Math.max(0, model.cameras.findIndex((c) => c.key === key)) % CAMERA_PALETTE.length];
  // a camera as its cell shows it (frame pack, the browser's decoder, or <video>); also under a depth picture
  const cameraView = (cam: VizCamera, e: VizEpisodeCamera) => {
    const decodes = !!e.samples_url && !!e.index_url && cam.kind === 'video';
    if (cam.kind === 'frames' || e.access === 'frames') return <FramesCell cam={cam} ep={e} clock={clock} />;
    if (decodes) return <SamplesCell cam={cam} ep={e} clock={clock} onMode={onDecodeMode} onBusy={onBusy} />;
    return <VideoCell cam={cam} ep={e} clock={clock} onBusy={onBusy} />;
  };
  // why a depth stream cannot be drawn over its camera, or null when it can
  const overlayWhyNot = (stream: VizStream): string | null => {
    const key = stream.depth?.pair_camera ?? null;
    const cam = key ? model.cameras.find((x) => x.key === key) : undefined;
    const e = key ? ep.cameras.find((x) => x.key === key) : undefined;
    if (!cam || !e || e.access === 'unsupported') return zh.viz.depth.noPair;
    return overlayFits(stream.depth ?? { width: null, height: null }, cam) ? null : zh.viz.depth.aspect;
  };
  const rowH = cellHeight(gridW || 1200, max !== null ? 1 : shape.cols);
  const narrow = max === null && cellWidth(gridW || 1200, shape.cols) < NARROW_CELL_PX;
  const overflow = template !== 'custom' ? (layout?.overflow ?? 0) : 0;
  const focusedCell = focus !== null && focus < cells.length ? cells[focus] : null;
  const focusedStream = focusedCell?.kind === 'curve' ? focusedCell.key : null;

  // -- the dataset's default layout (design doc 21 §6.4): saved for everyone, or taken away
  const canSave = full && source.scope === 'dataset';
  const saveLayout = () =>
    Modal.confirm({
      title: zh.viz.display.saveTitle,
      content: zh.viz.display.saveContent,
      okText: zh.viz.display.save,
      cancelText: zh.common.cancel,
      onOk: async () => {
        try {
          const doc = await fetchVizDisplay(qc, source.id);
          const s = clock.getSnapshot();
          await saveVizDisplay(qc, source.id, withLayout(doc.config, { template, shape, cells, hidden, track: trackKey, speed: s.speed, loop: s.loop }));
          Message.success(zh.viz.display.saved);
        } catch (e) {
          Message.error(errorMessage(e));
          throw e;
        }
      },
    });
  const restoreLayout = () =>
    Modal.confirm({
      title: zh.viz.display.restoreTitle,
      content: zh.viz.display.restoreContent,
      okText: zh.viz.display.restore,
      cancelText: zh.common.cancel,
      onOk: async () => {
        try {
          const doc = await fetchVizDisplay(qc, source.id);
          await saveVizDisplay(qc, source.id, withoutLayout(doc.config));
        } catch (e) {
          Message.error(errorMessage(e));
          throw e;
        }
        setMax(null);
        setLayout(null);
        setTemplate('smart');
        setHidden({});
        setTrackKey(null);
        clock.setSpeed(1);
        clock.setLoop(false);
        Message.success(zh.viz.display.restored);
      },
    });

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
      <Head model={model} ep={ep} clock={clock} loadingNext={loadingNext} full={full} lead={lead}>
        {full ? (
          <>
            <Select size="small" style={{ width: 132 }} value={template} onChange={(v: LayoutTemplate) => setTemplate(v)} aria-label={zh.viz.layoutTitle}>
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
              {gridSizes(shape).map((g) => (
                <Select.Option key={gridKey(g)} value={gridKey(g)}>
                  {zh.viz.grid(g.cols, g.rows)}
                </Select.Option>
              ))}
            </Select>
          </>
        ) : null}
        {canSave ? (
          <Dropdown
            trigger="click"
            position="br"
            droplist={
              <Menu onClickMenuItem={(k) => (k === 'save' ? saveLayout() : restoreLayout())}>
                <Menu.Item key="save">{zh.viz.display.save}</Menu.Item>
                <Menu.Item key="restore" disabled={!hasLayout(model.display)}>
                  {zh.viz.display.restore}
                </Menu.Item>
              </Menu>
            }
          >
            <Button size="small" title={zh.viz.display.menuTitle} icon={<IconLayout />} data-testid="vz-layout-menu">
              {zh.viz.display.menu}
            </Button>
          </Dropdown>
        ) : null}
        <Button size="small" className={sideOpen ? 'on' : ''} title={zh.viz.infoTitle} icon={<IconInfoCircle />} onClick={() => setSideOpen((v) => !v)}>
          {zh.viz.info}
        </Button>
      </Head>
      {!full && evidence.length ? (
        <div className="vz-evlist">
          {evidence.map((ev) => (
            <button
              key={ev.id}
              type="button"
              className={`chip ${ev.level}${ev.id === focusEvidence ? ' on' : ''}`}
              title={ev.start === null ? (ev.whole ? zh.viz.mini.wholeTitle : zh.viz.mini.unlocatableTitle) : ev.label}
              onClick={() => focusEv(ev.id)}
              data-testid="vz-chip"
            >
              <i className="dot" />
              <code>{ev.item}</code>
              <span className="lab">{ev.label}</span>
              {ev.start === null ? <span className="muted">{ev.whole ? zh.viz.mini.whole : zh.viz.mini.unlocatable}</span> : null}
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
            className={`vz-grid${narrow ? ' is-narrow' : ''}`}
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
                    {c.kind === 'depth' ? (
                      <button
                        type="button"
                        title={zh.viz.depth.settingsTitle}
                        onClick={(e) => {
                          e.stopPropagation();
                          setFocus(i);
                          if (settings?.i === i) {
                            setSettings(null);
                            return;
                          }
                          const w = wrap.current?.getBoundingClientRect();
                          const r = (e.currentTarget.closest('.vz-cell') as HTMLElement).getBoundingClientRect();
                          if (w) setSettings({ i, left: Math.max(0, Math.min(r.right - w.left - 268, w.width - 272)), top: r.top - w.top + 36 });
                        }}
                      >
                        <IconSettings />
                        <span className="t">{zh.viz.depth.settings}</span>
                      </button>
                    ) : null}
                    <button
                      type="button"
                      title={zh.viz.cell.swapTitle}
                      onClick={(e) => {
                        e.stopPropagation();
                        openMenu(i, e.currentTarget.closest('.vz-cell') as HTMLElement);
                      }}
                    >
                      <IconSwap />
                      <span className="t">{zh.viz.cell.swap}</span>
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
                      <span className="t">{max === i ? zh.viz.cell.restore : zh.viz.cell.expand}</span>
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
                const transcoded = cam.transcoded || e.transcoded || (e.access !== 'frames' && !!e.transcode_url && !clientKeys.has(cam.key) && !canDecode(cam.codec_string));
                return (
                  <div key={i} className={cls} onPointerDown={() => setFocus(i)} data-testid={`vz-cell-${i}`}>
                    {cameraView(cam, e)}
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
              if (c.kind === 'depth') {
                const stream = model.streams.find((x) => x.key === c.key && x.kind === 'depth');
                const e = (ep.streams ?? []).find((x) => x.key === c.key);
                if (!stream || !e) return <div key={i} className={cls} />;
                const view = c.view ?? DEFAULT_DEPTH_VIEW;
                const whyNot = overlayWhyNot(stream);
                const overlay = view.overlay && !whyNot;
                const pairKey = stream.depth?.pair_camera ?? null;
                const pairCam = pairKey ? model.cameras.find((x) => x.key === pairKey) : undefined;
                const pairEp = pairKey ? ep.cameras.find((x) => x.key === pairKey) : undefined;
                return (
                  <div key={i} className={cls} onPointerDown={() => setFocus(i)} data-testid={`vz-cell-${i}`}>
                    <DepthCell stream={stream} ep={e} clock={clock} view={view} overlay={overlay}>
                      {overlay && pairCam && pairEp ? cameraView(pairCam, pairEp) : null}
                    </DepthCell>
                    <span className="vz-cap" title={stream.name}>
                      <i className="dot" style={{ color: pairKey ? cameraColor(pairKey) : 'var(--c-text-3)' }} />
                      {stream.name}
                      <span className="vz-dp">{zh.viz.depth.tag}</span>
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
          {overflow > 0 ? (
            <div className="vz-overflow" data-testid="vz-overflow">
              {zh.viz.overflow(overflow)}
            </div>
          ) : null}
          {settings && cells[settings.i]?.kind === 'depth'
            ? (() => {
                const c = cells[settings.i] as Extract<CellContent, { kind: 'depth' }>;
                const stream = model.streams.find((x) => x.key === c.key && x.kind === 'depth');
                const e = (ep.streams ?? []).find((x) => x.key === c.key);
                if (!stream || !e) return null;
                return (
                  <DepthSettings
                    at={{ left: settings.left, top: settings.top }}
                    view={c.view ?? DEFAULT_DEPTH_VIEW}
                    indexUrl={e.url ? e.index_url : null}
                    overlayWhyNot={overlayWhyNot(stream)}
                    onChange={(v) => {
                      const next = [...cells];
                      next[settings.i] = { kind: 'depth', key: c.key, view: v };
                      setCustom(next);
                    }}
                    onClose={() => setSettings(null)}
                  />
                );
              })()
            : null}
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
            clientDecoded={clientKeys}
          />
        ) : null}
      </div>
      <Transport
        clock={clock}
        tl={tl}
        onPrevEpisode={full ? onPrevEpisode : undefined}
        onNextEpisode={full ? onNextEpisode : undefined}
        playBlocked={playBlocked}
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

/** "LeRobot v3", "mcap", "Lance（lerobot-lancedb 0.3）" ... */
export function formatLabel(f: VizDataset['format']): string {
  if (f.kind === 'lerobot') return `LeRobot ${f.version ?? ''}`.trim();
  if (f.kind === 'lance') return f.layout ? zh.viz.lanceLayout(f.layout) : 'Lance';
  return f.kind;
}

function Head({
  model,
  ep,
  clock,
  loadingNext,
  full,
  lead,
  children,
}: {
  model: VizDataset;
  ep: VizEpisode;
  clock: PlayerClock;
  loadingNext: boolean;
  full: boolean;
  lead?: React.ReactNode;
  children: React.ReactNode;
}) {
  const waiting = useClockValue(clock, (s) => s.playing && s.waiting);
  const fps = ep.fps ?? ep.timeline.fps ?? model.fps;
  return (
    <div className="vz-head">
      {lead ? <span className="lead">{lead}</span> : null}
      <span className="ep" title={model.name}>
        {model.name}
        {/* the mini player's title bar already says which episode (2026-10-04) */}
        {full ? <small>{`ep ${ep.index}`}</small> : null}
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
  clientDecoded: ReadonlySet<string>;
}) {
  const stream = focused?.kind === 'curve' ? focused.key : '';
  const series = useVizSeries(source, ep.index, stream, points, !!stream);
  return <SidePanel {...rest} focused={focused} model={model} ep={ep} series={stream ? series.data : undefined} />;
}
