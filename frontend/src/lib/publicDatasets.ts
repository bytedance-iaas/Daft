// The HuggingFace cache bucket's datasets as the add-dataset picker shows them (C4 7.3.0: every format the platform
// reads, not only LeRobot - requester 2026-10-12).
import { zh } from '../locales/zh';

/** A cache-bucket dataset in the picker: its name, format and episode count; a raw UMI session runs the EEF module only. */
export function publicOptionLabel(d: { name: string; format_hint?: string; episodes?: number | null }): string {
  const format = d.format_hint && d.format_hint !== 'unknown' ? zh.format[d.format_hint] ?? d.format_hint : '';
  const label = format ? zh.taskForm.publicOption(d.name, format, d.episodes ?? null) : d.name;
  return d.format_hint === 'umi_session' ? `${label}（${zh.taskForm.publicEefOnly}）` : label;
}
