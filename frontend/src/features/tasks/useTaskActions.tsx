// Task operations shared by the task list and the task detail page: state actions, subtasks,
// delete / restore / purge, and the start flow with its two failure modes (precheck_failed,
// source_changed → fingerprint dialog → repreflight, D37).
import { Message, Modal } from '@arco-design/web-react';
import { useQueryClient } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, idempotencyKey, unwrap } from '../../api/client';
import { ApiError, errorMessage } from '../../api/errors';
import { qk, showSubtask, showTaskState } from '../../api/queries';
import type { SourceChange } from '../../api/types';
import type { TaskActionKey } from '../../lib/taskView';
import { zh } from '../../locales/zh';
import { FingerprintDialog, asSourceChange } from './FingerprintDialog';
import { PurgeDialog } from './PurgeDialog';

export interface ActionTarget {
  id: string;
  name: string;
  held?: number;
  deliveryUri?: string;
  exported?: boolean;
}

export interface PrecheckItem {
  /** input | output | vlm */
  check: string;
  ok: boolean;
  code?: string;
  reason?: string;
  target?: string;
}

/**
 * precheck_failed: C4 1.1.0 only says «a per-check reason». The W8 Daemon sends
 * details.checks = [{id: input|output|vlm, ok, code, reason (Chinese), target, elapsed_ms}];
 * older shapes ({check|name, ok, reason} or a map {input: {ok, reason} | "reason"}) still read.
 */
export function precheckItems(details: Record<string, unknown> | undefined): PrecheckItem[] {
  if (!details) return [];
  const raw = (details.checks ?? details) as unknown;
  if (Array.isArray(raw)) {
    return raw
      .filter((x): x is Record<string, unknown> => Boolean(x) && typeof x === 'object')
      .map((x) => ({
        check: String(x.id ?? x.check ?? x.name ?? ''),
        ok: Boolean(x.ok),
        code: typeof x.code === 'string' ? x.code : undefined,
        reason: x.reason ? String(x.reason) : undefined,
        target: typeof x.target === 'string' && x.target ? x.target : undefined,
      }));
  }
  if (raw && typeof raw === 'object') {
    return Object.entries(raw as Record<string, unknown>)
      .filter(([k]) => ['input', 'output', 'vlm'].includes(k))
      .map(([check, v]) =>
        typeof v === 'string'
          ? { check, ok: false, reason: v }
          : { check, ok: Boolean((v as Record<string, unknown>)?.ok), reason: (v as Record<string, unknown>)?.reason as string | undefined },
      );
  }
  return [];
}

export function showPrecheckFailure(err: ApiError): void {
  const items = precheckItems(err.details);
  Modal.error({
    title: zh.actions.precheckFailed,
    content: items.length ? (
      <ul style={{ margin: 0, paddingLeft: 18 }} data-testid="precheck-list">
        {items.map((i) => (
          <li key={i.check} style={{ color: i.ok ? 'var(--c-success)' : 'var(--c-danger)' }}>
            {zh.actions.precheck[i.check] ?? i.check}：{i.ok ? zh.actions.precheckPass : i.reason ?? zh.actions.precheckFail}
            {!i.ok && i.target ? <div className="mono muted">{i.target}</div> : null}
          </li>
        ))}
      </ul>
    ) : (
      err.message
    ),
  });
}

