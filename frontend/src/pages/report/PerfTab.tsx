import { Card, Descriptions, Grid, Radio, Select, Space, Spin, Table, Typography } from '@arco-design/web-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api, unwrap } from '../../api/client';
import { qk } from '../../api/queries';
import type { Perf, Subtask } from '../../api/types';
import { CHART_COLORS, Chart, barOption, chartSummary, type ChartOption } from '../../components/Chart';
import { PageError } from '../../components/PageError';
import { StatCell } from '../../components/StatCell';
import { compactNumber, percent } from '../../lib/format';
import { callKindLabel, fieldLabel, formatValue, seconds, subtaskName, usageByCallKind } from '../../lib/reportView';
import { stageLabel } from '../../lib/taskView';
import { zh } from '../../locales/zh';

const { Row, Col } = Grid;

const Stat = StatCell;

function latencyOption(rows: Perf['latency']): ChartOption {
  const names = rows.map((r) => r.call_kind);
  const series = (['p50_s', 'p90_s', 'p99_s'] as const).map((k, i) => ({ name: k.slice(0, 3).toUpperCase(), type: 'bar', data: rows.map((r) => r[k]), barMaxWidth: 16, itemStyle: { color: CHART_COLORS[i] } }));
  return {
    grid: { left: 48, right: 16, top: 32, bottom: 28 },
    legend: { top: 0 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'category', data: names, axisTick: { show: false } },
    yAxis: { type: 'value', name: zh.report.secondsUnit, splitLine: { lineStyle: { color: '#E5E6EB' } } },
    series,
  };
}

function objectRows(o: Record<string, unknown> | undefined): { label: string; value: string }[] {
  return Object.entries(o ?? {}).map(([k, v]) => ({ label: zh.report.perfServiceKeys[k] ?? fieldLabel(k), value: k === 'reasoning_effort' && v === null ? zh.taskDetail.cfgEffortDefault : formatValue(v) }));
}

type Scope = { kind: 'all' } | { kind: 'main' } | { kind: 'subtask'; id: string };

/**
 * 性能剖析 (07 §5, 06 §6.1): latency by call kind (v1 labels verbatim, wall-clock, never
 * count × mean), effective concurrency, stage wall shares, retries and merging, interrupted
 * redo count (D26); all calls, main run only, or one subtask.
 */
