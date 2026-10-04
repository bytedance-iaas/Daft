import { describe, expect, it } from 'vitest';
import type { VizTimeline } from '../api/types';
import {
  bisectRight,
  episodeTimeOfMedia,
  evidenceRange,
  fmtClock,
  frameAt,
  frameCount,
  frameStep,
  mediaTime,
  segmentAt,
  stepFrom,
  timeOfFrame,
} from './vizTime';

const FPS30: VizTimeline = { kind: 'frame', fps: 30, frame_reference: null, frame_times: null };
// an mcap timeline: the action topic numbers the frames, unevenly
const TS: VizTimeline = { kind: 'timestamp', fps: null, frame_reference: '/action', frame_times: [0, 0.1, 0.2, 0.35, 0.4] };

describe('vizTime', () => {
  it('bisects to the last element at or before x', () => {
    expect(bisectRight([0, 1, 2, 2, 3], 2)).toBe(3);
    expect(bisectRight([0, 1, 2], -1)).toBe(-1);
    expect(bisectRight([0, 1, 2], 9)).toBe(2);
    expect(bisectRight([], 1)).toBe(-1);
  });

  it('formats mm:ss.s and carries 59.96 s into the next minute', () => {
    expect(fmtClock(0)).toBe('00:00.0');
    expect(fmtClock(65.24)).toBe('01:05.2');
    expect(fmtClock(59.96)).toBe('01:00.0');
    expect(fmtClock(-3)).toBe('00:00.0');
    expect(fmtClock(Number.NaN)).toBe('00:00.0');
  });

  it('converts between time and frames on an fps timeline', () => {
    expect(frameAt(FPS30, 90, 1.0)).toBe(30);
    expect(frameAt(FPS30, 90, 1.016)).toBe(30);           // nearest frame
    expect(frameAt(FPS30, 90, 1.02)).toBe(31);
    expect(frameAt(FPS30, 90, 99)).toBe(89);               // clamped to the last frame
    expect(timeOfFrame(FPS30, 90, 45)).toBe(1.5);
    expect(frameStep(FPS30, 90, 0)).toBeCloseTo(1 / 30);
    expect(stepFrom(FPS30, 90, 1.0, 1)).toBeCloseTo(31 / 30);
    expect(stepFrom(FPS30, 90, 0, -1)).toBe(0);
  });

  it('numbers frames by the reference topic on a timestamp timeline (mcap)', () => {
    const ep = { frames: 5, timeline: TS };
    expect(frameCount(ep)).toBe(5);
    expect(frameAt(TS, 5, 0.3)).toBe(2);                   // the last frame at or before t
    expect(frameAt(TS, 5, 0.35)).toBe(3);
    expect(frameAt(TS, 5, -1)).toBe(0);
    expect(timeOfFrame(TS, 5, 3)).toBe(0.35);
    expect(frameStep(TS, 5, 0.36)).toBeCloseTo(0.05);
    expect(stepFrom(TS, 5, 0.3, 1)).toBe(0.35);
  });

  it('places a camera by its offset and its window in a v3 file', () => {
    const cam = { offset_s: 0.05, from_ts: 12.0 };
    expect(mediaTime(cam, 1.0)).toBeCloseTo(12.95);
    expect(episodeTimeOfMedia(cam, 12.95)).toBeCloseTo(1.0);
    expect(mediaTime({ offset_s: 0, from_ts: null }, 2)).toBe(2);
  });

  it('finds the segment under t and reports gaps', () => {
    const segs = [
      { start_s: 0, end_s: 1 },
      { start_s: 1, end_s: 2.5 },
      { start_s: 3, end_s: 4 },
    ];
    expect(segmentAt(segs, 0.5)).toBe(0);
    expect(segmentAt(segs, 1)).toBe(1);
    expect(segmentAt(segs, 2.7)).toBe(-1);
    expect(segmentAt(segs, 3.99)).toBe(2);
    expect(segmentAt(segs, 4)).toBe(-1);
    expect(segmentAt([], 1)).toBe(-1);
  });

  it('puts findings on the episode clock through the checks clock', () => {
    // mcap: the checks start at the first action message, 0.05 s after the episode's zero, at 10 Hz
    const clock = { offset_s: 0.05, fps: 10 };
    expect(evidenceRange({ frames: [3, 5] }, TS, 5, clock)).toEqual([0.35, 0.65]);
    expect(evidenceRange({ frames: [3] }, TS, 5, clock)).toEqual([0.35, 0.45]);
    expect(evidenceRange({ time_s: [1, 2] }, TS, 5, clock)).toEqual([1.05, 2.05]);
    expect(evidenceRange({ time_s: [1, null] }, TS, 5, clock)).toEqual([1.05, 1.05]);
    // no checks clock (dataset scope): the episode's own frames
    expect(evidenceRange({ frames: [30, 59] }, FPS30, 90, null)).toEqual([1, 2]);
    expect(evidenceRange({ frames: [null, 4] }, FPS30, 90, null)).toBeNull();
    expect(evidenceRange({}, FPS30, 90, null)).toBeNull();
  });
});
