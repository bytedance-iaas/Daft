import { Button, Dropdown, Menu, Space, Tooltip } from '@arco-design/web-react';
import { IconDown } from '@arco-design/web-react/icon';
import type { ActionPlan, TaskActionKey } from '../../lib/taskView';
import { zh } from '../../locales/zh';

export function actionLabel(key: TaskActionKey, opts: { held?: number; exported?: boolean; pending?: number } = {}): string {
  switch (key) {
    case 'retry':
      return zh.taskList.retryCount(opts.held ?? 0);
    case 'export':
      return opts.exported ? zh.actions.reexport : zh.actions.export;
    case 'adjudicate':
      // D47: the count when there are pending items, the plain entry otherwise.
      return opts.pending ? zh.actions.adjudicateCount(opts.pending) : zh.actions.adjudicate;
    default:
      return (zh.actions as unknown as Record<string, string>)[key] ?? key;
  }
}

/** A primary action and the rest under 「更多」, with disabled ones explained (07 §4.1). */
export function TaskActionButtons({
  plan,
  onAction,
  held,
  exported,
  pending,
  size = 'small',
  primaryType = 'text',
}: {
  plan: ActionPlan;
  onAction: (key: TaskActionKey) => void;
  held?: number;
  exported?: boolean;
  /** Pending adjudication items, for 人工裁决（N）. */
  pending?: number;
  size?: 'mini' | 'small' | 'default';
  primaryType?: 'text' | 'primary' | 'secondary';
}) {
  const label = (k: TaskActionKey) => actionLabel(k, { held, exported, pending });
  const primary = plan.primary;
  const primaryDisabled = primary ? plan.disabled[primary] : undefined;
  return (
    <Space size={4}>
      {primary ? (
        <Tooltip content={primaryDisabled} disabled={!primaryDisabled}>
          <Button type={primaryType} size={size} disabled={Boolean(primaryDisabled)} onClick={() => onAction(primary)}>
            {label(primary)}
          </Button>
        </Tooltip>
      ) : null}
      {plan.more.length ? (
        <Dropdown
          trigger="click"
          position="br"
          droplist={
            <Menu onClickMenuItem={(key) => onAction(key as TaskActionKey)}>
              {plan.more.map((k) => (
                <Menu.Item key={k} disabled={Boolean(plan.disabled[k])}>
                  {plan.disabled[k] ? (
                    <Tooltip content={plan.disabled[k]} position="left">
                      <span>{label(k)}</span>
                    </Tooltip>
                  ) : (
                    <span style={k === 'delete' || k === 'purge' ? { color: 'var(--c-danger)' } : undefined}>{label(k)}</span>
                  )}
                </Menu.Item>
              ))}
            </Menu>
          }
        >
          <Button type={primaryType === 'text' ? 'text' : 'secondary'} size={size}>
            {zh.common.more} <IconDown />
          </Button>
        </Dropdown>
      ) : null}
    </Space>
  );
}
