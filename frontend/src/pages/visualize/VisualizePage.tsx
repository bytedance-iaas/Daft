import { Button, Space } from '@arco-design/web-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { PageHeader } from '../../components/PageHeader';
import { useVizModel, type VizRef } from '../../features/visualizer/data';
import { Player, type PlayerControl } from '../../features/visualizer/Player';
import { readPrefs, writePrefs } from '../../lib/prefs';
import { firstEpisode, hasEpisode } from '../../lib/vizTime';
import { zh } from '../../locales/zh';
import { DatasetInfo } from './DatasetInfo';
import { EpisodeRail, useVizDatasets } from './EpisodeRail';
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
 * in the address, the one shown last.
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
  const control = useRef<PlayerControl | null>(null);

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

  return (
    <div className="page">
      <PageHeader
        crumbs={[{ label: zh.nav.datasets, to: '/datasets' }, { label: zh.vizPage.title }]}
        title={zh.vizPage.title}
        docTitle={item ? `${zh.vizPage.title} · ${item.name}${current !== null ? ` · ep ${current}` : ''}` : zh.vizPage.title}
        description={zh.vizPage.desc}
        extra={
          <Space>
            <Button disabled={!datasetId || item?.format === 'unsupported'} onClick={() => navigate(`/tasks/new?dataset_id=${encodeURIComponent(datasetId ?? '')}`)}>
              {zh.vizPage.newTask}
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
        />
        <button
          type="button"
          className="vz-rail-toggle"
          title={zh.vizPage.railToggle}
          aria-label={zh.vizPage.railToggle}
          onClick={() =>
            setCollapsed((c) => {
              writePrefs({ vizRailCollapsed: !c });
              return !c;
            })
          }
        >
          {collapsed ? '›' : '‹'}
        </button>
        <section className="vz-main">
          {!datasetId ? (
            <div className="vz-pending">{zh.vizPage.pickFirst}</div>
          ) : pending ? (
            <div className="vz-pending" role="alert">
              <p>{zh.vizPage.mappingPending(item?.viz?.reason ?? '')}</p>
              <Button type="primary" onClick={() => navigate(`/datasets/${encodeURIComponent(datasetId)}`)}>
                {zh.vizPage.goMapping}
              </Button>
            </div>
          ) : source && current !== null ? (
            <div style={{ marginBottom: 16 }}>
              <Player
                source={source}
                index={current}
                mode="full"
                control={control}
                onClock={DEV_CLOCK}
                onPrevEpisode={prev !== null ? () => go(datasetId, prev) : undefined}
                onNextEpisode={next !== null ? () => go(datasetId, next) : undefined}
              />
            </div>
          ) : model.isError ? (
            <Player source={source as VizRef} index={0} mode="full" />
          ) : null}
          {datasetId && model.data && !pending ? <DatasetInfo key={datasetId} datasetId={datasetId} model={model.data} player={control} /> : null}
        </section>
      </div>
    </div>
  );
}
