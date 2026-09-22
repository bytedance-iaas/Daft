import { Form } from '@arco-design/web-react';
import type { ReactNode } from 'react';

/**
 * A labelled field of the new-task form: Arco's red * for required fields, the local or server
 * error under it (field-level errors, 07 §9), and persistent red deep-link notes (07 §2.1).
 */
export function Field({
  label,
  required,
  error,
  extra,
  notes,
  ok,
  warn,
  children,
  testId,
}: {
  label: ReactNode;
  required?: boolean;
  error?: string;
  extra?: ReactNode;
  notes?: readonly string[];
  ok?: ReactNode;
  /** Passed, but with something to fix by hand (a leftover probe object). */
  warn?: ReactNode;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <Form.Item label={label} required={required} validateStatus={error ? 'error' : warn ? 'warning' : undefined} help={error} extra={extra} data-testid={testId}>
      {children}
      {ok ? <div className="field-note-ok">{ok}</div> : null}
      {warn ? (
        <div className="field-note-warn" role="status">
          {warn}
        </div>
      ) : null}
      {notes?.map((n) => (
        <div className="field-note-error" key={n} role="alert">
          {n}
        </div>
      ))}
    </Form.Item>
  );
}
