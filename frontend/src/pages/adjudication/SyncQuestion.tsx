// 同步存疑 (registry 8.1, 2026-10-13): the sync check found the cameras out of step with each other - one camera
// off against the arm's motion while the others are not, or cameras whose lags differ. The module cannot say
// which camera is right, so a person looks: why it was asked, each camera's readings (whole-episode and window
// by window: the curves); the card's own 可视化 button plays the cameras side by side (a compare button and a
// correction toggle were tried and dropped on 2026-10-13: the review's job is to see the problem as recorded).
// 可用 keeps the episode, 确有错位 rejects it.
import { LazyVisible } from '../../components/LazyVisible';
import { SyncCurvesPanel } from '../report/episodeBlocks';

/** The curves of the sync review: the picture and arm motion per camera, the similarity curve, the windowed lags.
 * (A readings table was tried and dropped on 2026-10-13: the finding text carries the numbers the rule used.) */
export function SyncEvidence({ taskId, ep, rev }: { taskId: string; ep: number; rev: number }) {
  return (
    <div data-testid={`sync-evidence-${ep}`}>
      <LazyVisible placeholder={<div style={{ height: 80 }} />}>
        <SyncCurvesPanel taskId={taskId} rev={rev} ep={ep} />
      </LazyVisible>
    </div>
  );
}