export function useTaskActions(): { run: (action: TaskActionKey, t: ActionTarget) => void; dialogs: ReactNode } {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [purgeId, setPurgeId] = useState<string | null>(null);
  const [fp, setFp] = useState<{ taskId: string; change: SourceChange | null } | null>(null);
  const [repreflighting, setRepreflighting] = useState(false);

  const refresh = (id: string) => {
    void qc.invalidateQueries({ queryKey: qk.tasksAll });
    void qc.invalidateQueries({ queryKey: ['task', id] });
    void qc.invalidateQueries({ queryKey: qk.overview });
  };

  const fail = (e: unknown) => {
    if (e instanceof ApiError && e.code === 'precheck_failed') showPrecheckFailure(e);
    else Message.error(errorMessage(e));
  };

  const act = async (id: string, action: 'start' | 'pause' | 'resume' | 'stop', done: string) => {
    try {
      const task = await unwrap(api().POST('/tasks/{id}/actions/{action}', { params: { path: { id, action }, header: { 'Idempotency-Key': idempotencyKey() } } }));
      showTaskState(qc, task);
      Message.success(done);
      refresh(id);
    } catch (e) {
      if (e instanceof ApiError && e.code === 'source_changed') {
        setFp({ taskId: id, change: asSourceChange(e.details) });
        return;
      }
      fail(e);
    }
  };

  const confirm = (title: string, content: ReactNode, onOk: () => Promise<void>, okText?: string, danger = false) =>
    Modal.confirm({ title, content, okText: okText ?? zh.common.ok, cancelText: zh.common.cancel, okButtonProps: danger ? { status: 'danger' } : undefined, onOk });

  const run = (action: TaskActionKey, t: ActionTarget) => {
    const id = t.id;
    switch (action) {
      case 'view':
        navigate(`/tasks/${id}`);
        return;
      case 'report':
        navigate(`/tasks/${id}/report`);
        return;
      case 'adjudicate':
        navigate(`/tasks/${id}/adjudication`);
        return;
      case 'edit':
        navigate(`/tasks/new?edit=${encodeURIComponent(id)}`);
        return;
      case 'copy':
        navigate(`/tasks/new?copy=${encodeURIComponent(id)}`);
        return;
      case 'start':
        void act(id, 'start', zh.actions.done.start);
        return;
      case 'pause':
        void act(id, 'pause', zh.actions.done.pause);
        return;
      case 'resume':
        void act(id, 'resume', zh.actions.done.resume);
        return;
      case 'stop':
      case 'cancelQueue':
        confirm(action === 'stop' ? zh.actions.confirmStop.title : zh.actions.cancelQueue, zh.actions.confirmStop.content, () => act(id, 'stop', zh.actions.done.stop), undefined, true);
        return;
      case 'retry':
        confirm(
          zh.actions.confirmRetry.title,
          zh.actions.confirmRetry.content(t.held ?? 0),
          async () => {
            try {
              const r = await unwrap(api().POST('/tasks/{id}/retry', { params: { path: { id }, header: { 'Idempotency-Key': idempotencyKey() } }, body: {} }));
              showSubtask(qc, id, r.subtask);
              Message.success(zh.actions.done.retry);
              refresh(id);
            } catch (e) {
              fail(e);
            }
          },
          zh.actions.confirmRetry.ok,
        );
        return;
      case 'continue':
        confirm(
          zh.actions.confirmContinue.title,
          zh.actions.confirmContinue.content,
          async () => {
            try {
              const r = await unwrap(api().POST('/tasks/{id}/continue', { params: { path: { id }, header: { 'Idempotency-Key': idempotencyKey() } } }));
              showSubtask(qc, id, r.subtask);
              Message.success(zh.actions.done.continue);
              refresh(id);
            } catch (e) {
              fail(e);
            }
          },
          zh.actions.confirmContinue.ok,
        );
        return;
      case 'export':
        confirm(
          t.exported ? zh.actions.confirmExport.titleAgain : zh.actions.confirmExport.titleFirst,
          zh.actions.confirmExport.content(t.deliveryUri ?? zh.taskForm.outputUri),
          async () => {
            try {
              const r = await unwrap(api().POST('/tasks/{id}/reexport', { params: { path: { id }, header: { 'Idempotency-Key': idempotencyKey() } } }));
              showSubtask(qc, id, r.subtask);
              Message.success(zh.actions.done.reexport);
              refresh(id);
            } catch (e) {
              fail(e);
            }
          },
          zh.actions.confirmExport.ok,
        );
        return;
      case 'delete':
        confirm(
          zh.actions.confirmDelete.title,
          zh.actions.confirmDelete.content(t.name),
          async () => {
            try {
              await unwrap(api().DELETE('/tasks/{id}', { params: { path: { id } } }));
              Message.success(zh.actions.done.delete);
              refresh(id);
            } catch (e) {
              fail(e);
            }
          },
          zh.actions.delete,
          true,
        );
        return;
      case 'restore':
        void (async () => {
          try {
            const task = await unwrap(api().POST('/tasks/{id}/restore', { params: { path: { id } } }));
            showTaskState(qc, task);
            Message.success(zh.actions.done.restore);
            refresh(id);
          } catch (e) {
            fail(e);
          }
        })();
        return;
      case 'purge':
        setPurgeId(id);
        return;
      default:
        return;
    }
  };

  const confirmRepreflight = async () => {
    if (!fp) return;
    setRepreflighting(true);
    try {
      const res = await unwrap(api().POST('/tasks/{id}/repreflight', { params: { path: { id: fp.taskId }, header: { 'Idempotency-Key': idempotencyKey() } } }));
      refresh(fp.taskId);
      setFp(null);
      if (res.compatible) {
        Message.success(zh.fingerprint.compatible);
        navigate(`/tasks/${fp.taskId}`);
      } else {
        Message.warning(zh.fingerprint.incompatible);
        navigate(`/tasks/new?edit=${encodeURIComponent(fp.taskId)}`, { state: { incompatibilities: res.incompatibilities } });
      }
    } catch (e) {
      fail(e);
    } finally {
      setRepreflighting(false);
    }
  };

  const dialogs = (
    <>
      <PurgeDialog taskId={purgeId} onClose={() => setPurgeId(null)} />
      <FingerprintDialog visible={Boolean(fp)} change={fp?.change ?? null} loading={repreflighting} onCancel={() => setFp(null)} onConfirm={() => void confirmRepreflight()} />
    </>
  );

  return { run, dialogs };
}
