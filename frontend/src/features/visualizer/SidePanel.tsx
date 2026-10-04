import { Button, Select } from '@arco-design/web-react';
import type { VizCamera, VizEpisode, VizEpisodeCamera, VizSeries, VizStream } from '../../api/types';
import { fmtNum, legendEntries, valueAt } from '../../lib/vizCurves';
import type { CellContent } from '../../lib/vizLayout';
import { fmtClock, frameAt } from '../../lib/vizTime';
import { zh } from '../../locales/zh';
import type { PlayerClock } from './clock';
import { useClockValue } from './useClock';

function Kv({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </div>
  );
}

function Now({ clock, ep }: { clock: PlayerClock; ep: VizEpisode }) {
  const t = useClockValue(clock, (s) => Math.round(s.t * 1000) / 1000);
  return (
    <>
      <Kv k={zh.viz.side.frame} v={frameAt(ep.timeline, ep.frames, t) + 1} />
      <Kv k={zh.viz.side.time} v={fmtClock(t)} />
    </>
  );
}

function SeriesValue({ clock, series, i }: { clock: PlayerClock; series: VizSeries; i: { main: number; action: number } }) {
  const t = useClockValue(clock, (s) => Math.round(s.t * 100) / 100);
  const v = (k: number) => (k >= 0 ? fmtNum(valueAt(series.t, series.lines[k].values, t)) : null);
  const main = v(i.main);
  const action = v(i.action);
  return <span className="val">{[main, action].filter((x) => x !== null).join(' / ')}</span>;
}

/**
 * The info panel (design doc 18 §5.6): it follows the focused cell; with several annotation tracks
 * it also chooses the one the subtitle shows.
 */
