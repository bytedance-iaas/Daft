import { Button, Modal, Space } from '@arco-design/web-react';
import { useMemo } from 'react';
import type { EpisodeView } from '../../api/types';
import { getBase } from '../../base';
import { cameraOfScope, EEF_MODULE, miniLayout, type FindingScope } from '../../lib/vizLayout';
import { evidenceRange, frameCount } from '../../lib/vizTime';
import { zh } from '../../locales/zh';
import { noEefOverlay, useEefOverlay, useVizEpisode, useVizModel, type VizRef } from './data';
import type { MiniSeek } from './miniPlayer';
import { Player, type Evidence, type PlayerOverlays } from './Player';

/**
 * The mini player (design doc 18 §4.6, F13.6): one episode of a task, read from the task's frozen
 * input, with the episode's findings as chips and bands - placed with the checks' clock of the
 * task-level episode answer (an mcap's first action message and action rate), or the episode's own
 * frames. A finding that cannot be placed is listed as such. The cells follow the focused finding's
 * module; 「在可视化页打开」 opens the full page while the task's registration exists.
 *
 * A task that ran the EEF module gets its marks drawn over the cameras (design doc 22 §3.3): asked once
 * the episode is on screen, nothing when the task has none, one line when they cannot be read. An EEF
 * finding lays out every camera with marks; `seek` opens on a sample frame the EEF opinion cites.
 */
export function MiniPlayerModal({
  taskId,
  view,
  focus,
  onFocus,
  onClose,
  seek = null,
}: {
  taskId: string;
  /** the episode: its findings when a revision has them (a running task's pipeline has none yet) */
  view: Pick<EpisodeView, 'episode_index' | 'findings' | 'dataset_id'>;
  /** index into view.findings, or null */
  focus: number | null;
  onFocus: (i: number) => void;
  onClose: () => void;
  /** open on this sample frame of a camera of the EEF bundle, paused there */
  seek?: MiniSeek | null;
}) {
  const source: VizRef = useMemo(() => ({ scope: 'task', id: taskId }), [taskId]);
  const model = useVizModel(source);
  const ep = useVizEpisode(source, view.episode_index);
  const overlay = useEefOverlay(taskId, view.episode_index, !!ep.data);
  const findings = useMemo(() => view.findings ?? [], [view.findings]);
  const evidence: Evidence[] = useMemo(
    () =>
      findings.map((f, i) => {
        const at = ep.data ? evidenceRange(f.finding, ep.data.timeline, frameCount(ep.data), ep.data.check_clock) : null;
        const whole = !f.finding.frames && !f.finding.time_s;
        return { id: String(i), level: f.level, item: f.finding.item ?? f.finding.code, label: f.finding.message_zh, start: at?.[0] ?? null, end: at?.[1] ?? null, whole };
      }),
    [findings, ep.data],
  );
  // the bundle's cameras by their own ids -> the player's cameras (findings of the EEF module name the former)
  const alias = useMemo(() => Object.fromEntries((overlay.data?.cameras ?? []).flatMap((c) => (c.viz_camera ? [[c.camera_id, c.viz_camera]] : []))) as Record<string, string>, [overlay.data]);
  const focused = focus !== null ? findings[focus] : undefined;
  const scope = useMemo((): FindingScope | null => {
    if (seek) return { camera: alias[seek.camera] ?? seek.camera };
    const s = (focused?.finding.scope ?? null) as FindingScope | null;
    return s ? { ...s, camera: s.camera ? (alias[s.camera] ?? s.camera) : undefined, cameras: s.cameras?.map((c) => alias[c] ?? c) } : null;
  }, [seek, focused, alias]);
  const module = seek ? EEF_MODULE : (focused?.module ?? null);
  const overlays: PlayerOverlays | null = useMemo(() => {
    const cams = (overlay.data?.cameras ?? []).filter((c) => c.viz_camera && !c.skipped && c.layers.length);
    if (!cams.length) return null;
    const at = module === EEF_MODULE && model.data ? cameraOfScope(scope, model.data.cameras) : null;
    return { cameras: Object.fromEntries(cams.map((c) => [c.viz_camera as string, c])), focus: at };
  }, [overlay.data, module, scope, model.data]);
  const overlaid = useMemo(() => (overlays ? Object.keys(overlays.cameras) : []), [overlays]);
  const arrangement = useMemo(() => (model.data ? miniLayout(module, scope, model.data, overlaid) : null), [model.data, module, scope, overlaid]);
  const seekAt = seek ? (overlay.data?.cameras.find((c) => c.camera_id === seek.camera)?.times_s[seek.frame] ?? null) : null;
  const startAt = seek ? seekAt : focus !== null ? (evidence[focus]?.start ?? null) : null;
  const full = view.dataset_id ? `${getBase()}/visualize?dataset=${encodeURIComponent(view.dataset_id)}&ep=${view.episode_index}` : null;
  return (
    <Modal
      visible
      title={zh.viz.mini.title(view.episode_index)}
      onCancel={onClose}
      style={{ width: 'min(1240px, 94vw)' }}
      unmountOnExit
      footer={
        <Space>
          {full ? (
            <Button href={full} title={zh.viz.mini.openFullTitle} anchorProps={{ target: '_blank', rel: 'noopener noreferrer' }}>
              {zh.viz.mini.openFull}
            </Button>
          ) : null}
          <Button type="primary" onClick={onClose}>
            {zh.common.close}
          </Button>
        </Space>
      }
      data-testid="vz-mini"
    >
      {overlay.error && !noEefOverlay(overlay.error) ? (
        <div className="vz-warn" role="alert" data-testid="vz-overlay-failed">
          {zh.viz.overlay.failed}
        </div>
      ) : null}
      <Player
        source={source}
        index={view.episode_index}
        mode="mini"
        evidence={evidence}
        focusEvidence={focus !== null ? String(focus) : null}
        onFocusEvidence={(id) => onFocus(Number(id))}
        arrangement={arrangement}
        startAt={startAt}
        overlays={overlays}
      />
    </Modal>
  );
}
