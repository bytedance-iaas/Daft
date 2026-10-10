import { Button, Space } from '@arco-design/web-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { PageHeader } from '../../components/PageHeader';
import { DisplayConfigDrawer } from '../../features/datasets/DisplayConfigDrawer';
import { useDatasetEefOverlay, useVizModel, type VizRef } from '../../features/visualizer/data';
import { Player, type PlayerControl, type PlayerOverlays } from '../../features/visualizer/Player';
import { parseChoice, type OverlayChoice } from '../../lib/eefOverlay';
import { readPrefs, writePrefs } from '../../lib/prefs';
import { firstEpisode, hasEpisode } from '../../lib/vizTime';
import { zh } from '../../locales/zh';
import { DatasetInfo } from './DatasetInfo';
import { EpisodeRail, RailButton, useVizDatasets } from './EpisodeRail';
import './visualizePage.css';

/** Development builds put the player's clock on window.__vizClock (the drift check, F13.4). */
const DEV_CLOCK = import.meta.env.DEV
  ? (clock: unknown) => {
      (window as unknown as { __vizClock?: unknown }).__vizClock = clock;
    }
  : undefined;


/**
 * 「可视化」 (design doc 18 §5.0, D63): `/visualize?dataset=<id>&ep=<n>`. The left rail picks the
 * dataset and the episode; the player and the dataset's info tree fill the rest. Without a dataset
 * in the address, the one shown last. The rail folds from its own top right and then takes no room;
 * 展开侧栏 sits before the player's title, or at the top left of whatever the page shows instead.
 */
