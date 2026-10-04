import { Button, Select, Switch } from '@arco-design/web-react';
import { IconPause, IconPlayArrowFill, IconSkipNextFill, IconSkipPreviousFill } from '@arco-design/web-react/icon';
import { useRef, useState } from 'react';
import type { VizEvent, VizTimeline, VizTrack } from '../../api/types';
import { clamp, fmtClock, frameAt, segmentAt, stepFrom, timeOfFrame } from '../../lib/vizTime';
import { zh } from '../../locales/zh';
import type { PlayerClock } from './clock';
import { useClockValue } from './useClock';

/** A finding placed on the timeline (mini player). */
export interface Evidence {
  id: string;
  level: 'blocking' | 'review' | 'info';
  /** taxonomy item / code shown on the chip */
  item: string;
  label: string;
  /** episode seconds; null when the finding has no place on the timeline */
  start: number | null;
  end: number | null;
  /** about the whole episode (no frames, no time): listed, never drawn */
  whole?: boolean;
}

export interface TimelineInfo {
  timeline: VizTimeline;
  frames: number;
  duration: number;
}

const SPEEDS = [1, 1.5, 2];
const ROW = 7;

function TimeLabel({ clock, duration }: { clock: PlayerClock; duration: number }) {
  const t = useClockValue(clock, (s) => Math.round(s.t * 10) / 10);
  return <span className="time">{`${fmtClock(t)} / ${fmtClock(duration)}`}</span>;
}

function FrameInput({ clock, tl }: { clock: PlayerClock; tl: TimelineInfo }) {
  const frame = useClockValue(clock, (s) => frameAt(tl.timeline, tl.frames, s.t));
  const [draft, setDraft] = useState<string | null>(null);
  const go = () => {
    if (draft !== null) {
      const f = Number(draft);
      if (Number.isFinite(f)) {
        clock.pause();
        clock.seek(timeOfFrame(tl.timeline, tl.frames, clamp(Math.round(f), 0, tl.frames - 1)));
      }
    }
    setDraft(null);
  };
  return (
    <span className="frame">
      {zh.viz.frame}
      <input
        aria-label={zh.viz.frameInput}
        title={zh.viz.frameInput}
        value={draft ?? String(frame)}
        onChange={(e) => setDraft(e.target.value.replace(/[^\d]/g, ''))}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            go();
            (e.target as HTMLInputElement).blur();
          }
          if (e.key === 'Escape') setDraft(null);
        }}
        onBlur={go}
      />
      /<span>{Math.max(0, tl.frames - 1)}</span>
    </span>
  );
}

/**
 * The progress bar (design doc 18 §5.4): the selected annotation track's segments above the track
 * (other tracks stacked over it), the findings and events below; click or drag to seek, hover for
 * time, frame and step; a segment click goes to its start, a finding click focuses it.
 */
