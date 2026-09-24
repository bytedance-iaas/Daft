import { Select } from '@arco-design/web-react';
import { zh } from '../locales/zh';

export const REGIONS = Object.keys(zh.regions);

export function regionLabel(code: string | null | undefined): string {
  if (!code) return '—';
  const name = zh.regions[code];
  return name ? `${name} ${code}` : code;
}

/**
 * 地域 (07 §8): the reviewed three, plus any other code typed in (deep links from the rerun
 * viewer may carry e.g. ap-southeast-1). Values are always the region codes.
 */
export function RegionSelect({
  value,
  onChange,
  disabled,
  placeholder,
  allowEmpty,
  emptyLabel,
  exclude,
  ariaLabel,
  status,
}: {
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  placeholder?: string;
  allowEmpty?: boolean;
  emptyLabel?: string;
  /** Left out of the list (the empty entry stands for it), unless it is the value. */
  exclude?: string;
  ariaLabel?: string;
  status?: 'error';
}) {
  const known = REGIONS.filter((r) => r !== exclude || r === value);
  if (value && !known.includes(value)) known.push(value);
  return (
    <Select
      value={value}
      onChange={(v: string) => onChange((v ?? '').trim().toLowerCase())}
      disabled={disabled}
      placeholder={placeholder ?? zh.taskForm.regionPlaceholder}
      allowCreate
      showSearch
      status={status}
      aria-label={ariaLabel}
      options={[
        ...(allowEmpty ? [{ label: emptyLabel ?? '', value: '' }] : []),
        ...known.map((r) => ({ label: regionLabel(r), value: r })),
      ]}
    />
  );
}
