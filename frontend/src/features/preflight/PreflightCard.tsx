import { Alert, Button, Card, Collapse, Space, Spin, Tag, Typography } from '@arco-design/web-react';
import { errorMessage } from '../../api/errors';
import type { ModuleRegistry, PreflightResult } from '../../api/types';
import { grouped } from '../../lib/format';
import { needsVlm, reasonText } from '../../lib/preflight';
import { zh } from '../../locales/zh';
import type { PreflightState } from './usePreflight';

function formatName(r: PreflightResult): string {
  if (r.format.kind === 'lerobot') return `LeRobot ${r.format.version ?? ''}`.trim();
  return r.format.kind;
}

/**
 * 预检结果 (07 §3): what was recognised, what needs attention; failures stay here, inline, never
 * as a global error. CLI `warnings` are English and shown as they are.
 */
export function PreflightCard({ state, registry, onRerun }: { state: PreflightState & { current: boolean }; registry: ModuleRegistry | undefined; onRerun: () => void }) {
  const r = state.result;
  const tag =
    state.status === 'running' ? (
      <Tag>{zh.taskForm.preflightRunning}</Tag>
    ) : state.status === 'error' ? (
      <Tag color="red">{zh.taskForm.preflightFailed}</Tag>
    ) : r ? (
      r.format.supported ? (
        <Tag color="green">{zh.taskForm.preflightOk}</Tag>
      ) : (
        <Tag color="red">{zh.taskForm.preflightUnsupported}</Tag>
      )
    ) : null;
  const vlmModules = registry?.modules.filter((m) => needsVlm(m)) ?? [];
  const embodimentNeeded = r?.modules.find((m) => m.availability === 'needs_input' && m.input_hint?.field === 'embodiment_id');
  return (
    <Card
      title={
        <Space>
          {zh.taskForm.sectionPreflight}
          {tag}
        </Space>
      }
      extra={
        state.status !== 'idle' ? (
          <Button size="small" onClick={onRerun} disabled={state.status === 'running'}>
            {zh.taskForm.preflightRedo}
          </Button>
        ) : null
      }
      data-testid="preflight-card"
    >
      {state.status === 'idle' ? <Typography.Text type="secondary">{zh.taskForm.preflightIdle}</Typography.Text> : null}
      {state.status === 'running' && !r ? <Spin tip={zh.taskForm.preflightRunning} /> : null}
      {state.status === 'error' ? <Alert type="error" content={errorMessage(state.error)} /> : null}
      {r ? (
        <Space direction="vertical" style={{ width: '100%' }}>
          {r.format.supported && r.dataset ? (
            <Alert
              type="success"
              content={
                <div>
                  <div>
                    {zh.taskForm.preflightSummary(
                      formatName(r),
                      r.dataset.episode_count,
                      r.dataset.cameras.length,
                      r.dataset.cameras.join(' / '),
                      r.dataset.fps ? String(r.dataset.fps) : '',
                      r.dataset.total_frames ? grouped(r.dataset.total_frames) : '',
                    )}
                  </div>
                  <div className="muted" style={{ fontSize: 12 }}>
                    {zh.taskForm.preflightReadOnly}
                  </div>
                </div>
              }
            />
          ) : (
            <Alert type="error" content={reasonText(r.modules[0]) || r.format.detail} />
          )}
          {r.validation.length ? (
            <Alert
              type="error"
              title={zh.taskForm.validation}
              content={
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {r.validation.map((x) => (
                    <li key={x}>{x}</li>
                  ))}
                </ul>
              }
            />
          ) : null}
          {r.dataset && r.dataset.labels.without_task > 0 && vlmModules.length ? <Alert type="info" content={zh.taskForm.unlabeled(r.dataset.labels.without_task)} /> : null}
          {embodimentNeeded ? <Alert type="warning" content={reasonText(embodimentNeeded)} /> : null}
          {r.dataset ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {r.dataset.profile ? zh.taskForm.profileHit(r.dataset.profile.matched, r.dataset.profile.by) : zh.taskForm.profileMiss}
            </Typography.Text>
          ) : null}
          {r.warnings.length ? (
            <Collapse bordered={false}>
              <Collapse.Item header={zh.taskForm.otherWarnings} name="w">
                <ul className="mono" style={{ margin: 0, paddingLeft: 18 }}>
                  {r.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </Collapse.Item>
            </Collapse>
          ) : null}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {zh.taskForm.greyNote}
          </Typography.Text>
        </Space>
      ) : null}
    </Card>
  );
}
