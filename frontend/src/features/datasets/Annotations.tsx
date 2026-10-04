import { Button, Message, Modal, Space } from '@arco-design/web-react';
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { api, idempotencyKey, unwrap } from '../../api/client';
import { ApiError, errorMessage } from '../../api/errors';
import { qk } from '../../api/queries';
import type { DatasetDetail, Upload, UploadIssue } from '../../api/types';
import { uploadAnnotations, type UploadPhase } from '../../api/uploads';
import { zh } from '../../locales/zh';

const ACCEPT = '.json,.zip,application/json,application/zip';

/**
 * Picking an external annotation file (design doc 18 §4.5): a JSON or a zip goes to POST /uploads,
 * which checks it against the formats it knows; a rejected file shows its located problems.
 */
function useAnnotationUpload(onUploaded: (u: Upload) => void) {
  const input = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<UploadPhase | null>(null);
  const [problem, setProblem] = useState<{ message: string; errors: UploadIssue[] } | null>(null);
  const pick = async (file: File | undefined) => {
    if (!file) return;
    setProblem(null);
    setPhase('uploading');
    try {
      onUploaded(await uploadAnnotations(file, setPhase));
    } catch (e) {
      const details = e instanceof ApiError ? (e.details as { errors?: UploadIssue[] } | undefined) : undefined;
      setProblem({ message: errorMessage(e), errors: details?.errors ?? [] });
    } finally {
      setPhase(null);
      if (input.current) input.current.value = '';
    }
  };
  const picker = <input ref={input} type="file" accept={ACCEPT} style={{ display: 'none' }} aria-label={zh.annotations.label} onChange={(e) => void pick(e.target.files?.[0])} />;
  const label = phase === 'uploading' ? zh.annotations.uploading : phase === 'validating' ? zh.annotations.validating : null;
  const error: ReactNode = problem ? (
    <div className="field-note-error" style={{ marginTop: 4 }} data-testid="annotations-error">
      {problem.message}
      {problem.errors.length > 1 ? (
        <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
          {problem.errors.slice(0, 5).map((e, i) => (
            <li key={i}>{e.problem}</li>
          ))}
        </ul>
      ) : null}
    </div>
  ) : null;
  return { picker, open: () => input.current?.click(), busy: phase !== null, label, error };
}

function uploadedText(u: Upload): string {
  const s = u.validation.summary as { episodes?: number; segments?: number; events?: number };
  return zh.annotations.uploaded(u.name, s.episodes ?? 0, s.segments ?? null, s.events ?? null);
}

/** The add drawer's 外部标注文件 field: the uploaded file is attached when the dataset is saved. */
export function AnnotationsField({ value, onChange, onBusy }: { value: Upload | null; onChange: (u: Upload | null) => void; onBusy?: (busy: boolean) => void }) {
  const up = useAnnotationUpload((u) => onChange(u));
  // the drawer holds 保存 while a file is on its way
  const busyRef = useRef(onBusy);
  busyRef.current = onBusy;
  useEffect(() => {
    busyRef.current?.(up.busy);
  }, [up.busy]);
  useEffect(() => () => busyRef.current?.(false), []);
  return (
    <div data-testid="annotations-field">
      {up.picker}
      <Space wrap>
        <Button size="small" loading={up.busy} onClick={up.open}>
          {up.label ?? (value ? zh.annotations.replace : zh.annotations.pick)}
        </Button>
        {value ? (
          <>
            <span data-testid="annotations-uploaded">{uploadedText(value)}</span>
            <Button size="mini" type="text" onClick={() => onChange(null)}>
              {zh.annotations.remove}
            </Button>
          </>
        ) : null}
      </Space>
      <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
        {zh.annotations.hint}
      </div>
      {up.error}
    </div>
  );
}

/** The detail page's 外部标注文件 row: what is attached, 更换 (upload, then attach) and 摘掉. */
export function DatasetAnnotations({ d }: { d: Pick<DatasetDetail, 'id' | 'annotations'> }) {
  const qc = useQueryClient();
  const [saving, setSaving] = useState(false);
  const attach = async (uploadId: string | null) => {
    setSaving(true);
    try {
      await unwrap(api().PUT('/datasets/{id}/annotations', { params: { path: { id: d.id }, header: { 'Idempotency-Key': idempotencyKey() } }, body: { upload_id: uploadId } }));
      Message.success(uploadId ? zh.annotations.attached : zh.annotations.detached);
      void qc.invalidateQueries({ queryKey: qk.dataset(d.id) });
      void qc.invalidateQueries({ queryKey: ['viz'] });
    } catch (e) {
      Message.error(errorMessage(e));
    } finally {
      setSaving(false);
    }
  };
  const up = useAnnotationUpload((u) => void attach(u.upload_id));
  const a = d.annotations;
  return (
    <div data-testid="dataset-annotations">
      {up.picker}
      <Space wrap>
        <span>{a ? zh.annotations.info(a.name, a.format, a.episodes) : <span className="muted">{zh.annotations.none}</span>}</span>
        <Button size="mini" loading={up.busy || saving} onClick={up.open}>
          {up.label ?? (a ? zh.annotations.replace : zh.annotations.pick)}
        </Button>
        {a ? (
          <Button
            size="mini"
            type="text"
            status="danger"
            disabled={saving}
            onClick={() =>
              Modal.confirm({
                title: zh.annotations.removeTitle,
                content: zh.annotations.removeContent,
                okText: zh.annotations.remove,
                cancelText: zh.common.cancel,
                onOk: () => attach(null),
              })
            }
          >
            {zh.annotations.remove}
          </Button>
        ) : null}
      </Space>
      {up.error}
    </div>
  );
}
