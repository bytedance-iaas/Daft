import { Button, Result } from '@arco-design/web-react';
import { Link } from 'react-router-dom';
import { isApiError, errorMessage } from '../api/errors';
import { zh } from '../locales/zh';

/** Page-level error (doc 07 §9): the page could not load at all. Shows the Daemon's message. */
export function PageError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const notFound = isApiError(error) && (error.code === 'not_found' || error.status === 404);
  return (
    <Result
      status={notFound ? '404' : 'error'}
      title={notFound ? zh.errors.notFound : zh.errors.pageTitle}
      subTitle={errorMessage(error)}
      extra={
        <>
          {onRetry && !notFound ? (
            <Button type="primary" onClick={onRetry}>
              {zh.common.retry}
            </Button>
          ) : null}
          <Link to="/overview" style={{ marginLeft: 8 }}>
            <Button>{zh.errors.backHome}</Button>
          </Link>
        </>
      }
    />
  );
}