export function Progress({
  clock,
  tl,
  track,
  others,
  evidence,
  events,
  focusEvidence,
  onFocusEvidence,
}: {
  clock: PlayerClock;
  tl: TimelineInfo;
  track: VizTrack | null;
  others: readonly VizTrack[];
  evidence: readonly Evidence[];
  events: readonly VizEvent[];
  focusEvidence: string | null;
  onFocusEvidence?: (id: string) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const drag = useRef(false);
  const [tip, setTip] = useState<{ left: number; text: string } | null>(null);
  const pct = useClockValue(clock, (s) => (tl.duration > 0 ? Math.round((s.t / tl.duration) * 10000) / 100 : 0));
  const extra = others.length;
  const off = extra * ROW;
  const dur = tl.duration > 0 ? tl.duration : 1;
  const at = (clientX: number) => {
    const r = box.current?.getBoundingClientRect();
    if (!r || r.width <= 0) return 0;
    return clamp((clientX - r.left) / r.width, 0, 1) * tl.duration;
  };
  const left = (s: number) => `${clamp((s / dur) * 100, 0, 100)}%`;
  const width = (a: number, b: number) => `${Math.max(0.4, clamp(((b - a) / dur) * 100, 0, 100))}%`;
  return (
    <div
      ref={box}
      className="vz-prog"
      style={{ height: 34 + off }}
      onPointerDown={(e) => {
        drag.current = true;
        e.currentTarget.setPointerCapture?.(e.pointerId);
        clock.seek(at(e.clientX));
      }}
      onPointerMove={(e) => {
        const t = at(e.clientX);
        const segs = track?.segments ?? [];
        const k = segmentAt(segs, t);
        const r = box.current?.getBoundingClientRect();
        setTip({ left: r && r.width > 0 ? ((e.clientX - r.left) / r.width) * 100 : 0, text: zh.viz.tip(fmtClock(t), frameAt(tl.timeline, tl.frames, t), k >= 0 ? segs[k].label : null) });
        if (drag.current) clock.seek(t);
      }}
      onPointerUp={() => {
        drag.current = false;
      }}
      onPointerLeave={() => setTip(null)}
      data-testid="vz-progress"
    >
      {others.map((o, i) => (
        <div key={o.key} className="steps" style={{ top: 4 + i * ROW, height: 4 }} title={o.name}>
          {o.segments.map((s, j) => (
            <span key={j} style={{ left: left(s.start_s), width: width(s.start_s, s.end_s), height: 4 }} title={zh.viz.stepTitle(`${o.name} · ${s.label}`, s.start_s, s.end_s, s.quality === 'unqualified')} />
          ))}
        </div>
      ))}
      <div className="steps" style={{ top: off + 4 }}>
        {(track?.segments ?? []).map((s, j) => (
          <span
            key={j}
            className={s.quality === 'unqualified' ? 'unq' : ''}
            style={{ left: left(s.start_s), width: width(s.start_s, s.end_s) }}
            title={zh.viz.stepTitle(s.label, s.start_s, s.end_s, s.quality === 'unqualified')}
            onPointerDown={(e) => {
              e.stopPropagation();
              clock.seek(s.start_s);
            }}
          />
        ))}
      </div>
      <div className="track" style={{ top: off + 14 }}>
        <i className="fill" style={{ width: `${pct}%` }} />
      </div>
      <i className="knob" style={{ left: `${pct}%`, top: off + 11 }} />
      <div className="ev" style={{ top: off + 24 }}>
        {evidence.map((ev) => {
          if (ev.start === null || ev.end === null) return null;
          const w = ((ev.end - ev.start) / dur) * 100;
          return (
            <span
              key={ev.id}
              className={`${ev.level}${ev.id === focusEvidence ? ' focus' : ''}${w < 0.4 ? ' pt' : ''}`}
              style={{ left: left(ev.start), width: width(ev.start, ev.end) }}
              title={`${ev.label} · ${fmtClock(ev.start)}–${fmtClock(ev.end as number)}`}
              onPointerDown={(e) => {
                e.stopPropagation();
                onFocusEvidence?.(ev.id);
              }}
            />
          );
        })}
        {events.map((ev, i) => (
          <span key={`e${i}`} className="event" style={{ left: left(ev.t_s) }} title={`${ev.label} · ${fmtClock(ev.t_s)}`} />
        ))}
      </div>
      {tip ? (
        <span className="tip" style={{ left: `${tip.left}%`, display: 'block', top: off - 26 }}>
          {tip.text}
        </span>
      ) : null}
    </div>
  );
}

export function Transport({
  clock,
  tl,
  progress,
  onPrevEpisode,
  onNextEpisode,
}: {
  clock: PlayerClock;
  tl: TimelineInfo;
  progress: React.ReactNode;
  onPrevEpisode?: () => void;
  onNextEpisode?: () => void;
}) {
  const playing = useClockValue(clock, (s) => s.playing);
  const speed = useClockValue(clock, (s) => s.speed);
  const loop = useClockValue(clock, (s) => s.loop);
  const step = (n: number) => {
    clock.pause();
    clock.seek(stepFrom(tl.timeline, tl.frames, clock.getSnapshot().t, n));
  };
  return (
    <div className="vz-transport">
      <button type="button" className="tb" title={zh.viz.prevFrame} aria-label={zh.viz.prevFrame} onClick={() => step(-1)}>
        <IconSkipPreviousFill />
      </button>
      <button type="button" className="tb play" title={zh.viz.play} aria-label={zh.viz.play} onClick={() => clock.toggle()} data-testid="vz-play">
        {playing ? <IconPause /> : <IconPlayArrowFill />}
      </button>
      <button type="button" className="tb" title={zh.viz.nextFrame} aria-label={zh.viz.nextFrame} onClick={() => step(1)}>
        <IconSkipNextFill />
      </button>
      <Select size="small" style={{ width: 72 }} value={speed} onChange={(v: number) => clock.setSpeed(v)} aria-label={zh.viz.speed}>
        {SPEEDS.map((v) => (
          <Select.Option key={v} value={v}>{`${v}x`}</Select.Option>
        ))}
      </Select>
      {progress}
      <TimeLabel clock={clock} duration={tl.duration} />
      <FrameInput clock={clock} tl={tl} />
      <label className="loop" title={zh.viz.loopTitle}>
        <Switch size="small" checked={loop} onChange={(v) => clock.setLoop(v)} />
        {zh.viz.loop}
      </label>
      {onPrevEpisode || onNextEpisode ? (
        <span className="epnav">
          <Button size="small" title={zh.viz.prevEpisodeTitle} disabled={!onPrevEpisode} onClick={onPrevEpisode}>
            {zh.viz.prevEpisode}
          </Button>
          <Button size="small" title={zh.viz.nextEpisodeTitle} disabled={!onNextEpisode} onClick={onNextEpisode}>
            {zh.viz.nextEpisode}
          </Button>
        </span>
      ) : null}
      <span className="vz-keys" title={zh.viz.keys}>
        <kbd>␣</kbd> <kbd>←</kbd>
        <kbd>→</kbd>
      </span>
    </div>
  );
}