export function SidePanel({
  focused,
  model,
  ep,
  clock,
  series,
  hidden,
  onToggle,
  trackKey,
  onTrack,
  onClose,
  clientDecoded,
}: {
  focused: CellContent | null;
  model: { cameras: VizCamera[]; streams: VizStream[] };
  ep: VizEpisode;
  clock: PlayerClock;
  /** the focused curve's loaded series, if any */
  series: VizSeries | undefined;
  hidden: ReadonlySet<string>;
  onToggle: (name: string) => void;
  trackKey: string | null;
  onTrack: (key: string) => void;
  onClose: () => void;
  /** cameras the browser decodes itself (design doc 19 §3) */
  clientDecoded?: ReadonlySet<string>;
}) {
  let title = zh.viz.side.title;
  let body: React.ReactNode;
  if (!focused) body = <div className="empty">{zh.viz.side.none}</div>;
  else if (focused.kind === 'empty') body = <div className="empty">{zh.viz.side.empty}</div>;
  else if (focused.kind === 'video') {
    const cam = model.cameras.find((c) => c.key === focused.key);
    const e: VizEpisodeCamera | undefined = ep.cameras.find((c) => c.key === focused.key);
    if (cam) {
      title = zh.viz.side.camera(cam.name);
      const openable = e?.url && (e.access === 'direct' || e.access === 'local' || e.access === 'remux' || e.access === 'blob');
      body = (
        <>
          <div className="sec">{zh.viz.side.stream}</div>
          <Kv k={zh.viz.side.key} v={<span className="mono">{cam.source}</span>} />
          <Kv k={zh.viz.side.resolution} v={cam.width && cam.height ? `${cam.width} × ${cam.height}` : '—'} />
          <Kv k={zh.viz.side.codec} v={cam.transcoded ? zh.viz.side.codecTranscoded(cam.codec) : [cam.codec, cam.pix_fmt].filter(Boolean).join(' · ') || '—'} />
          <Kv k={zh.viz.side.fps} v={cam.fps ? zh.viz.fps(cam.fps) : '—'} />
          <Kv k={zh.viz.side.frames} v={ep.frames} />
          <Kv k={zh.viz.side.duration} v={fmtClock(ep.duration_s)} />
          <div className="sec">{zh.viz.side.source}</div>
          <Kv k={zh.viz.side.range} v={e && e.from_ts !== null && e.from_ts !== undefined ? zh.viz.side.rangeOf(e.from_ts, e.to_ts ?? null) : zh.viz.side.wholeFile} />
          {e && e.offset_s ? <Kv k={zh.viz.side.offset} v={zh.viz.side.offsetOf(e.offset_s)} /> : null}
          <Kv k={zh.viz.side.access} v={clientDecoded?.has(cam.key) ? zh.viz.access.client : (zh.viz.access[e?.access ?? cam.access] ?? e?.access ?? cam.access)} />
          <div className="sec">{zh.viz.side.current}</div>
          <Now clock={clock} ep={ep} />
          {openable ? (
            <div style={{ marginTop: 14 }}>
              <Button size="small" onClick={() => window.open(e?.url as string, '_blank', 'noopener')}>
                {zh.viz.side.openSource}
              </Button>
            </div>
          ) : null}
        </>
      );
    }
  } else {
    const stream = model.streams.find((s) => s.key === focused.key);
    if (stream) {
      title = zh.viz.side.curve(stream.name);
      const roles = (role: 'state' | 'action' | 'other') => [...new Set(stream.lines.filter((l) => l.role === role).map((l) => l.source ?? ''))].filter(Boolean);
      const legend = series ? legendEntries(series.lines) : legendEntries(stream.lines.map((l) => ({ name: l.name, role: l.role, values: [] })));
      body = (
        <>
          <div className="sec">{zh.viz.side.fields}</div>
          {roles('state').length ? <Kv k={zh.viz.side.state} v={<span className="mono">{roles('state').join('、')}</span>} /> : null}
          {roles('action').length ? <Kv k={zh.viz.side.action} v={<span className="mono">{roles('action').join('、')}</span>} /> : null}
          {roles('other').length ? <Kv k={zh.viz.side.other} v={<span className="mono">{roles('other').join('、')}</span>} /> : null}
          <Kv k={zh.viz.side.unit} v={stream.unit || '—'} />
          <Kv k={zh.viz.side.sampling} v={zh.viz.side.samplingOf(series?.total_points ?? 0, stream.rate_hz)} />
          <div className="sec">{zh.viz.side.series}</div>
          {legend.map((l) => (
            <label key={l.name} className="sl">
              <input type="checkbox" checked={!hidden.has(l.name)} onChange={() => onToggle(l.name)} />
              <span className="sw" style={{ color: l.color }} />
              <span className="nm" title={l.name}>
                {l.name}
              </span>
              {series ? <SeriesValue clock={clock} series={series} i={{ main: l.state >= 0 ? l.state : l.other, action: l.action }} /> : null}
            </label>
          ))}
          <div className="sec">{zh.viz.side.note}</div>
          <div className="note">{zh.viz.side.curveNote}</div>
        </>
      );
    }
  }
  const tracks = ep.annotations.tracks;
  return (
    <aside className="vz-side" data-testid="vz-side">
      <div className="vz-side-head">
        <span>{title}</span>
        <span className="spacer" />
        <button type="button" className="x" title={zh.viz.side.close} aria-label={zh.viz.side.close} onClick={onClose}>
          ×
        </button>
      </div>
      <div className="vz-side-body">
        {body}
        {tracks.length > 1 || ep.annotations.labels.length ? <div className="sec">{zh.viz.side.annotations}</div> : null}
        {tracks.length > 1 ? (
          <Kv
            k={zh.viz.side.track}
            v={
              <Select size="mini" value={trackKey ?? undefined} onChange={(v: string) => onTrack(v)} style={{ width: '100%' }}>
                {tracks.map((t) => (
                  <Select.Option key={t.key} value={t.key}>
                    {t.name}
                  </Select.Option>
                ))}
              </Select>
            }
          />
        ) : null}
        {ep.annotations.labels.map((l) => (
          <Kv key={l.key} k={l.name} v={l.value} />
        ))}
      </div>
    </aside>
  );
}
