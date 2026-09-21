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
  children,
  testId,
}: {
  label: ReactNode;
  required?: boolean;
  error?: string;
  extra?: ReactNode;
  notes?: readonly string[];
  ok?: ReactNode;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <Form.Item label={label} required={required} validateStatus={error ? 'error' : undefined} help={error} extra={extra} data-testid={testId}>
      {children}
      {ok ? <div className="field-note-ok">{ok}</div> : null}
      {notes?.map((n) => (
        <div className="field-note-error" key={n} role="alert">
          {n}
        </div>
      ))}
    </Form.Item>
  );
}
