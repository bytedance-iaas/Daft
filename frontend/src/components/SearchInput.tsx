import { Input } from '@arco-design/web-react';
import { IconSearch } from '@arco-design/web-react/icon';
import { useEffect, useState } from 'react';

/**
 * A search box that submits on Enter or on the icon. Uses KeyboardEvent.key rather than the
 * deprecated keyCode Arco's Input.Search listens to.
 */
export function SearchInput({ value, placeholder, onSearch, width = 240 }: { value: string; placeholder: string; onSearch: (v: string) => void; width?: number }) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  return (
    <Input
      style={{ width }}
      allowClear
      value={text}
      placeholder={placeholder}
      aria-label={placeholder}
      onChange={setText}
      onClear={() => onSearch('')}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onSearch(text.trim());
      }}
      suffix={<IconSearch style={{ cursor: 'pointer' }} onClick={() => onSearch(text.trim())} aria-label={placeholder} />}
    />
  );
}
