import { Breadcrumb } from '@arco-design/web-react';
import { useEffect, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { zh } from '../locales/zh';

export interface Crumb {
  label: string;
  to?: string;
}

/** Breadcrumb + title + description + actions; also sets the document title. */
export function PageHeader({
  crumbs,
  title,
  titleExtra,
  description,
  extra,
  docTitle,
}: {
  crumbs: Crumb[];
  title: ReactNode;
  titleExtra?: ReactNode;
  description?: ReactNode;
  extra?: ReactNode;
  docTitle?: string;
}) {
  useDocumentTitle(docTitle ?? (typeof title === 'string' ? title : crumbs[crumbs.length - 1]?.label ?? ''));
  return (
    <div className="page-header">
      <Breadcrumb>
        <Breadcrumb.Item>{zh.nav.breadcrumbRoot}</Breadcrumb.Item>
        {crumbs.map((c, i) => (
          <Breadcrumb.Item key={i}>{c.to ? <Link to={c.to}>{c.label}</Link> : c.label}</Breadcrumb.Item>
        ))}
      </Breadcrumb>
      <div className="page-header-row">
        <div style={{ minWidth: 0 }}>
          <h1>
            <span>{title}</span>
            {titleExtra}
          </h1>
          {description ? <div className="page-desc">{description}</div> : null}
        </div>
        {extra ? <div style={{ flex: 'none', display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', justifyContent: 'flex-end' }}>{extra}</div> : null}
      </div>
    </div>
  );
}

export function useDocumentTitle(page: string) {
  useEffect(() => {
    if (page) document.title = zh.app.docTitle(page);
  }, [page]);
}
