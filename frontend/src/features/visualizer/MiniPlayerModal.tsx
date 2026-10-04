import { Button, Modal, Space } from '@arco-design/web-react';
import { useMemo } from 'react';
import type { EpisodeView } from '../../api/types';
import { getBase } from '../../base';
import { miniLayout } from '../../lib/vizLayout';
import { evidenceRange, frameCount } from '../../lib/vizTime';
import { zh } from '../../locales/zh';
import { useVizEpisode, useVizModel, type VizRef } from './data';
import { Player, type Evidence } from './Player';

/**
 * The mini player (design doc 18 §4.6, F13.6): one episode of a task, read from the task's frozen
 * input, with the episode's findings as chips and bands - placed with the checks' clock of the
 * task-level episode answer (an mcap's first action message and action rate), or the episode's own
 * frames. A finding that cannot be placed is listed as such. The cells follow the focused finding's
 * module; 「在可视化页打开」 opens the full page while the task's registration exists.
 */
export function MiniPlayerModal({
  taskId,
  view,
  focus,
  onFocus,
  onClose,
}: {
  taskId: string;
  /** the episode: its findings when a revision has them (a running task's pipeline has none yet) */
  view: Pick<EpisodeView, 'episode_index' | 'findings' | 'dataset_id'>;
  /** index into view.findings, or null */
  focus: number | null;
  onFocus: (i: number) => void;
  onClose: () => void;
}) {
  const source: VizRef = useMemo(() => ({ scope: 'task', id: taskId }), [taskId]);
  const model = useVizModel(source);
  const ep = useVizEpisode(source, view.episode_index);
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
  const focused = focus !== null ? findings[focus] : undefined;
  const arrangement = useMemo(
    () => (model.data ? miniLayout(focused?.module ?? null, focused?.finding.scope ?? null, model.data) : null),
    [model.data, focused],
  );
  const startAt = focus !== null ? (evidence[focus]?.start ?? null) : null;
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
      <p className="muted" style={{ margin: '0 0 8px', fontSize: 12 }}>
        {zh.viz.mini.hint}
      </p>
      <Player
        source={source}
        index={view.episode_index}
        mode="mini"
        evidence={evidence}
        focusEvidence={focus !== null ? String(focus) : null}
        onFocusEvidence={(id) => onFocus(Number(id))}
        arrangement={arrangement}
        startAt={startAt}
      />
    </Modal>
  );
}