export function PerfTab({ taskId, rev, subtasks }: { taskId: string; rev: number; subtasks: readonly Subtask[] }) {
  const [scope, setScope] = useState<Scope>({ kind: 'all' });
  const subtaskId = scope.kind === 'subtask' ? scope.id : null;
  const perf = useQuery({
    queryKey: qk.perf(taskId, rev, scope.kind, subtaskId),
    queryFn: () => unwrap(api().GET('/tasks/{id}/perf', { params: { path: { id: taskId }, query: { rev, scope: scope.kind, ...(subtaskId ? { subtask: subtaskId } : {}) } } })),
  });
  const usage = useQuery({ queryKey: qk.usage(taskId), queryFn: () => unwrap(api().GET('/tasks/{id}/usage', { params: { path: { id: taskId } } })) });
  const p = perf.data;
  const radioValue = scope.kind === 'subtask' ? 'subtask' : scope.kind;
  const tokens = usageByCallKind(usage.data?.actual ?? [], scope.kind, subtaskId);
  return (
    <div className="card-gap" data-testid="perf">
      <Card>
        <Space wrap>
          <span>{zh.report.perfScope}</span>
          <Radio.Group
            type="button"
            value={radioValue}
            aria-label={zh.report.perfScope}
            onChange={(v: string) => {
              if (v === 'all' || v === 'main') setScope({ kind: v });
              else if (subtasks.length) setScope({ kind: 'subtask', id: subtasks[subtasks.length - 1].id });
            }}
          >
            <Radio value="all">{zh.report.perfAll}</Radio>
            <Radio value="main">{zh.report.perfMain}</Radio>
            {subtasks.length ? <Radio value="subtask">{zh.report.perfSubtask(subtaskName(subtasks, subtaskId ?? subtasks[subtasks.length - 1].id))}</Radio> : null}
          </Radio.Group>
          {scope.kind === 'subtask' && subtasks.length > 1 ? (
            <Select
              style={{ width: 200 }}
              value={scope.id}
              aria-label={zh.report.perfSubtask('')}
              onChange={(id: string) => setScope({ kind: 'subtask', id })}
              options={subtasks.map((s) => ({ label: subtaskName(subtasks, s.id), value: s.id }))}
            />
          ) : null}
        </Space>
        <Typography.Paragraph type="secondary" style={{ margin: '8px 0 0', fontSize: 12 }}>
          {zh.report.perfScopeDesc}
        </Typography.Paragraph>
      </Card>
      {perf.isLoading ? (
        <Spin style={{ display: 'block', margin: '48px auto' }} />
      ) : !p ? (
        <PageError error={perf.error} onRetry={() => void perf.refetch()} />
      ) : (
        <>
          <Card title={zh.report.perfService}>
            <Row gutter={24}>
              <Col span={12}>
                <Descriptions column={1} data={objectRows(p.backend)} />
              </Col>
              <Col span={12}>
                <Descriptions column={1} data={objectRows(p.container)} />
              </Col>
            </Row>
          </Card>
          <Card title={zh.report.perfLatency} extra={<span className="muted">{zh.report.perfLatencyDesc}</span>}>
            {p.latency.length ? (
              <>
                <Chart option={latencyOption(p.latency)} summary={p.latency.map((r) => `${r.call_kind} P50 ${r.p50_s} P90 ${r.p90_s} P99 ${r.p99_s}`).join('，')} height={240} />
                <Table
                  rowKey="call_kind"
                  size="small"
                  pagination={false}
                  data={p.latency}
                  data-testid="perf-latency"
                  columns={[
                    { title: zh.report.colCall, dataIndex: 'call_kind', render: (v: string) => <span className="mono">{callKindLabel(v)}</span> },
                    { title: zh.report.colCount, dataIndex: 'count' },
                    { title: zh.report.colFailed, dataIndex: 'failed', render: (v?: number) => v ?? 0 },
                    { title: zh.report.colHedged, dataIndex: 'hedged', render: (v?: number) => v ?? 0 },
                    { title: 'P50', dataIndex: 'p50_s', render: (v: number) => seconds(v) },
                    { title: 'P90', dataIndex: 'p90_s', render: (v: number) => seconds(v) },
                    { title: 'P99', dataIndex: 'p99_s', render: (v: number) => seconds(v) },
                    { title: zh.report.colWall, dataIndex: 'wall_s', render: (v: number) => seconds(v) },
                  ]}
                />
                <Typography.Paragraph type="secondary" style={{ margin: '8px 0 0', fontSize: 12 }}>
                  {zh.report.perfLatencyNote}
                </Typography.Paragraph>
              </>
            ) : (
              <Typography.Text type="secondary">{zh.report.perfNoData}</Typography.Text>
            )}
          </Card>
          <div className="stat-grid">
            <Stat label={zh.report.perfConcurrency} value={p.effective_concurrency === null || p.effective_concurrency === undefined ? '—' : String(p.effective_concurrency)} foot={zh.report.perfConcurrencyFoot} />
            <Stat label={zh.report.outerAttempts} value={String(p.retries?.outer_attempts ?? 0)} foot={`${zh.report.rescued} ${p.retries?.rescued ?? 0}`} />
            <Stat label={zh.report.mergeRequests} value={String(p.merge?.requests ?? 0)} foot={`${zh.report.mergeUnmerged} ${p.merge?.estimated_unmerged ?? 0}`} />
            <Stat label={zh.report.redone} value={String(p.redone_after_interruption ?? 0)} foot={zh.report.redoneFoot} />
          </div>
          <Card title={zh.report.perfStages}>
            {p.stages.length ? (
              <Row gutter={24}>
                <Col span={14}>
                  <Chart
                    option={barOption(
                      p.stages.map((s) => ({ name: stageLabel(s.id), value: s.wall_s })),
                      { horizontal: true },
                    )}
                    summary={chartSummary(p.stages.map((s) => ({ name: stageLabel(s.id), value: s.wall_s })))}
                    height={Math.max(160, p.stages.length * 30)}
                  />
                </Col>
                <Col span={10}>
                  <Table
                    rowKey="id"
                    size="small"
                    pagination={false}
                    data={p.stages}
                    data-testid="perf-stages"
                    columns={[
                      { title: zh.taskDetail.colStage, dataIndex: 'id', render: (v: string) => stageLabel(v) },
                      { title: zh.report.colWall, dataIndex: 'wall_s', render: (v: number) => seconds(v) },
                      { title: zh.report.colShare, dataIndex: 'share', render: (v?: number) => percent(v ?? null, 0) },
                    ]}
                  />
                </Col>
              </Row>
            ) : (
              <Typography.Text type="secondary">{zh.report.perfNoData}</Typography.Text>
            )}
          </Card>
          <Card title={zh.report.perfTokens} extra={<span className="muted">{zh.report.perfTokensDesc}</span>}>
            {tokens.length ? (
              <Table
                rowKey="call_kind"
                size="small"
                pagination={false}
                data={tokens}
                data-testid="perf-tokens"
                columns={[
                  { title: zh.report.colCall, dataIndex: 'call_kind', render: (v: string) => <span className="mono">{callKindLabel(v)}</span> },
                  { title: zh.taskDetail.tokenInput, dataIndex: 'prompt', render: (v: number) => compactNumber(v) },
                  { title: zh.taskDetail.tokenOutput, dataIndex: 'completion', render: (v: number) => compactNumber(v) },
                  { title: zh.taskDetail.tokenReasoning, dataIndex: 'reasoning', render: (v: number) => compactNumber(v) },
                  { title: zh.taskDetail.tokenCached, dataIndex: 'cached', render: (v: number) => compactNumber(v) },
                  { title: zh.taskDetail.tokenRequests, dataIndex: 'requests' },
                ]}
              />
            ) : (
              <Typography.Text type="secondary">{zh.report.perfNoData}</Typography.Text>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
