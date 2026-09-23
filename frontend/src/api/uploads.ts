// POST /uploads (C4 1.8.0): the input file of a module parameter, validated on arrival. The body
// is the file itself sent as JSON (every write is JSON, doc 03 §1); a .jsonl seed file is turned
// into a JSON array first, the form the Daemon accepts for kind eef_observation_seeds.
import { zh } from '../locales/zh';
import { api, unwrap } from './client';
import type { Upload, UploadKind } from './types';

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

export async function uploadText(text: string, kind: UploadKind, name: string): Promise<Upload> {
  const payload = kind === 'eef_observation_seeds' && name.toLowerCase().endsWith('.jsonl') ? jsonlToArray(text) : text;
  return unwrap(
    api().POST('/uploads', {
      params: { query: { kind, name } },
      body: payload as unknown as never,
      bodySerializer: (b: unknown) => b as string,
    }),
  );
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

export async function uploadFile(file: File, kind: UploadKind): Promise<Upload> {
  return uploadText(await readText(file), kind, file.name);
}