export function VisualizePage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const datasetId = params.get('dataset') || readPrefs().lastVizDataset || null;
  const epParam = params.get('ep');
  const asked = epParam !== null && /^\d+$/.test(epParam) ? Number(epParam) : null;
  const source: VizRef | null = useMemo(() => (datasetId ? { scope: 'dataset', id: datasetId } : null), [datasetId]);
  const model = useVizModel(source);
  const datasets = useVizDatasets();
  const item = datasets.data?.items.find((d) => d.id === datasetId);
  const [order, setOrder] = useState<number[]>([]);
  const [collapsed, setCollapsed] = useState(() => !!readPrefs().vizRailCollapsed);
  const fold = useCallback((folded: boolean) => {
    setCollapsed(folded);
    writePrefs({ vizRailCollapsed: folded });
  }, []);
  const unfold = collapsed ? <RailButton fold={false} onClick={() => fold(false)} /> : null;
  const control = useRef<PlayerControl | null>(null);
  const [display, setDisplay] = useState(false);
  // what the viewer chose to see of the EEF marks, shared with the mini player (kept in this browser)
  const [choice, setChoiceState] = useState<OverlayChoice>(() => parseChoice(readPrefs().eefOverlay));
  const setChoice = useCallback((c: OverlayChoice) => {
    setChoiceState(c);
    writePrefs({ eefOverlay: c });
  }, []);

  useEffect(() => {
    if (datasetId) writePrefs({ lastVizDataset: datasetId });
  }, [datasetId]);

  // an episode the dataset does not have (an old link, a typo) falls back to the first one
  const known = asked !== null && (!model.data || hasEpisode(model.data.episode_indices, model.data.episode_count, asked));
  const current = (known ? asked : null) ?? order[0] ?? (model.data ? firstEpisode(model.data.episode_indices, model.data.episode_count) : null);
  const go = useCallback(
    (dataset: string | null, ep: number | null) => {
      const next = new URLSearchParams();
      if (dataset) next.set('dataset', dataset);
      if (ep !== null) next.set('ep', String(ep));
      setParams(next, { replace: false });
    },
    [setParams],
  );
  const at = current === null ? -1 : order.indexOf(current);
  const prev = at > 0 ? order[at - 1] : null;
  const next = at >= 0 && at < order.length - 1 ? order[at + 1] : null;
  // an mcap dataset without a confirmed mapping (the model says so even when the list is stale)
  const pending = item?.viz?.state === 'mapping_pending' || model.data?.mapping.state === 'none';
  // the registration's EEF marks (design doc 25 §5.1): from its record and declaration, made once (202 meanwhile)
  const eef = useDatasetEefOverlay(datasetId, current, !!model.data && !pending, choice.maxGapMs);
  const ready = eef.data?.state === 'ready' ? eef.data.overlay : null;
  const overlays: PlayerOverlays | null = useMemo(() => {
    const cams = (ready?.cameras ?? []).filter((c) => c.viz_camera && !c.skipped && c.layers.length);
    if (!ready || !cams.length) return null;
    const names = new Map((model.data?.cameras ?? []).map((c) => [c.key, c.name]));
    return {
      cameras: Object.fromEntries(cams.map((c) => [c.viz_camera as string, c])),
      focus: null,
      choice,
      onChoice: setChoice,
      interpolation: ready.interpolation,
      dataset: true,
      unavailable: (ready.unavailable_cameras ?? []).map((u) => ({ name: (u.viz_camera && names.get(u.viz_camera)) || u.camera_id || u.source || '', reason: u.reason })),
    };
  }, [ready, model.data, choice, setChoice]);

  return (
    <div className="page">
      <PageHeader
        crumbs={[{ label: zh.nav.datasets, to: '/datasets' }, { label: zh.vizPage.title }]}
        title={zh.vizPage.title}
        docTitle={item ? `${zh.vizPage.title} · ${item.name}${current !== null ? ` · ep ${current}` : ''}` : zh.vizPage.title}
        extra={
          <Space>
            <Button disabled={!datasetId || item?.format === 'unsupported'} onClick={() => navigate(`/tasks/new?dataset_id=${encodeURIComponent(datasetId ?? '')}`)}>
              {zh.vizPage.newTask}
            </Button>
            <Button disabled={!datasetId || !model.data || pending} onClick={() => setDisplay(true)}>
              {zh.displayCfg.entry}
            </Button>
            <Button disabled={!datasetId} onClick={() => navigate(`/datasets/${encodeURIComponent(datasetId ?? '')}`)}>
              {zh.vizPage.detail}
            </Button>
          </Space>
        }
      />
      <div className={`vzpage${collapsed ? ' collapsed' : ''}`}>
        <EpisodeRail
          datasetId={datasetId}
          current={current}
          model={model.data}
          onDataset={(id) => go(id, null)}
          onEpisode={(ep) => go(datasetId, ep)}
          onOrder={setOrder}
          onFold={() => fold(true)}
        />
        <section className="vz-main">
          {!datasetId ? (
            <div className="vz-pending">
              {unfold ? <span className="lead">{unfold}</span> : null}
              {zh.vizPage.pickFirst}
            </div>
          ) : pending ? (
            <div className="vz-pending" role="alert">
              {unfold ? <span className="lead">{unfold}</span> : null}
              <p>{zh.vizPage.mappingPending(item?.viz?.reason ?? '')}</p>
              <Button type="primary" onClick={() => navigate(`/datasets/${encodeURIComponent(datasetId)}?mcap=1`)}>
                {zh.vizPage.goMapping}
              </Button>
            </div>
          ) : source && current !== null ? (
            <div style={{ marginBottom: 16 }}>
              {eef.data?.state === 'pending' ? (
                <div className="vz-note muted" data-testid="vz-overlay-making">
                  {zh.viz.overlay.making(eef.data.progress)}
                </div>
              ) : eef.data?.state === 'none' && eef.data.reason === 'not_generated' ? (
                // this episode has no trajectory (the others may): the server says why
                <div className="vz-note muted" data-testid="vz-overlay-none">
                  {eef.data.message}
                </div>
              ) : eef.data?.state === 'none' && eef.data.kind === 'missing_declaration' ? (
                <div className="vz-note muted" data-testid="vz-overlay-declare">
                  {zh.viz.overlay.declare}
                  <Button type="text" size="mini" onClick={() => navigate(`/datasets/${encodeURIComponent(datasetId)}?declaration=1`)}>
                    {zh.viz.overlay.declareGo}
                  </Button>
                </div>
              ) : eef.data?.state === 'failed' ? (
                <div className="vz-warn" role="alert" data-testid="vz-overlay-failed">
                  {zh.viz.overlay.failed}
                </div>
              ) : null}
              <Player
                source={source}
                index={current}
                mode="full"
                control={control}
                onClock={DEV_CLOCK}
                onPrevEpisode={prev !== null ? () => go(datasetId, prev) : undefined}
                onNextEpisode={next !== null ? () => go(datasetId, next) : undefined}
                lead={unfold}
                prefetch={next}
                overlays={overlays}
              />
            </div>
          ) : model.isError ? (
            // said here, not by a Player: its own observer mounting re-fetched the failed model, which went
            // back to pending, unmounted it and failed again - requests in a loop and nothing on screen
            <div className="vz-pending" role="alert">
              {unfold ? <span className="lead">{unfold}</span> : null}
              <p>
                {zh.viz.modelFailed}
                {model.error instanceof Error ? `：${model.error.message}` : ''}
              </p>
            </div>
          ) : unfold ? (
            // the model is on its way and no episode is known yet, or the dataset has none
            <div className="vz-lead-only">{unfold}</div>
          ) : null}
          {datasetId && model.data && !pending ? <DatasetInfo key={datasetId} datasetId={datasetId} model={model.data} player={control} /> : null}
        </section>
      </div>
      <DisplayConfigDrawer dataset={display && datasetId ? { id: datasetId, name: item?.name ?? model.data?.name ?? datasetId } : null} onClose={() => setDisplay(false)} />
    </div>
  );
}
