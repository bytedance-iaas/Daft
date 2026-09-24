// POST /uploads (C4 1.8.0): the input file of a module parameter, validated on arrival. The body
// is the file itself sent as JSON (every write is JSON, doc 03 §1); a .jsonl seed file is turned
// into a JSON array first, the form the Daemon accepts for kind eef_observation_seeds.
// XMLHttpRequest rather than fetch: its upload events tell sending the file (上传中) from the
// server checking it (校验中) apart (fourth round).
import { apiBaseUrl } from '../base';
import { zh } from '../locales/zh';
import { ApiError } from './errors';
import type { Upload, UploadKind } from './types';

/** `uploading` while the file goes out, `validating` once it is all sent and the server checks it. */
export type UploadPhase = 'uploading' | 'validating';

/** JSON text of a JSONL document (one value per non-empty line) as a JSON array. */
export function jsonlToArray(text: string): string {
  const rows = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, i) => {
      try {
        return JSON.parse(line) as unknown;
      } catch {
        throw new Error(zh.taskForm.uploadBadLine(i + 1));
      }
    });
  return JSON.stringify(rows);
}

function post(payload: string, kind: UploadKind, name: string, onPhase?: (p: UploadPhase) => void): Promise<Upload> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${apiBaseUrl()}/uploads?${new URLSearchParams({ kind, name })}`);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.setRequestHeader('Accept', 'application/json');
    // Some environments never report the upload itself: the answer still ends the call.
    xhr.upload.addEventListener('load', () => onPhase?.('validating'));
    const answer = (): unknown => {
      try {
        return xhr.responseText ? (JSON.parse(xhr.responseText) as unknown) : null;
      } catch {
        return null;
      }
    };
    xhr.addEventListener('load', () => {
      const body = answer();
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as Upload);
      else reject(ApiError.fromResponse(xhr.status, body));
    });
    xhr.addEventListener('error', () => reject(ApiError.network(new Error('upload failed'))));
    onPhase?.('uploading');
    xhr.send(payload);
  });
}

export async function uploadText(text: string, kind: UploadKind, name: string, onPhase?: (p: UploadPhase) => void): Promise<Upload> {
  const payload = kind === 'eef_observation_seeds' && name.toLowerCase().endsWith('.jsonl') ? jsonlToArray(text) : text;
  return post(payload, kind, name, onPhase);
}

/** A picked file's text (FileReader: works wherever a file input does). */
export function readText(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error ?? new Error(zh.taskForm.uploadReadFailed));
    reader.readAsText(file);
  });
}

export async function uploadFile(file: File, kind: UploadKind, onPhase?: (p: UploadPhase) => void): Promise<Upload> {
  const text = await readText(file);
  return uploadText(text, kind, file.name, onPhase);
}
