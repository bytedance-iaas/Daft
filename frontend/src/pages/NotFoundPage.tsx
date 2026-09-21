import { Button, Result } from '@arco-design/web-react';
import { Link } from 'react-router-dom';
import { useDocumentTitle } from '../components/PageHeader';
import { zh } from '../locales/zh';

export function NotFoundPage() {
  useDocumentTitle(zh.errors.routeNotFound);
  return (
    <Result
      status="404"
      title={zh.errors.routeNotFound}
      extra={
        <Link to="/overview">
          <Button type="primary">{zh.errors.backHome}</Button>
        </Link>
      }
    />
  );
}
