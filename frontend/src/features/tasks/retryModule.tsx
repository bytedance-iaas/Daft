import { Message, Modal } from '@arco-design/web-react';
import type { QueryClient } from '@tanstack/react-query';
import { api, idempotencyKey, unwrap } from '../../api/client';
import { errorMessage, isApiError } from '../../api/errors';
import { showSubtask } from '../../api/queries';
import { zh } from '../../locales/zh';
import { showPrecheckFailure } from './useTaskActions';

/**
 * 「重试此模块」 (task detail module table, report sections): a retry subtask scoped to one module —
 * its error episodes, or the whole module when it failed outright (03 §3.2).
 */
export function confirmModuleRetry({ taskId, moduleId, name, qc, onDone }: { taskId: string; moduleId: string; name: string; qc: QueryClient; onDone: () => void }): void {
  Modal.confirm({
    title: zh.taskDetail.retryModuleTitle(name),
    content: zh.actions.confirmRetry.contentModules(name),
    okText: zh.actions.confirmRetry.ok,
    cancelText: zh.common.cancel,
    onOk: async () => {
      try {
        const r = await unwrap(api().POST('/tasks/{id}/retry', { params: { path: { id: taskId }, header: { 'Idempotency-Key': idempotencyKey() } }, body: { modules: [moduleId] } }));
        showSubtask(qc, taskId, r.subtask);
        Message.success(zh.actions.done.retry);
        onDone();
      } catch (e) {
        if (isApiError(e, 'precheck_failed')) showPrecheckFailure(e);
        else Message.error(errorMessage(e));
      }
    },
  });
}
