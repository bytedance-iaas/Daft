import { Button, Card, Grid, Input, InputNumber, Radio, Typography } from '@arco-design/web-react';
import { useInfiniteQuery } from '@tanstack/react-query';
import { api, unwrap } from '../../api/client';
import type { EpisodePreview } from '../../api/types';
import { LazyVisible, Sentinel } from '../../components/LazyVisible';
import { parseForDisplay, selectionCount, toggleInExpr } from '../../lib/episodes';
import { zh } from '../../locales/zh';
import { Field } from './Field';
import type { Errors, FormValues } from './formModel';

const { Row, Col } = Grid;

export type PreviewSource = { dataset_id: string } | { source: 'tos' | 'public' | 'local'; uri: string; region?: string; credential?: string };

function Thumb({ ep }: { ep: EpisodePreview }) {
  const cam = ep.cameras[0];
  return (
    <div className="episode-thumb">
      {cam ? (
        <LazyVisible placeholder={<span>ep {ep.index}</span>}>
          {/* The browser takes the first frame itself (03 §10: no decoding on the server). */}
          <video preload="metadata" muted playsInline src={`${cam.url}#t=${cam.from_ts ?? 0},${(cam.from_ts ?? 0) + 0.1}`} aria-label={`ep ${ep.index} ${cam.name}`} />
        </LazyVisible>
      ) : (
        <span>ep {ep.index}</span>
      )}
    </div>
  );
}

function PreviewGrid({ source, expr, onToggle, disabled }: { source: PreviewSource; expr: string; onToggle: (i: number) => void; disabled: boolean }) {
  const q = useInfiniteQuery({
    queryKey: ['episode-previews', source],
    queryFn: ({ pageParam }) =>
      unwrap(api().GET('/datasets/episodes', { params: { query: { ...source, limit: 48, ...(pageParam ? { cursor: pageParam } : {}) } } })),
    initialPageParam: '' as string,
    getNextPageParam: (last) => (last.has_more && last.next_cursor ? last.next_cursor : undefined),
  });
  const picked = parseForDisplay(expr).indices;
  const items = q.data?.pages.flatMap((p) => p.items ?? []) ?? [];
  return (
    <div>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {zh.taskForm.gridHelp}
      </Typography.Text>
      <div className="episode-grid" style={{ marginTop: 8 }} data-testid="episode-grid">
        {items.map((ep) => (
          <div
            key={ep.index}
            role="checkbox"
            aria-checked={picked.has(ep.index)}
            aria-label={`ep ${ep.index}`}
            tabIndex={0}
            className={`episode-cell${picked.has(ep.index) ? ' picked' : ''}`}
            onClick={() => !disabled && onToggle(ep.index)}
            onKeyDown={(e) => {
              if (!disabled && (e.key === 'Enter' || e.key === ' ')) {
                e.preventDefault();
                onToggle(ep.index);
              }
            }}
          >
            <Thumb ep={ep} />
            <div style={{ padding: '4px 6px' }}>
              <div>
                <b>ep {ep.index}</b>
                {ep.length_s ? <span className="muted"> · {ep.length_s}s</span> : null}
              </div>
              <div className="muted" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={ep.task}>
                {ep.task || zh.taskForm.gridTaskNone}
              </div>
            </div>
          </div>
        ))}
        <Sentinel onVisible={() => q.hasNextPage && !q.isFetchingNextPage && void q.fetchNextPage()} disabled={!q.hasNextPage} version={q.data?.pages.length ?? 0} />
      </div>
      {q.hasNextPage ? (
        <Button size="small" type="text" loading={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>
          {zh.taskForm.gridLoadMore}
        </Button>
      ) : null}
    </div>
  );
}

/** Episode 选择 (07 §3): 全部 / 前 N 条 / 自选 with a first-frame preview grid synced to the text. */
export function EpisodeSection({
  v,
  set,
  errors,
  total,
  preview,
}: {
  v: FormValues;
  set: (patch: Partial<FormValues>) => void;
  errors: Errors;
  total: number | null;
  preview: PreviewSource | null;
}) {
  const count = selectionCount(v.episodeMode, v.headN, v.expr, total);
  const parsed = parseForDisplay(v.expr, total);
  return (
    <Card
      title={zh.taskForm.sectionEpisodes}
      extra={
        <Typography.Text type="secondary" data-testid="episode-count">
          {zh.taskForm.selectedCount(count ?? '?', total ?? '?')}
        </Typography.Text>
      }
    >
      <Radio.Group type="button" value={v.episodeMode} onChange={(x: FormValues['episodeMode']) => set({ episodeMode: x })} aria-label={zh.taskForm.sectionEpisodes}>
        <Radio value="all">{zh.taskForm.episodesAll(total ?? '')}</Radio>
        <Radio value="head">{zh.taskForm.episodesHead}</Radio>
        <Radio value="explicit">{zh.taskForm.episodesExplicit}</Radio>
      </Radio.Group>
      {v.episodeMode === 'head' ? (
        <Row style={{ marginTop: 16 }}>
          <Col span={8}>
            <Field label={`${zh.taskForm.headN}（${zh.taskForm.headHelp}）`} required error={errors.headN}>
              <InputNumber
                value={v.headN}
                min={1}
                precision={0}
                onChange={(x) => set({ headN: x === undefined || x === null ? undefined : Number(x) })}
                aria-label={zh.taskForm.headN}
              />
            </Field>
          </Col>
        </Row>
      ) : null}
      {v.episodeMode === 'explicit' ? (
        <div style={{ marginTop: 16 }}>
          <Field label={`${zh.taskForm.expr}（${zh.taskForm.exprHelp}）`} required error={errors.expr} extra={!parsed.ok ? zh.taskForm.exprUnparsed : undefined}>
            <Input className="mono" value={v.expr} onChange={(x) => set({ expr: x })} placeholder={zh.taskForm.exprPlaceholder} aria-label={zh.taskForm.expr} />
          </Field>
          {preview ? (
            <PreviewGrid
              source={preview}
              expr={v.expr}
              disabled={!parsed.ok}
              onToggle={(i) => {
                const next = toggleInExpr(v.expr, i);
                if (next !== null) set({ expr: next });
              }}
            />
          ) : (
            <Typography.Text type="secondary">{zh.taskForm.gridEmpty}</Typography.Text>
          )}
        </div>
      ) : null}
    </Card>
  );
}
