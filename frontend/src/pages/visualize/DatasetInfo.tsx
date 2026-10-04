import { Button, Card, Message, Spin } from '@arco-design/web-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api, unwrap } from '../../api/client';
import type { VizDataset, VizFieldNode } from '../../api/types';
import type { PlayerControl } from '../../features/visualizer/Player';
import { formatLabel } from '../../features/visualizer/Player';
import { zh } from '../../locales/zh';

function flatten(nodes: readonly VizFieldNode[]): VizFieldNode[] {
  return nodes.flatMap((n) => [n, ...flatten(n.children ?? [])]);
}

function sub(n: VizFieldNode): string {
  if (n.children) return String(n.children.length);
  if (n.shape?.length) return `${n.dtype ?? ''}[${n.shape.join(', ')}]`;
  return n.dtype ?? '';
}

function Node({ n, active, open, onOpen, onPick }: { n: VizFieldNode; active: string | null; open: ReadonlySet<string>; onOpen: (id: string) => void; onPick: (n: VizFieldNode) => void }) {
  const isOpen = open.has(n.id);
  const leaf = !n.children;
  return (
    <>
      <button
        type="button"
        className={`nd${isOpen ? ' open' : ''}${active === n.id ? ' active' : ''}`}
        onClick={() => (leaf ? onPick(n) : onOpen(n.id))}
        aria-expanded={leaf ? undefined : isOpen}
      >
        <span className={`car${leaf ? ' leaf' : ''}`}>▶</span>
        <span className="nm" title={n.name}>
          {n.name}
        </span>
        <span className="sub">{sub(n)}</span>
      </button>
      {!leaf && isOpen ? (
        <div className="kids">
          {n.children?.map((k) => (
            <Node key={k.id} n={k} active={active} open={open} onOpen={onOpen} onPick={onPick} />
          ))}
        </div>
      ) : null}
    </>
  );
}

function MetaPreview({ datasetId, path }: { datasetId: string; path: string }) {
  const meta = useQuery({
    queryKey: ['viz', 'dataset', datasetId, 'meta', path],
    queryFn: () => unwrap(api().GET('/datasets/{id}/viz/meta', { params: { path: { id: datasetId }, query: { path } } })),
    staleTime: 5 * 60_000,
  });
  if (meta.isLoading) return <Spin />;
  if (meta.isError || !meta.data) return <div className="empty">{zh.vizPage.previewFailed}</div>;
  let text = meta.data.text;
  if (meta.data.kind === 'json') {
    try {
      text = JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      // cut at 256 KiB: show it as it is
    }
  }
  return (
    <>
      <pre data-testid="vz-meta">{text}</pre>
      {meta.data.truncated ? <p className="muted">{zh.vizPage.previewTruncated(256)}</p> : null}
    </>
  );
}

/**
 * 「数据集信息」 (design doc 18 §5.7): the model's field tree on the left, the node's attributes on
 * the right, a metadata file's text, and 「加入播放器」 for cameras and curve groups.
 */
export function DatasetInfo({ datasetId, model, player }: { datasetId: string; model: VizDataset; player: React.MutableRefObject<PlayerControl | null> }) {
  const all = flatten(model.field_tree);
  const first = all.find((n) => !n.children) ?? null;
  const [active, setActive] = useState<string | null>(first?.id ?? null);
  const [open, setOpen] = useState<Set<string>>(() => new Set(model.field_tree.map((n) => n.id)));
  const node = all.find((n) => n.id === active) ?? null;
  const add = (c: Parameters<PlayerControl['add']>[0]) => {
    const how = player.current?.add(c);
    if (how) Message.success(how === 'empty' ? zh.vizPage.added : zh.vizPage.replaced);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };
  const attrs: [string, string][] = [];
  if (node) {
    if (node.dtype) attrs.push([zh.vizPage.attrs.dtype, node.dtype]);
    if (node.shape) attrs.push([zh.vizPage.attrs.shape, `[${node.shape.join(', ')}]`]);
    if (node.names?.length) attrs.push([zh.vizPage.attrs.names, node.names.join('、')]);
    if (node.file) attrs.push([zh.vizPage.attrs.file, node.file]);
    for (const [k, v] of Object.entries(node.detail ?? {})) if (v !== null && v !== '') attrs.push([k, String(v)]);
  }
  const stream = node?.stream ? model.streams.find((s) => s.key === node.stream) : undefined;
  return (
    <Card
      title={zh.vizPage.info}
      extra={<span className="muted">{`${formatLabel(model.format)} · ${zh.vizPage.summary(model.episode_count, model.cameras.length, model.fps, model.total_frames)}`}</span>}
      bodyStyle={{ padding: 0 }}
      data-testid="vz-info"
    >
      <div className="dsinfo">
        <div className="tree">
          {model.field_tree.map((n) => (
            <Node
              key={n.id}
              n={n}
              active={active}
              open={open}
              onOpen={(id) =>
                setOpen((o) => {
                  const next = new Set(o);
                  if (next.has(id)) next.delete(id);
                  else next.add(id);
                  return next;
                })
              }
              onPick={(k) => setActive(k.id)}
            />
          ))}
        </div>
        <div className="detail">
          {!node ? (
            <div className="empty">{zh.vizPage.infoNone}</div>
          ) : (
            <>
              <h4>{node.name}</h4>
              {attrs.length ? (
                <div className="attrs">
                  {attrs.map(([k, v]) => [
                    <span key={`${k}-k`} className="k">
                      {k}
                    </span>,
                    <span key={`${k}-v`}>{v}</span>,
                  ])}
                </div>
              ) : null}
              {node.file ? <MetaPreview datasetId={datasetId} path={node.file} /> : null}
              {node.camera || (stream && stream.available) ? (
                <div className="acts">
                  <Button type="primary" size="small" onClick={() => add(node.camera ? { kind: 'video', key: node.camera } : { kind: 'curve', key: stream!.key })}>
                    {zh.vizPage.addToPlayer}
                  </Button>
                </div>
              ) : null}
            </>
          )}
        </div>
      </div>
    </Card>
  );
}
